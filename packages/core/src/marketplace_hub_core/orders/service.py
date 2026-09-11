import json
import re
from datetime import UTC, datetime, timedelta
from uuid import UUID

from pydantic import SecretStr
from sqlalchemy.exc import IntegrityError, SQLAlchemyError

from marketplace_hub_core.auth.models import AuthenticatedSession, AuthRealm
from marketplace_hub_core.catalogs.costing import SqlCatalogCostResolver
from marketplace_hub_core.marketplace_connections.repository import (
    VERIFICATION_KEY,
    SqlMarketplaceConnectionsRepository,
    settings_object,
)
from marketplace_hub_core.marketplace_connections.security import decrypt_credentials
from marketplace_hub_core.orders.export import export_csv
from marketplace_hub_core.orders.filters import OrderFilters
from marketplace_hub_core.orders.repository import (
    OrdersAccountUnavailableError,
    OrdersAmbiguousMatchError,
    OrdersImportBusyError,
    OrdersNotFoundError,
    SqlOrdersRepository,
    utc,
)
from marketplace_hub_core.orders.selection import SqlOrderSelections
from marketplace_hub_core.orders.tracking import (
    FORMATS,
    MAX_CELL_LENGTH,
    MAX_CELLS,
    MAX_COLUMNS,
    MAX_FILE_BYTES,
    MAX_ROWS,
    PREVIEW_ROWS,
    clean,
    mapped_tracking_rows,
    normalize_tracking,
    parse_tracking_file,
    validate_mapping,
)
from marketplace_hub_core.seller_settings.repository import MarketplaceAccountNotFoundError
from marketplace_hub_core.seller_settings.security import CredentialStorageUnavailableError
from marketplace_hub_core.tenancy.service import (
    SellerNotAccessibleError,
    WorkspacePermissionError,
    WorkspaceService,
)

PUBLIC_FIELDS = {
    "external_line_id", "order_id", "marketplace", "created_at", "status", "status_label",
    "storefront", "currency", "product_name", "ean", "sku", "quantity", "sale_amount",
    "shipping_amount", "commission_amount", "commission_rate", "payout_amount", "purchase_cost",
    "purchase_cost_source", "profit_amount", "profit_pct", "sale_amount_eur",
    "shipping_amount_eur", "commission_amount_eur", "payout_amount_eur", "purchase_cost_eur",
    "profit_amount_eur", "monetary_warnings", "details",
}
PROGRESS = {
    "downloading": "Download ordini in corso.", "details": "Recupero dettagli ordini.",
    "normalizing": "Preparazione delle righe ordine.", "saving": "Salvataggio ordini.",
    "waiting_retry": "Il marketplace richiede un'attesa. Nuovo tentativo in corso.",
}
PROGRESS_PATTERN = re.compile(
    r"Download ordini (?:cancelled|need_to_be_sent|open|received|returned|returned_paid|sent|"
    r"sent_and_autopaid): [0-9]{1,12}|Download ordini Worten: [0-9]{1,12} ordini, [0-9]{1,12} righe"
)
ERROR_MESSAGES = {
    "permission_revoked": "Autorizzazione al negozio revocata. Sincronizzazione interrotta.",
    "account_unavailable": "Account marketplace non disponibile o da verificare.",
    "account_busy": "Un’importazione tracking è in corso. "
    "Riprova la sincronizzazione tra poco.",
    "credentials_unavailable": "Credenziali marketplace non disponibili per la sincronizzazione.",
    "invalid_credentials": "Il marketplace non ha accettato le credenziali API.",
    "permission_denied": "Le credenziali API non autorizzano la lettura degli ordini.",
    "unexpected_response": "Il marketplace ha restituito dati ordine non riconoscibili.",
    "invalid_response": "Il marketplace ha restituito dati ordine non riconoscibili.",
    "invalid_configuration": "Configurazione marketplace non valida per la sincronizzazione.",
    "unsupported_marketplace": "Ordini non disponibili per questo marketplace.",
    "rate_limited": "Limite di richieste marketplace raggiunto. Riprova più tardi.",
    "upstream_unavailable": "Marketplace temporaneamente non disponibile. Riprova.",
    "timeout": "Tempo di sincronizzazione esaurito. Riprova.",
    "queue_unavailable": "Coda temporaneamente non disponibile. Riprova.",
    "worker_interrupted": "Sincronizzazione interrotta dal worker. Puoi avviarla di nuovo.",
    "sync_failed": "Sincronizzazione non completata. I dati già salvati restano disponibili.",
}


