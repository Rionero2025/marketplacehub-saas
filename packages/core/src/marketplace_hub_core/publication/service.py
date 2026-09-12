import hashlib
import json
import time
from collections import Counter
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from sqlalchemy import select, update

from marketplace_hub_core.auth.models import AuthenticatedSession, AuthRealm
from marketplace_hub_core.catalogs.schema import seller_catalog_view_rows as view_rows
from marketplace_hub_core.catalogs.schema import seller_price_lists as lists
from marketplace_hub_core.catalogs.schema import seller_suppliers as suppliers
from marketplace_hub_core.catalogs.work import CatalogWorkRepository
from marketplace_hub_core.marketplace_connections.repository import (
    SqlMarketplaceConnectionsRepository,
)
from marketplace_hub_core.marketplace_connections.security import decrypt_credentials
from marketplace_hub_core.publication.connectors import PublicationConnector, RemoteFailure
from marketplace_hub_core.publication.models import Rules
from marketplace_hub_core.publication.pricing import edit_offer, prepare
from marketplace_hub_core.publication.schema import items, jobs


class PublicationError(ValueError):
    pass


def now():
    return datetime.now(UTC)


def digest(account):
    return hashlib.sha256(account["credentials_encrypted"].encode()).hexdigest()


def draft_version(job, rows):
    value = [
        str(job["id"]),
        job["status"],
        job["rules_json"],
        [(str(r["id"]), r["public_json"], r["payload_json"], r["status"]) for r in rows],
    ]
    return hashlib.sha256(json.dumps(value).encode()).hexdigest()