class OrdersValidationError(ValueError):
    pass


class OrdersQueueUnavailableError(RuntimeError):
    pass


class _ExportStream:
    """Own the temporary export until normal completion, failure or cancellation."""

    def __init__(self, rows, guard):
        self._rows = rows
        self._output = export_csv(rows, guard)
        self._closed = False

    def __iter__(self):
        return self

    def __next__(self):
        if self._closed:
            raise StopIteration
        try:
            return next(self._output)
        except BaseException:
            self.close()
            raise

    def close(self):
        if self._closed:
            return
        self._closed = True
        try:
            self._output.close()
        finally:
            self._rows.close()

    def __del__(self):
        try:
            self.close()
        except Exception:
            pass


def job_payload(job):
    if job is None:
        return None
    total, processed = job["total"], job["processed"]
    progress = 100 if job["status"] == "done" else (
        min(99, int(processed / total * 100)) if total else None
    )
    return {
        "id": str(job["id"]), "status": job["status"], "processed": processed,
        "total": total, "progress": progress, "message": job["message"],
        "error_code": job["error_code"], "account_id": str(job["account_id"]),
        "marketplace": job["marketplace"], "environment": job["environment"],
        "maximum": job["maximum"], "include_details": job["include_details"],
        **{key: utc(job[key]).isoformat() if job[key] else None
           for key in ("created_at", "started_at", "finished_at")},
    }


def _safe_details(value, depth=0):
    if depth > 5:
        return None
    if isinstance(value, dict):
        sensitive = {"raw", "credentials", "api_key", "secret_key", "client_key", "authorization",
                     "access_token", "password", "token"}
        return {str(key): _safe_details(item, depth + 1) for key, item in value.items()
                if str(key).lower() not in sensitive}
    if isinstance(value, list):
        return [_safe_details(item, depth + 1) for item in value]
    return value if value is None or isinstance(value, str | int | float | bool) else None


def item_payload(row):
    values = json.loads(row["canonical_json"])
    result = {key: values.get(key) for key in PUBLIC_FIELDS}
    result["details"] = _safe_details(result.get("details") or {})
    result["id"] = str(row["id"])
    return result


class OrdersService:
    def __init__(self, repository: SqlOrdersRepository, workspace: WorkspaceService,
                 accounts: SqlMarketplaceConnectionsRepository, queue, master_key: SecretStr,
                 fetcher=None, catalog_cost_resolver=None):
        self.repository, self.workspace, self.accounts = repository, workspace, accounts
        self.queue, self.master_key, self.fetcher = queue, master_key, fetcher
        self.catalog_cost_resolver = (
            catalog_cost_resolver or SqlCatalogCostResolver(repository.engine)
        )
        self.selections = SqlOrderSelections(repository)

    def _scope(self, principal, seller_id, account_id, environment, *, write=False):
        seller = self.workspace.require_seller(principal, seller_id, permission="LOGISTICS",
                                               write=write)
        organization_id = UUID(seller["organization_id"])
        account = self.accounts.get(seller_id, organization_id, account_id)
        if environment not in {"live", "playground"} or (
            environment == "playground" and account["marketplace"] != "kaufland"
        ):
            raise OrdersValidationError("Ambiente non disponibile per questo marketplace.")
        if account["marketplace"] not in {"kaufland", "worten"}:
            raise OrdersValidationError("Importazione ordini non disponibile nel marketplace.")
        verification = settings_object(account["settings_json"]).get(VERIFICATION_KEY, {})
        if write and (not account["active"] or not isinstance(verification, dict)
                      or verification.get("connection_status") != "connected"):
            raise OrdersValidationError("Verifica il marketplace prima di sincronizzare.")
        return seller, organization_id, account

    def _tracking_scope(self, principal, seller_id, account_id, environment, *, write=False):
        scoped = self._scope(
            principal, seller_id, account_id, environment, write=write,
        )
        if scoped[2]["marketplace"] != "kaufland":
            raise OrdersValidationError("Il recupero tracking è disponibile solo per Kaufland.")
        return scoped

    @staticmethod
    def _validate_payment_filter(account, criteria):
        if account["marketplace"] != "kaufland" and criteria.payment != "all":
            raise OrdersValidationError(
                "Il filtro pagamenti è disponibile solo per Kaufland."
            )

    def _recover(self, job):
        if job is None or job["status"] not in {"queued", "running"}:
            return job
        try:
            state = self.queue.state(job["id"])
        except Exception:
            # A temporary Redis outage does not prove a running worker has failed.
            return job
        aged = datetime.now(UTC) - utc(job["created_at"]) > timedelta(seconds=30)
        interrupted = state in {"failed", "stopped", "canceled", "finished", "stale"}
        if interrupted or (state == "missing" and aged):
            self.repository.finish(job["id"], error_code="worker_interrupted",
                                   message=ERROR_MESSAGES["worker_interrupted"])
            return self.repository.job(job["id"])
        return job

    def list(self, principal, seller_id, account_id, environment, **filters):
        seller, organization_id, account = self._scope(
            principal, seller_id, account_id, environment,
        )
        criteria = filters.get("criteria") or OrderFilters(
            search=filters.get("search", ""), statuses=filters.get("status") or None,
            storefronts=filters.get("storefront") or None,
        )
        if filters.get("date_from"):
            criteria.date_from = utc(filters["date_from"]).date()
        if filters.get("date_to"):
            criteria.date_to = (utc(filters["date_to"]) - timedelta(microseconds=1)).date()
        self._validate_payment_filter(account, criteria)
        payments_enabled = account["marketplace"] == "kaufland"
        rows, total, options, selection, payment_selection = self.selections.list(
            principal.session_id, (organization_id, seller_id, account_id, environment),
            criteria, filters["page"], filters["page_size"], payments=payments_enabled,
        )
        latest = self._recover(self.repository.latest_job(
            organization_id, seller_id, account_id, environment,
        ))
        verification = settings_object(account["settings_json"]).get(VERIFICATION_KEY, {})
        connected = (isinstance(verification, dict)
                     and verification.get("connection_status") == "connected")
        result = {"seller_id": str(seller_id), "account_id": str(account_id),
                "marketplace": account["marketplace"], "environment": environment,
                "can_sync": "LOGISTICS" in seller["write_permissions"] and account["active"]
                and connected, "items": [item_payload(row) for row in rows], "total": total,
                "page": filters["page"], "page_size": filters["page_size"],
                "latest_job": job_payload(latest), "filters": options, "selection": selection}
        if payments_enabled:
            result["payment_selection"] = payment_selection
        return result

    def select(self, principal, seller_id, account_id, environment, selection_id, filters,
               action, line_id=None, selected=None, purpose="orders", orders_selection_id=None):
        _, organization_id, account = self._scope(
            principal, seller_id, account_id, environment,
        )
        self._validate_payment_filter(account, filters)
        if action not in {"set", "clear", "select_all"} or (
            action == "set" and (line_id is None or not isinstance(selected, bool))
        ) or (action != "set" and (line_id is not None or selected is not None)):
            raise OrdersValidationError("Azione di selezione non valida.")
        if purpose not in {"orders", "payments"} or (
            purpose == "payments" and orders_selection_id is None
        ) or (purpose == "orders" and orders_selection_id is not None):
            raise OrdersValidationError("Scopo della selezione non valido.")
        payments_enabled = account["marketplace"] == "kaufland"
        if purpose == "payments" and not payments_enabled:
            raise OrdersValidationError("Selezione pagamenti disponibile solo per Kaufland.")
        self.selections.purge_stale(exclude_session_id=principal.session_id)
        return {"selection": self.selections.change(
            principal.session_id, (organization_id, seller_id, account_id, environment),
            filters, selection_id, action, line_id, selected, purpose=purpose,
            orders_selection_id=orders_selection_id, payments_enabled=payments_enabled,
        )}

    def export(self, principal, seller_id, account_id, environment, selection_id, filters,
               kind, authenticate):
        _, organization_id, account = self._scope(principal, seller_id, account_id, environment)
        self._validate_payment_filter(account, filters)
        scope = (organization_id, seller_id, account_id, environment)
        current_time = datetime.now(UTC)
        self.selections.purge_stale(
            current_time=current_time, exclude_session_id=principal.session_id,
        )
        rows = self.selections.export_rows(
            principal.session_id, scope, filters, selection_id, kind,
            current_time=current_time,
        )
        if not rows.selected_count:
            rows.close()
            raise OrdersValidationError("Seleziona almeno una riga prima di esportare.")

        def guard():
            current = authenticate()
            if current.session_id != principal.session_id:
                raise OrdersValidationError("Sessione non disponibile.")
            self._scope(current, seller_id, account_id, environment)

        return _ExportStream(rows, guard)

    def item(self, principal, seller_id, account_id, environment, line_id):
        _, organization_id, _ = self._scope(principal, seller_id, account_id, environment)
        return {"item": item_payload(self.repository.item(
            organization_id, seller_id, account_id, environment, line_id,
        ))}

    def tracking_capabilities(self, principal, seller_id, account_id, environment):
        seller, _, account = self._tracking_scope(
            principal, seller_id, account_id, environment,
        )
        verification = settings_object(account["settings_json"]).get(VERIFICATION_KEY, {})
        connected = (isinstance(verification, dict)
                     and verification.get("connection_status") == "connected")
        writable = ("LOGISTICS" in seller["write_permissions"]
                    and bool(account["active"]) and connected)
        return {"capabilities": {
            "marketplace": "kaufland", "formats": list(FORMATS),
            "max_bytes": MAX_FILE_BYTES, "max_rows": MAX_ROWS,
            "max_columns": MAX_COLUMNS, "max_cells": MAX_CELLS,
            "max_cell_length": MAX_CELL_LENGTH,
            "preview_rows": PREVIEW_ROWS, "can_import": writable, "can_edit": writable,
        }}

    def authorize_tracking_upload(self, principal, seller_id, account_id, environment):
        self._tracking_scope(principal, seller_id, account_id, environment, write=True)

    def tracking_preview(self, principal, seller_id, account_id, environment, file_name, content):
        self._tracking_scope(principal, seller_id, account_id, environment, write=True)
        parsed = parse_tracking_file(file_name, content)
        return {"status": "ready_for_mapping", "preview": {
            **parsed, "rows": parsed["rows"][:PREVIEW_ROWS],
        }}

    def import_tracking(
        self, principal, seller_id, account_id, environment, file_name, content, mapping,
        authenticate,
    ):
        self._tracking_scope(
            principal, seller_id, account_id, environment, write=True,
        )
        parsed = parse_tracking_file(file_name, content)
        selected = validate_mapping(mapping, parsed["columns"])
        unmatched = []
        invalid = []
        valid = []
        # Materializing validates every bounded identifier before opening the
        # transaction, so a later malformed row cannot leave a partial import.
        records = list(mapped_tracking_rows(parsed["rows"], selected))
        for record in records:
            if not record["order_unit_id"] and not record["order_id"]:
                invalid.append({"row": record["row"], "error": "Identificativo ordine assente"})
                continue
            if not record["carrier"] and not record["tracking"]:
                invalid.append({"row": record["row"], "error": "Tracking/corriere assente"})
                continue
            valid.append(record)
        current = authenticate()
        if current.session_id != principal.session_id:
            raise OrdersValidationError("La sessione è cambiata durante l'importazione.")
        _, organization_id, _ = self._tracking_scope(
            current, seller_id, account_id, environment, write=True,
        )
        imported = self.repository.import_tracking_batch(
            organization_id, seller_id, account_id, environment, records=valid,
            source="portal_import", actor_id=current.user_id,
        )
        unmatched.extend(imported["unmatched"])
        status = "completed_with_warnings" if unmatched or invalid else "completed"
        return {"status": status, "result": {
            "updated": imported["updated"], "unmatched": unmatched, "invalid": invalid,
        }}

    def update_tracking(self, principal, seller_id, account_id, environment, line_id,
                        carrier, tracking):
        _, organization_id, _ = self._tracking_scope(
            principal, seller_id, account_id, environment, write=True,
        )
        carrier_value, tracking_value = clean(carrier), normalize_tracking(tracking)
        if not carrier_value and not tracking_value:
            raise OrdersValidationError("Indica almeno il corriere oppure il tracking.")
        found = self.repository.update_tracking(
            organization_id, seller_id, account_id, environment, line_id=line_id,
            carrier=carrier_value, tracking=tracking_value, source="manual",
            actor_id=principal.user_id,
        )
        if not found:
            raise OrdersNotFoundError("Riga ordine non disponibile.")
        return {"updated": 1, "item": item_payload(self.repository.item(
            organization_id, seller_id, account_id, environment, line_id,
        ))}

    def read_job(self, principal, seller_id, job_id):
        job = self.repository.job(job_id)
        if job["seller_id"] != seller_id:
            raise OrdersNotFoundError("Sincronizzazione non disponibile.")
        _, organization_id, _ = self._scope(principal, seller_id, job["account_id"],
                                             job["environment"])
        if organization_id != job["organization_id"]:
            raise OrdersNotFoundError("Sincronizzazione non disponibile.")
        return {"job": job_payload(self._recover(job))}

    def sync(self, principal, seller_id, account_id, environment, maximum, include_details):
        _, organization_id, account = self._scope(principal, seller_id, account_id, environment,
                                                  write=True)
        if isinstance(maximum, bool) or maximum not in (500, 1000, 5000, None):
            raise OrdersValidationError("Seleziona 500, 1000, 5000 oppure tutti gli ordini.")
        active = self._recover(self.repository.latest_job(
            organization_id, seller_id, account_id, environment, active=True,
        ))
        if active and active["status"] in {"queued", "running"}:
            return {"job": job_payload(active)}
        try:
            job = self.repository.create_job({
                "organization_id": organization_id, "seller_id": seller_id,
                "account_id": account_id, "requested_by": principal.user_id,
                "realm": principal.realm.value, "marketplace": account["marketplace"],
                "environment": environment, "maximum": maximum, "include_details": include_details,
            })
        except IntegrityError:
            active = self.repository.latest_job(organization_id, seller_id, account_id,
                                                 environment, active=True)
            if active:
                return {"job": job_payload(active)}
            raise
        try:
            self.queue.enqueue(job["id"])
        except Exception:
            self.repository.finish(job["id"], error_code="queue_unavailable",
                                   message=ERROR_MESSAGES["queue_unavailable"])
            raise OrdersQueueUnavailableError(ERROR_MESSAGES["queue_unavailable"]) from None
        return {"job": job_payload(job)}

    async def run_job(self, job_id):
        job = self.repository.job(job_id)
        if not self.repository.claim(job_id):
            return
        principal = AuthenticatedSession(
            session_id=UUID(int=0), user_id=job["requested_by"], login="", display_name="",
            realm=AuthRealm(job["realm"]), expires_at=datetime.max.replace(tzinfo=UTC),
        )
        warning_rows = 0
        # Background execution re-evaluates the initiating user's current grants;
        # it does not retain or require a browser session/token after enqueue.
        async def authorized():
            if self.repository.job(job_id)["status"] != "running":
                raise OrdersValidationError("Sincronizzazione non più attiva.")
            return self._scope(principal, job["seller_id"], job["account_id"],
                               job["environment"], write=True)

        async def on_batch(rows, processed, total):
            nonlocal warning_rows
            await authorized()
            self.repository.upsert_batch(
                job,
                rows,
                catalog_cost_resolver=self.catalog_cost_resolver,
            )
            warning_rows += sum(bool(row.get("monetary_warnings")) for row in rows)
            self.repository.progress(job_id, processed=processed, total=total,
                                     message="Salvataggio ordini.")

        async def on_progress(message):
            await authorized()
            safe = message if isinstance(message, str) and PROGRESS_PATTERN.fullmatch(message) \
                else PROGRESS.get(message, PROGRESS["downloading"])
            self.repository.progress(job_id, message=safe)

        try:
            _, _, account = await authorized()
            credentials = decrypt_credentials(account["credentials_encrypted"], self.master_key)
            fetcher = self.fetcher
            if fetcher is None:
                from marketplace_hub_core.orders.connectors import fetch_orders
                fetcher = fetch_orders
            summary = await fetcher(
                job["marketplace"], credentials, environment=job["environment"],
                maximum=job["maximum"], include_details=job["include_details"],
                on_batch=on_batch, on_progress=on_progress, before_request=authorized,
            )
            await authorized()
            summary = summary if isinstance(summary, dict) else {}
            tickets_warning = bool(summary.get("tickets_warning"))
            if "tickets_snapshot" in summary:
                try:
                    self.repository.upsert_payment_ticket_snapshot(
                        job, summary["tickets_snapshot"],
                    )
                except SQLAlchemyError:
                    # Orders are already durable.  A ticket-only schema/storage
                    # failure keeps the previous atomic snapshot and is visible
                    # as a warning, without converting the order job to failed.
                    tickets_warning = True
            detail_warnings = summary.get("warning_count", 0)
            if isinstance(detail_warnings, int) and not isinstance(detail_warnings, bool):
                warning_rows = max(warning_rows, detail_warnings)
            message = "Sincronizzazione completata."
            if warning_rows:
                message += f" {warning_rows} righe con avvisi: consulta i dettagli degli ordini."
            checked = summary.get("details_checked", 0)
            if isinstance(checked, int) and not isinstance(checked, bool) and checked > 0:
                message += f" Dettagli verificati: {checked}."
            if tickets_warning:
                message += " Ticket non aggiornati; conservato l’ultimo snapshot disponibile."
            self.repository.finish(job_id, message=message)
        except (SellerNotAccessibleError, WorkspacePermissionError):
            self.repository.finish(job_id, error_code="permission_revoked",
                                   message=ERROR_MESSAGES["permission_revoked"])
        except (
            MarketplaceAccountNotFoundError,
            OrdersAccountUnavailableError,
            OrdersValidationError,
        ):
            self.repository.finish(job_id, error_code="account_unavailable",
                                   message=ERROR_MESSAGES["account_unavailable"])
        except OrdersImportBusyError:
            self.repository.finish(job_id, error_code="account_busy",
                                   message=ERROR_MESSAGES["account_busy"])
        except OrdersAmbiguousMatchError:
            self.repository.finish(job_id, error_code="invalid_response",
                                   message=ERROR_MESSAGES["invalid_response"])
        except CredentialStorageUnavailableError:
            self.repository.finish(job_id, error_code="credentials_unavailable",
                                   message=ERROR_MESSAGES["credentials_unavailable"])
        except TimeoutError:
            self.repository.finish(job_id, error_code="timeout", message=ERROR_MESSAGES["timeout"])
        except Exception as exc:
            code = getattr(exc, "code", "sync_failed")
            code = code if isinstance(code, str) and code in ERROR_MESSAGES else "sync_failed"
            self.repository.finish(job_id, error_code=code, message=ERROR_MESSAGES[code])