class PublicationService:
    def __init__(self, engine, workspace, master_key, queue=None, connector=None):
        self.engine, self.workspace, self.master_key = engine, workspace, master_key
        self.queue, self.connector = queue, connector or PublicationConnector()
        self.work = CatalogWorkRepository(engine)
        self.accounts = SqlMarketplaceConnectionsRepository(engine)

    def scope(self, principal, seller, write=False):
        value = self.workspace.require_seller(principal, seller, permission="CATALOG", write=write)
        return UUID(value["organization_id"]), value

    def account(self, org, seller, account_id):
        a = self.accounts.get(seller, org, account_id)
        if not a["active"] or a["marketplace"] not in {"kaufland", "worten"}:
            raise PublicationError("Pubblicazione non disponibile per questo account.")
        return a

    def index(self, principal, seller):
        org, access = self.scope(principal, seller)
        result = self.work.index(org, seller)
        with self.engine.connect() as c:
            ids = (
                c.execute(
                    select(jobs.c.id)
                    .where(*self.work.scope(jobs, org, seller))
                    .order_by(jobs.c.created_at.desc())
                    .limit(30)
                )
                .scalars()
                .all()
            )
        result.update(
            seller_id=str(seller),
            can_manage="CATALOG" in access["write_permissions"],
            jobs=[self.detail(principal, seller, j, rows=False) for j in ids],
        )
        return result

    def options(self, principal, seller, account_id, storefront, playground):
        org, _ = self.scope(principal, seller)
        a = self.account(org, seller, account_id)
        return {
            "seller_id": str(seller),
            "account_id": str(account_id),
            **self.connector.options(
                a["marketplace"],
                decrypt_credentials(a["credentials_encrypted"], self.master_key),
                storefront,
                playground,
            ),
        }

    def preview(self, principal, seller, rules):
        org, _ = self.scope(principal, seller, True)
        a = self.account(org, seller, rules.account_id)
        if a["marketplace"] == "worten":
            if rules.storefront != "pt" or rules.playground:
                raise PublicationError(
                    "Worten usa Portogallo e ambiente reale; controlla la selezione."
                )
        elif rules.handling > 30:
            raise PublicationError("Kaufland consente al massimo 30 giorni di preparazione.")
        elif rules.storefront == "pt":
            raise PublicationError("Paese non supportato da Kaufland.")
        if (
            a["marketplace"] == "kaufland"
            and rules.storefront in {"cz", "pl"}
            and not rules.fx_date
        ):
            raise PublicationError("Indica il cambio EUR e la relativa data per CZK o PLN.")
        with self.engine.begin() as c:
            view = self.work._view(c, org, seller, rules.view_id, lock=True)
            if view["revision"] != rules.revision:
                raise PublicationError(
                    "La vista è cambiata: ricarica le viste prima di preparare l’invio."
                )
            if str(rules.account_id) not in json.loads(view["accounts_json"]):
                raise PublicationError("La vista non è destinata a questo account marketplace.")
            recipe = json.loads(view["recipe_json"])
            supplier = c.execute(
                select(suppliers.c.name)
                .join(lists, lists.c.supplier_id == suppliers.c.id)
                .where(
                    lists.c.id == UUID(recipe["price_list_id"]),
                    *self.work.scope(lists, org, seller),
                    *self.work.scope(suppliers, org, seller),
                )
            ).scalar_one_or_none()
            if supplier is None:
                raise PublicationError("Listino di origine non disponibile per la pubblicazione.")
            selected, count, last = [], 0, -1
            # Never materialize the full supplier feed or the whole saved view.
            while True:
                batch = (
                    c.execute(
                        select(view_rows)
                        .where(view_rows.c.view_id == rules.view_id, view_rows.c.position > last)
                        .order_by(view_rows.c.position)
                        .limit(200)
                    )
                    .mappings()
                    .all()
                )
                if not batch:
                    break
                last = batch[-1]["position"]
                for row in batch:
                    calculated = prepare(
                        json.loads(row["data_json"]), supplier, a["marketplace"], rules
                    )
                    if calculated is None:
                        continue
                    count += 1
                    if rules.start <= count < rules.start + rules.limit:
                        selected.append(calculated)
            if not selected:
                raise PublicationError("Nessun prodotto nell’intervallo e nei filtri scelti.")
            duplicates = Counter(v[0]["sku"] for v in selected)
            job_id = uuid4()
            c.execute(
                jobs.insert().values(
                    id=job_id,
                    organization_id=org,
                    seller_id=seller,
                    account_id=a["id"],
                    requested_by=principal.user_id,
                    realm=principal.realm.value,
                    view_id=view["id"],
                    view_name=view["name"],
                    account_name=a["account_name"],
                    credentials_digest=digest(a),
                    marketplace=a["marketplace"],
                    rules_json=rules.model_dump_json(),
                    status="draft",
                    total=len(selected),
                    filtered_total=count,
                    created_at=now(),
                    updated_at=now(),
                )
            )
            for pos, (public, payload) in enumerate(selected, rules.start):
                if duplicates[public["sku"]] > 1:
                    public["problem"] = "SKU duplicato nell’intervallo"
                c.execute(
                    items.insert().values(
                        id=uuid4(),
                        job_id=job_id,
                        position=pos,
                        public_json=json.dumps(public),
                        payload_json=json.dumps(payload),
                        status="invalid" if public["problem"] else "pending",
                        result_code="",
                    )
                )
        return self.detail(principal, seller, job_id)

    def _job(self, c, org, seller, job_id, lock=False):
        stmt = select(jobs).where(jobs.c.id == job_id, *self.work.scope(jobs, org, seller))
        j = c.execute(stmt.with_for_update() if lock else stmt).mappings().first()
        if not j:
            raise PublicationError("Invio non disponibile.")
        return j

    def detail(self, principal, seller, job_id, rows=True):
        org, _ = self.scope(principal, seller)
        with self.engine.begin() as c:
            j = self._job(c, org, seller, job_id, lock=True)
            updated = (
                j["updated_at"].replace(tzinfo=UTC)
                if j["updated_at"].tzinfo is None
                else j["updated_at"]
            )
            if j["status"] in {"queued", "running"} and now() - updated > timedelta(
                minutes=30 if j["status"] == "queued" else 3
            ):
                c.execute(
                    update(jobs)
                    .where(jobs.c.id == job_id)
                    .values(status="interrupted", updated_at=now())
                )
                c.execute(
                    update(items)
                    .where(items.c.job_id == job_id, items.c.status == "sending")
                    .values(status="unknown", result_code="worker_interrupted")
                )
                j = self._job(c, org, seller, job_id)
            data = (
                c.execute(select(items).where(items.c.job_id == job_id).order_by(items.c.position))
                .mappings()
                .all()
            )
            counts = Counter(r["status"] for r in data)
            result = {
                "id": str(j["id"]),
                "version": draft_version(j, data),
                "seller_id": str(seller),
                "account_id": str(j["account_id"]),
                "marketplace": j["marketplace"],
                "account_name": j["account_name"],
                "view_name": j["view_name"],
                "status": j["status"],
                "total": j["total"],
                "filtered_total": j["filtered_total"],
                "counts": dict(counts),
                "rules": json.loads(j["rules_json"]),
                "created_at": j["created_at"].isoformat(),
                "rows": [
                    {
                        "id": str(r["id"]),
                        "position": r["position"],
                        "status": r["status"],
                        "result_code": r["result_code"],
                        **json.loads(r["public_json"]),
                    }
                    for r in data
                ]
                if rows
                else [],
            }
        return result

    def edit(self, principal, seller, job_id, body):
        org, _ = self.scope(principal, seller, True)
        with self.engine.begin() as c:
            j = self._job(c, org, seller, job_id, lock=True)
            if j["status"] != "draft":
                raise PublicationError("Puoi modificare solo un’anteprima non ancora inviata.")
            data = (
                c.execute(select(items).where(items.c.job_id == job_id).order_by(items.c.position))
                .mappings()
                .all()
            )
            if body.version != draft_version(j, data):
                raise PublicationError(
                    "Anteprima modificata in un’altra sessione. Riaprila prima di salvare."
                )
            changes = {r.id: r.model_dump(exclude_unset=True, exclude={"id"}) for r in body.rows}
            if len(changes) != len(body.rows) or not set(changes) <= {r["id"] for r in data}:
                raise PublicationError("Righe non appartenenti all’anteprima.")
            rules = Rules.model_validate_json(j["rules_json"])
            prepared = [
                (
                    r,
                    *edit_offer(
                        json.loads(r["public_json"]),
                        json.loads(r["payload_json"]),
                        changes.get(r["id"], {}),
                        j["marketplace"],
                        rules,
                    ),
                )
                for r in data
            ]
            duplicates = Counter(p["sku"] for _, p, _ in prepared)
            for row, public, payload in prepared:
                if duplicates[public["sku"]] > 1:
                    public["problem"] = "SKU duplicato nell’intervallo"
                c.execute(
                    update(items)
                    .where(items.c.id == row["id"])
                    .values(
                        public_json=json.dumps(public),
                        payload_json=json.dumps(payload),
                        status="invalid" if public["problem"] else "pending",
                    )
                )
            c.execute(update(jobs).where(jobs.c.id == job_id).values(updated_at=now()))
        return self.detail(principal, seller, job_id)

    def submit(self, principal, seller, job_id, selected, version=None):
        org, _ = self.scope(principal, seller, True)
        with self.engine.begin() as c:
            j = self._job(c, org, seller, job_id, lock=True)
            if version is not None:
                data = (
                    c.execute(
                        select(items).where(items.c.job_id == job_id).order_by(items.c.position)
                    )
                    .mappings()
                    .all()
                )
                if version != draft_version(j, data):
                    raise PublicationError(
                        "Anteprima cambiata. Riaprila e controlla i valori prima dell’invio."
                    )
            if j["status"] not in {"draft", "interrupted"}:
                raise PublicationError(
                    "Invio già registrato. Consulta lo storico: nessun secondo invio creato."
                )
            a = self.account(org, seller, j["account_id"])
            if digest(a) != j["credentials_digest"]:
                raise PublicationError("Le credenziali sono cambiate. Prepara una nuova anteprima.")
            rules = Rules.model_validate_json(j["rules_json"])
            if j["status"] == "draft":
                if now() - (j["created_at"].replace(tzinfo=UTC)) > timedelta(hours=2):
                    raise PublicationError("Anteprima scaduta. Preparane una nuova.")
                view = self.work._view(c, org, seller, j["view_id"], lock=True)
                if view["revision"] != rules.revision or str(a["id"]) not in json.loads(
                    view["accounts_json"]
                ):
                    raise PublicationError(
                        "Vista o destinazioni modificate. Prepara una nuova anteprima."
                    )
            if a["marketplace"] == "kaufland" and (not rules.shipping_group or not rules.warehouse):
                raise PublicationError(
                    "Configura gruppo spedizione e magazzino prima dell’anteprima."
                )
            pending = set(
                c.execute(
                    select(items.c.id).where(items.c.job_id == job_id, items.c.status == "pending")
                ).scalars()
            )
            chosen = set(selected)
            if not chosen or not chosen <= pending:
                raise PublicationError("Seleziona solo prodotti validi ancora da inviare.")
            c.execute(
                update(items)
                .where(
                    items.c.job_id == job_id, items.c.status == "pending", items.c.id.not_in(chosen)
                )
                .values(status="skipped")
            )
            c.execute(
                update(jobs)
                .where(jobs.c.id == job_id)
                .values(
                    status="queued",
                    updated_at=now(),
                    requested_by=principal.user_id,
                    realm=principal.realm.value,
                )
            )
        try:
            if self.queue is None:
                raise RuntimeError
            self.queue.enqueue(job_id)
        except Exception:
            # Enqueue might have reached Redis. Leave the durable receipt intact;
            # stale recovery and an atomic worker claim make a repeated request safe.
            raise PublicationError(
                "Invio registrato; coda non confermata. Controlla lo storico prima di riprovare."
            ) from None
        return self.detail(principal, seller, job_id)

    def run(self, job_id):
        execution_token = uuid4()
        with self.engine.begin() as c:
            claimed = c.execute(
                update(jobs)
                .where(jobs.c.id == job_id, jobs.c.status == "queued")
                .values(status="running", execution_token=execution_token, updated_at=now())
            )
            if claimed.rowcount != 1:
                return
            j = dict(c.execute(select(jobs).where(jobs.c.id == job_id)).mappings().one())
        principal = AuthenticatedSession(
            UUID(int=0),
            j["requested_by"],
            "",
            "",
            AuthRealm(j["realm"]),
            datetime.max.replace(tzinfo=UTC),
        )
        rules = Rules.model_validate_json(j["rules_json"])
        try:
            while True:
                org, _ = self.scope(principal, j["seller_id"], True)
                a = self.account(org, j["seller_id"], j["account_id"])
                if digest(a) != j["credentials_digest"]:
                    raise PublicationError("Account modificato.")
                credentials = decrypt_credentials(a["credentials_encrypted"], self.master_key)
                with self.engine.begin() as c:
                    current = self._job(c, org, j["seller_id"], job_id, lock=True)
                    if (
                        current["status"] != "running"
                        or current["execution_token"] != execution_token
                    ):
                        return
                    pending = (
                        c.execute(
                            select(items)
                            .where(items.c.job_id == job_id, items.c.status == "pending")
                            .order_by(items.c.position)
                            .limit(1 if j["marketplace"] == "kaufland" else 100)
                        )
                        .mappings()
                        .all()
                    )
                    if not pending:
                        c.execute(
                            update(jobs)
                            .where(jobs.c.id == job_id)
                            .values(status="finished", updated_at=now())
                        )
                        return
                    ids = [r["id"] for r in pending]
                    c.execute(update(items).where(items.c.id.in_(ids)).values(status="sending"))
                    c.execute(update(jobs).where(jobs.c.id == job_id).values(updated_at=now()))
                uncertain = False
                try:
                    state, code = self.connector.send(
                        j["marketplace"],
                        credentials,
                        rules,
                        [json.loads(r["payload_json"]) for r in pending],
                    )
                except RemoteFailure as exc:
                    uncertain = exc.uncertain
                    state, code = ("unknown" if uncertain else "rejected"), exc.code
                with self.engine.begin() as c:
                    # Recovery may already have marked a request unknown: a late
                    # response must not restart a job or overwrite that decision.
                    c.execute(
                        update(items)
                        .where(items.c.id.in_(ids), items.c.status == "sending")
                        .values(status=state, result_code=code)
                    )
                    c.execute(
                        update(jobs)
                        .where(
                            jobs.c.id == job_id,
                            jobs.c.status == "running",
                            jobs.c.execution_token == execution_token,
                        )
                        .values(status="interrupted" if uncertain else "running", updated_at=now())
                    )
                if uncertain:
                    return
                time.sleep(0.1)
        except Exception:
            with self.engine.begin() as c:
                current = (
                    c.execute(select(jobs).where(jobs.c.id == job_id).with_for_update())
                    .mappings()
                    .one()
                )
                if current["execution_token"] != execution_token:
                    return
                c.execute(
                    update(items)
                    .where(items.c.job_id == job_id, items.c.status == "sending")
                    .values(status="unknown", result_code="execution_interrupted")
                )
                c.execute(
                    update(jobs)
                    .where(
                        jobs.c.id == job_id,
                        jobs.c.status == "running",
                        jobs.c.execution_token == execution_token,
                    )
                    .values(status="interrupted", updated_at=now())
                )
