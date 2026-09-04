from __future__ import annotations

import json
import hashlib
import os
import socket
import threading
import uuid
import logging
from datetime import date, datetime, timezone, timedelta
from typing import Any, Callable

from marketplace_core.contracts import JobReceipt, JobRequest
from services.database_config import database_engine
from services.db import connect, json_text, now_iso, row, rows

_SCHEMA_LOCK = threading.RLock()
_SCHEMA_READY = False
_LOCAL_LOCK = threading.RLock()
_LOCAL_THREADS: dict[str, threading.Thread] = {}


def _json_safe(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, dict):
        return {str(k): _json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_json_safe(v) for v in value]
    item = getattr(value, "item", None)
    if callable(item):
        try:
            return _json_safe(item())
        except Exception:
            pass
    return str(value)


def ensure_job_schema() -> None:
    global _SCHEMA_READY
    if _SCHEMA_READY:
        return
    with _SCHEMA_LOCK:
        if _SCHEMA_READY:
            return
        with connect() as con:
            con.executescript(
                """
                CREATE TABLE IF NOT EXISTS background_jobs (
                    id TEXT PRIMARY KEY,
                    kind TEXT NOT NULL,
                    tenant_id TEXT NOT NULL DEFAULT '',
                    seller_id INTEGER,
                    status TEXT NOT NULL DEFAULT 'queued',
                    progress_current INTEGER NOT NULL DEFAULT 0,
                    progress_total INTEGER NOT NULL DEFAULT 0,
                    progress_pct REAL NOT NULL DEFAULT 0,
                    message TEXT NOT NULL DEFAULT '',
                    payload_json TEXT NOT NULL DEFAULT '{}',
                    result_json TEXT NOT NULL DEFAULT '{}',
                    error TEXT NOT NULL DEFAULT '',
                    worker_id TEXT NOT NULL DEFAULT '',
                    attempts INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    started_at TEXT NOT NULL DEFAULT '',
                    heartbeat_at TEXT NOT NULL DEFAULT '',
                    finished_at TEXT NOT NULL DEFAULT ''
                );
                CREATE INDEX IF NOT EXISTS idx_background_jobs_queue
                ON background_jobs(status,created_at);
                CREATE INDEX IF NOT EXISTS idx_background_jobs_seller
                ON background_jobs(seller_id,created_at);
                CREATE INDEX IF NOT EXISTS idx_background_jobs_kind
                ON background_jobs(kind,status,created_at);
                """
            )
        _SCHEMA_READY = True


def enqueue_job(request: JobRequest) -> JobReceipt:
    ensure_job_schema()
    from services.tenant_db import current_tenant_id, tenant_id_for_seller
    tenant_id = int(str(request.tenant_id or "0") or 0)
    if tenant_id <= 0:
        tenant_id = current_tenant_id()
    if tenant_id <= 0 and request.seller_id:
        tenant_id = tenant_id_for_seller(int(request.seller_id))
    if tenant_id <= 0:
        raise RuntimeError("Impossibile accodare un job SaaS senza tenant_id.")
    # v318: background work is governed by the same subscription rules as HTTP APIs.
    from services.entitlements import (
        assert_capacity, job_feature, record_usage, require_tenant_feature, tenant_resource_usage, limit_for,
    )
    feature = job_feature(request.kind)
    if feature:
        require_tenant_feature(tenant_id, feature)
    monthly_limit = limit_for(tenant_id, "monthly_background_jobs")
    job_id = uuid.uuid4().hex
    kind = str(request.kind or "").strip()
    payload = _json_safe(request.payload)
    canonical_payload = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    deduplicate = kind in {"orders.kaufland.sync", "orders.worten.sync"}
    with connect() as con:
        if deduplicate:
            # Transaction locks work across API processes. No in-memory lock or
            # schema migration is needed, and existing queued/running jobs count.
            if database_engine() == "postgresql":
                key = json.dumps([tenant_id, request.seller_id, kind, canonical_payload])
                lock_id = int.from_bytes(hashlib.sha256(key.encode()).digest()[:8], "big", signed=True)
                con.execute("SELECT pg_advisory_xact_lock(?)", (lock_id,))
            else:
                con.execute("BEGIN IMMEDIATE")
            active = con.execute(
                """SELECT id,status,payload_json FROM background_jobs
                   WHERE tenant_id=? AND kind=? AND (seller_id=? OR (seller_id IS NULL AND CAST(? AS BIGINT) IS NULL))
                   AND status IN ('queued','running') ORDER BY created_at,id""",
                (str(tenant_id), kind, request.seller_id, request.seller_id),
            ).fetchall()
            for item in active:
                try:
                    existing = json.dumps(json.loads(item["payload_json"]), sort_keys=True, separators=(",", ":"))
                except (TypeError, ValueError):
                    continue
                if existing == canonical_payload:
                    return JobReceipt(job_id=str(item["id"]), status=str(item["status"]))
        # A reused job must neither consume quota nor be rejected because the
        # original request already used the last available monthly slot.
        if monthly_limit is not None:
            usage = tenant_resource_usage(tenant_id)
            assert_capacity(tenant_id, "monthly_background_jobs", usage.get("background_jobs", 0), increment=1, label="job mensili")
        con.execute(
            """INSERT INTO background_jobs(
                id,kind,tenant_id,seller_id,status,payload_json,created_at
            ) VALUES(?,?,?,?,?,?,?)""",
            (
                job_id,
                kind,
                str(tenant_id),
                request.seller_id,
                "queued",
                json_text(payload),
                now_iso(),
            ),
        )
    record_usage(tenant_id, "background_jobs", 1)
    return JobReceipt(job_id=job_id, status="queued")


def _decode(item: dict[str, Any] | None) -> dict[str, Any] | None:
    if not item:
        return None
    result = dict(item)
    for source, target in (("payload_json", "payload"), ("result_json", "result")):
        try:
            result[target] = json.loads(result.get(source) or "{}")
        except (TypeError, ValueError, json.JSONDecodeError):
            result[target] = {}
    return result


def get_job(job_id: str) -> dict[str, Any] | None:
    ensure_job_schema()
    return _decode(row("SELECT * FROM background_jobs WHERE id=?", (job_id,)))


def recent_jobs(*, seller_id: int | None = None, kind_prefix: str = "", limit: int = 20) -> list[dict[str, Any]]:
    ensure_job_schema()
    where = []
    params: list[Any] = []
    if seller_id is not None:
        where.append("seller_id=?")
        params.append(int(seller_id))
    if kind_prefix:
        where.append("kind LIKE ?")
        params.append(f"{kind_prefix}%")
    sql = "SELECT * FROM background_jobs"
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY created_at DESC LIMIT ?"
    params.append(max(1, int(limit)))
    return [_decode(item) or {} for item in rows(sql, tuple(params))]


def _claim_guard(claim: dict[str, Any] | None) -> tuple[str, tuple]:
    if claim is None:
        return "", ()
    return " AND worker_id=? AND attempts=? AND status='running'", (
        str(claim.get("worker_id") or ""), int(claim.get("attempts") or 0),
    )


def update_job_progress(job_id: str, current: int, total: int, message: str = "", *, claim: dict | None = None) -> None:
    total_i = max(0, int(total or 0))
    current_i = max(0, int(current or 0))
    pct = min(100.0, (current_i / total_i * 100.0) if total_i else 0.0)
    guard, args = _claim_guard(claim)
    with connect() as con:
        con.execute(
            """UPDATE background_jobs SET progress_current=?,progress_total=?,progress_pct=?,
               message=?,heartbeat_at=? WHERE id=? AND status='running'""" + guard,
            (current_i, total_i, pct, str(message or ""), now_iso(), job_id, *args),
        )


def complete_job(job_id: str, result: Any = None, message: str = "Completato", *, claim: dict | None = None) -> None:
    guard, args = _claim_guard(claim)
    with connect() as con:
        con.execute(
            """UPDATE background_jobs SET status='done',progress_pct=100,message=?,
               result_json=?,heartbeat_at=?,finished_at=? WHERE id=?""" + guard,
            (str(message or "Completato"), json_text(_json_safe(result or {})), now_iso(), now_iso(), job_id, *args),
        )


def fail_job(job_id: str, error: BaseException | str, *, claim: dict | None = None) -> None:
    guard, args = _claim_guard(claim)
    with connect() as con:
        con.execute(
            """UPDATE background_jobs SET status='error',error=?,message='Errore',
               heartbeat_at=?,finished_at=? WHERE id=?""" + guard,
            (str(error), now_iso(), now_iso(), job_id, *args),
        )


def cancel_job(job_id: str) -> bool:
    with connect() as con:
        cur = con.execute(
            "UPDATE background_jobs SET status='cancelled',finished_at=? WHERE id=? AND status='queued'",
            (now_iso(), job_id),
        )
        return bool(getattr(cur, "rowcount", 0))


def _worker_name() -> str:
    return f"{socket.gethostname()}:{os.getpid()}:{threading.get_ident()}"


def claim_job(job_id: str, *, worker_id: str | None = None) -> dict[str, Any] | None:
    """Atomically claim a specific queued job."""
    ensure_job_schema()
    worker = worker_id or _worker_name()
    with connect() as con:
        cur = con.execute(
            """UPDATE background_jobs SET status='running',worker_id=?,attempts=attempts+1,
               started_at=?,heartbeat_at=? WHERE id=? AND status='queued'""",
            (worker, now_iso(), now_iso(), job_id),
        )
        if int(getattr(cur, "rowcount", 0) or 0) <= 0:
            return None
        item = con.execute("SELECT * FROM background_jobs WHERE id=?", (job_id,)).fetchone()
        return _decode(dict(item) if item else None)


def claim_next_job(*, worker_id: str | None = None, kind_prefix: str = "") -> dict[str, Any] | None:
    ensure_job_schema()
    worker = worker_id or _worker_name()
    # PostgreSQL path uses SKIP LOCKED so multiple SaaS workers can safely share the queue.
    if database_engine() == "postgresql":
        with connect() as con:
            params: list[Any] = []
            extra = ""
            if kind_prefix:
                extra = " AND kind LIKE ?"
                params.append(f"{kind_prefix}%")
            item = con.execute(
                f"""SELECT * FROM background_jobs WHERE status='queued'{extra}
                    ORDER BY created_at LIMIT 1 FOR UPDATE SKIP LOCKED""",
                tuple(params),
            ).fetchone()
            if not item:
                return None
            job_id = str(item["id"])
            con.execute(
                """UPDATE background_jobs SET status='running',worker_id=?,attempts=attempts+1,
                   started_at=?,heartbeat_at=? WHERE id=?""",
                (worker, now_iso(), now_iso(), job_id),
            )
            claimed = con.execute("SELECT * FROM background_jobs WHERE id=?", (job_id,)).fetchone()
            return _decode(dict(claimed) if claimed else None)

    # SQLite/dev fallback: optimistic specific claim. Dedicated SaaS deployment is PostgreSQL.
    candidates = rows(
        "SELECT id FROM background_jobs WHERE status='queued' "
        + ("AND kind LIKE ? " if kind_prefix else "")
        + "ORDER BY created_at LIMIT 5",
        (f"{kind_prefix}%",) if kind_prefix else (),
    )
    for candidate in candidates:
        claimed = claim_job(str(candidate["id"]), worker_id=worker)
        if claimed:
            return claimed
    return None


def _payload_date(payload: dict[str, Any], key: str) -> date:
    value = str(payload.get(key) or "").strip()
    if not value:
        raise RuntimeError(f"Parametro job mancante: {key}")
    try:
        return date.fromisoformat(value[:10])
    except ValueError as exc:
        raise RuntimeError(f"Data job non valida per {key}: {value}") from exc


def _account_credentials(seller_id: int, account_id: int) -> tuple[dict[str, Any], dict[str, Any]]:
    from services.security import decrypt_dict
    account = row(
        "SELECT * FROM marketplace_accounts WHERE id=? AND seller_id=? AND active=1",
        (account_id, seller_id),
    )
    if not account:
        raise RuntimeError("Account marketplace non trovato o non autorizzato per il Seller.")
    return account, decrypt_dict(account["credentials_encrypted"])


def _run_orders_kaufland(job: dict[str, Any]) -> dict[str, Any]:
    from marketplace_core.orders import OrderScope, OrdersCore
    from services.kaufland import KauflandClient

    payload = job.get("payload") or {}
    seller_id = int(job.get("seller_id") or 0)
    account_id = int(payload.get("account_id") or 0)
    environment = str(payload.get("environment") or "live")
    _account, credentials = _account_credentials(seller_id, account_id)
    client = KauflandClient(
        credentials.get("client_key", ""),
        credentials.get("secret_key", ""),
        environment == "test",
    )
    core = OrdersCore()
    scope = OrderScope(seller_id, account_id, "kaufland", environment)

    def progress(done: int, total: int, label: str) -> None:
        update_job_progress(str(job["id"]), done, total, label, claim=job)

    return core.sync_kaufland(
        scope,
        client,
        maximum=payload.get("maximum", 1000),
        include_tracking_details=bool(payload.get("include_tracking_details", True)),
        progress=progress,
    )


def _run_buybox_kaufland_quick(job: dict[str, Any]) -> dict[str, Any]:
    from marketplace_core.buybox import BuyBoxCore, BuyBoxScope
    from services.kaufland import KauflandClient
    from services.db import now_iso

    payload = job.get("payload") or {}
    seller_id = int(job.get("seller_id") or 0)
    account_id = int(payload.get("account_id") or 0)
    environment = str(payload.get("environment") or "live")
    tasks = list(payload.get("tasks") or [])
    pseudonyms = list(payload.get("own_seller_pseudonyms") or [])
    _account, credentials = _account_credentials(seller_id, account_id)
    client = KauflandClient(
        credentials.get("client_key", ""),
        credentials.get("secret_key", ""),
        environment == "test",
    )
    client.before_request = None
    client.requests_per_second = max(float(client.requests_per_second or 1), 45.0)
    core = BuyBoxCore()
    scope = BuyBoxScope(seller_id, account_id, "kaufland", environment)
    previous = core.kaufland_previous_checks(scope, tasks)

    def progress(done: int, total: int, outcome: dict[str, Any]) -> None:
        kind = str(outcome.get("kind") or "")
        update_job_progress(str(job["id"]), done, total, f"Buy Box: {kind}", claim=job)

    outcomes = core.run_kaufland_quick_batch(
        client,
        tasks,
        previous_by_offer=previous,
        own_seller_pseudonyms=pseudonyms,
        checked_at=now_iso(),
        max_workers=int(payload.get("max_workers") or 20),
        on_progress=progress,
    )
    successful = [item["result"] for item in outcomes if item.get("kind") == "ok"]
    saved = core.persist_kaufland_checks(scope, successful)
    needs_full = sum(1 for item in outcomes if item.get("kind") == "needs_full")
    errors = sum(1 for item in outcomes if item.get("kind") == "error")
    return {
        "total": len(tasks),
        "successful": len(successful),
        "saved": saved,
        "needs_full": needs_full,
        "errors": errors,
    }


def _run_orders_worten(job: dict[str, Any]) -> dict[str, Any]:
    from marketplace_core.orders import OrderScope, OrdersCore

    payload = job.get("payload") or {}
    seller_id = int(job.get("seller_id") or 0)
    account_id = int(payload.get("account_id") or 0)
    environment = str(payload.get("environment") or "live")
    date_from = _payload_date(payload, "date_from")
    date_to = _payload_date(payload, "date_to")
    _account, credentials = _account_credentials(seller_id, account_id)
    core = OrdersCore()
    scope = OrderScope(seller_id, account_id, "worten", environment)

    def progress(done: int, total: int, label: str) -> None:
        update_job_progress(str(job["id"]), done, total, label, claim=job)

    saved = core.sync_normalized(
        scope, credentials, date_from=date_from, date_to=date_to, progress=progress
    )
    return {
        "marketplace": "worten",
        "saved": int(saved),
        "date_from": date_from.isoformat(),
        "date_to": date_to.isoformat(),
    }


def _run_accounting_sync(job: dict[str, Any]) -> dict[str, Any]:
    from marketplace_core.accounting import AccountingCore, AccountingPeriod, AccountingScope

    payload = job.get("payload") or {}
    seller_id = int(job.get("seller_id") or 0)
    account_id = int(payload.get("account_id") or 0)
    marketplace = str(payload.get("marketplace") or "").strip().lower()
    date_from = _payload_date(payload, "date_from")
    date_to = _payload_date(payload, "date_to")
    _account, credentials = _account_credentials(seller_id, account_id)
    core = AccountingCore()
    scope = AccountingScope(seller_id, account_id, marketplace)

    def progress(done: int, total: int, label: str) -> None:
        update_job_progress(str(job["id"]), done, total, label, claim=job)

    return core.synchronize(
        scope, credentials, AccountingPeriod(date_from, date_to),
        full=bool(payload.get("full")), progress=progress,
    )


def _run_accounting_costs(job: dict[str, Any]) -> dict[str, Any]:
    from marketplace_core.accounting import AccountingCore, AccountingPeriod, AccountingScope

    payload = job.get("payload") or {}
    seller_id = int(job.get("seller_id") or 0)
    account_id = int(payload.get("account_id") or 0)
    marketplace = str(payload.get("marketplace") or "").strip().lower()
    date_from = _payload_date(payload, "date_from")
    date_to = _payload_date(payload, "date_to")
    core = AccountingCore()
    scope = AccountingScope(seller_id, account_id, marketplace)

    def progress(done: int, total: int, label: str) -> None:
        update_job_progress(str(job["id"]), done, total, label, claim=job)

    return core.refresh_costs(
        scope, AccountingPeriod(date_from, date_to), progress=progress
    )



def _run_packlink_shipments_sync(job: dict[str, Any]) -> dict[str, Any]:
    from marketplace_core.packlink import PacklinkCore, PacklinkScope

    seller_id = int(job.get("seller_id") or 0)
    core = PacklinkCore()

    def progress(done: int, total: int, label: str) -> None:
        update_job_progress(str(job["id"]), done, total, label, claim=job)

    return core.synchronize_shipments(PacklinkScope(seller_id), progress=progress)


def _run_tracking_documents_analyze(job: dict[str, Any]) -> dict[str, Any]:
    from marketplace_core.accounting import AccountingPeriod
    from marketplace_core.tracking import TrackingCore, TrackingScope

    payload = job.get("payload") or {}
    seller_id = int(job.get("seller_id") or 0)
    account_id = int(payload.get("account_id") or 0)
    marketplace = str(payload.get("marketplace") or "").strip().lower()
    period = AccountingPeriod(
        _payload_date(payload, "date_from"),
        _payload_date(payload, "date_to"),
    )
    core = TrackingCore()

    def progress(done: int, total: int, label: str) -> None:
        update_job_progress(str(job["id"]), done, total, label, claim=job)

    return core.analyze_archived_documents(
        TrackingScope(seller_id, account_id, marketplace),
        period,
        file_ids=payload.get("file_ids") or [],
        urls=payload.get("urls") or [],
        supplier_choice=str(payload.get("supplier_choice") or ""),
        progress=progress,
    )


def _run_packlink_quotes_mass(job: dict[str, Any]) -> dict[str, Any]:
    from marketplace_core.packlink import PacklinkCore, PacklinkScope

    payload = job.get("payload") or {}
    seller_id = int(job.get("seller_id") or 0)
    core = PacklinkCore()

    def progress(done: int, total: int, item: dict[str, Any]) -> None:
        order_id = str(item.get("order_id") or "")
        label = f"Tariffe Packlink: {order_id}" if order_id else "Tariffe Packlink"
        update_job_progress(str(job["id"]), done, total, label, claim=job)

    return core.quote_many(
        PacklinkScope(seller_id),
        list(payload.get("tasks") or []),
        origin_country=str(payload.get("origin_country") or ""),
        origin_zip=str(payload.get("origin_zip") or ""),
        source=str(payload.get("source") or "PRO"),
        max_workers=int(payload.get("max_workers") or 6),
        progress=progress,
    )


def _run_packlink_drafts_mass(job: dict[str, Any]) -> dict[str, Any]:
    from marketplace_core.packlink import PacklinkCore, PacklinkScope

    payload = job.get("payload") or {}
    seller_id = int(job.get("seller_id") or 0)
    core = PacklinkCore()

    def progress(done: int, total: int, item: dict[str, Any]) -> None:
        order_id = str(item.get("order_id") or "")
        status = str(item.get("status") or "")
        update_job_progress(
            str(job["id"]), done, total,
            f"Spedizioni Packlink: {order_id} · {status}" if order_id else "Spedizioni Packlink",
            claim=job,
        )

    return core.create_drafts_many(
        PacklinkScope(seller_id),
        list(payload.get("tasks") or []),
        sender=dict(payload.get("sender") or {}),
        warehouse_id=str(payload.get("warehouse_id") or ""),
        job_id=str(job.get("id") or ""),
        max_workers=int(payload.get("max_workers") or 2),
        progress=progress,
    )



def _run_catalog_materialize(job: dict[str, Any]) -> dict[str, Any]:
    from marketplace_core.catalogs import CatalogCore

    payload = job.get("payload") or {}
    seller_id = int(job.get("seller_id") or 0)
    price_list_id = int(payload.get("price_list_id") or 0)
    item = row(
        """SELECT pl.id,pl.local_path FROM price_lists pl
           WHERE pl.id=? AND (pl.owner_seller_id=? OR pl.visibility='global' OR EXISTS(
               SELECT 1 FROM price_list_access pla
               WHERE pla.price_list_id=pl.id AND pla.seller_id=?
           ))""",
        (price_list_id, seller_id, seller_id),
    )
    if not item:
        raise RuntimeError("Listino non trovato o non autorizzato per questo Seller.")
    source_path = str(item.get("local_path") or "")
    if not source_path:
        raise RuntimeError("Il listino non dispone di un file locale da normalizzare.")
    core = CatalogCore()

    def progress(done: int, total: int, label: str) -> None:
        update_job_progress(str(job["id"]), done, total, label, claim=job)

    return core.materialize(price_list_id, source_path, progress=progress)

def execute_claimed_job(job: dict[str, Any]) -> dict[str, Any]:
    kind = str(job.get("kind") or "")
    handlers: dict[str, Callable[[dict[str, Any]], dict[str, Any]]] = {
        "orders.kaufland.sync": _run_orders_kaufland,
        "orders.worten.sync": _run_orders_worten,
        "buybox.kaufland.quick": _run_buybox_kaufland_quick,
        "accounting.orders.sync": _run_accounting_sync,
        "accounting.costs.refresh": _run_accounting_costs,
        "packlink.shipments.sync": _run_packlink_shipments_sync,
        "packlink.quotes.mass": _run_packlink_quotes_mass,
        "packlink.drafts.mass": _run_packlink_drafts_mass,
        "tracking.documents.analyze": _run_tracking_documents_analyze,
        "catalog.materialize": _run_catalog_materialize,
    }
    handler = handlers.get(kind)
    if handler is None:
        raise RuntimeError(f"Job non supportato dal worker v316: {kind}")
    from services.tenant_db import assert_seller_in_tenant, tenant_database_scope
    tenant_id = int(str(job.get("tenant_id") or "0") or 0)
    if tenant_id <= 0:
        raise RuntimeError("Job legacy senza tenant_id: migrazione richiesta.")
    seller_id = int(job.get("seller_id") or 0)
    if seller_id > 0:
        assert_seller_in_tenant(seller_id, tenant_id)
    # Re-check at execution time too: a plan can be downgraded/suspended after
    # the job was queued but before a worker claims it.
    from services.entitlements import job_feature, require_tenant_feature
    feature = job_feature(kind)
    with tenant_database_scope(tenant_id):
        # Subscription/usage tables are RLS-protected too: check the plan under
        # the job tenant, before running any marketplace operation.
        if feature:
            require_tenant_feature(tenant_id, feature)
        return handler(job)


def heartbeat_job(job: dict[str, Any]) -> bool:
    guard, args = _claim_guard(job)
    with connect() as con:
        cursor = con.execute(
            "UPDATE background_jobs SET heartbeat_at=? WHERE id=?" + guard,
            (now_iso(), str(job["id"]), *args),
        )
        return bool(cursor.rowcount)


def recover_stale_jobs(*, stale_seconds: int = 300, max_attempts: int = 3, kind_prefix: str = "") -> dict[str, int]:
    """Retry only idempotent imports; uncertain external actions need review."""
    ensure_job_schema()
    cutoff = (datetime.now(timezone.utc) - timedelta(seconds=max(60, stale_seconds))).isoformat(timespec="seconds")
    safe_retry = {"orders.kaufland.sync", "orders.worten.sync", "catalog.materialize"}
    result = {"requeued": 0, "review": 0}
    with connect() as con:
        if database_engine() != "postgresql":
            con.execute("BEGIN IMMEDIATE")
        query = """SELECT * FROM background_jobs WHERE status='running'
            AND COALESCE(NULLIF(heartbeat_at,''),NULLIF(started_at,''),created_at)<?"""
        args: list[Any] = [cutoff]
        if kind_prefix:
            query += " AND kind LIKE ?"
            args.append(kind_prefix + "%")
        query += " ORDER BY created_at LIMIT 100"
        if database_engine() == "postgresql":
            query += " FOR UPDATE SKIP LOCKED"
        for raw in con.execute(query, tuple(args)).fetchall():
            job = dict(raw)
            retry = job["kind"] in safe_retry and int(job.get("attempts") or 0) < max(1, max_attempts)
            if retry:
                con.execute(
                    """UPDATE background_jobs SET status='queued',worker_id='',started_at='',
                       heartbeat_at='',finished_at='',progress_current=0,progress_total=0,progress_pct=0,
                       error='',message='Riaccodato dopo interruzione del worker' WHERE id=?""",
                    (job["id"],),
                )
                result["requeued"] += 1
            else:
                con.execute(
                    """UPDATE background_jobs SET status='error',finished_at=?,
                       message='Verifica richiesta dopo interruzione',
                       error='Worker non più attivo: verificare l’esito prima di ripetere il lavoro.' WHERE id=?""",
                    (now_iso(), job["id"]),
                )
                result["review"] += 1
    return result


def _run_claimed(job: dict[str, Any], *, heartbeat_interval: float = 30.0) -> None:
    stop = threading.Event()
    def pulse():
        while not stop.wait(heartbeat_interval):
            try:
                if not heartbeat_job(job):
                    return
            except Exception:
                logging.getLogger(__name__).warning("Job heartbeat unavailable: %s", job["id"])
    thread = threading.Thread(target=pulse, daemon=True, name=f"mh-heartbeat-{str(job['id'])[:8]}")
    thread.start()
    try:
        result = execute_claimed_job(job)
        complete_job(str(job["id"]), result, claim=job)
    except Exception as error:
        fail_job(str(job["id"]), error, claim=job)
    finally:
        stop.set()
        thread.join(timeout=1)


def run_job(job_id: str) -> bool:
    job = claim_job(job_id)
    if not job:
        return False
    _run_claimed(job)
    return True


def run_next_job(*, kind_prefix: str = "") -> bool:
    recover_stale_jobs(kind_prefix=kind_prefix)
    job = claim_next_job(kind_prefix=kind_prefix)
    if not job:
        return False
    _run_claimed(job)
    return True


def start_job_in_background(job_id: str) -> bool:
    """Start a daemon worker in the current web process without blocking Streamlit.

    This is the transition runtime. The queue is already persistent, so replacing
    this daemon thread with a dedicated Render worker requires no UI changes.
    """
    with _LOCAL_LOCK:
        current = _LOCAL_THREADS.get(job_id)
        if current and current.is_alive():
            return False
        thread = threading.Thread(
            target=run_job,
            args=(job_id,),
            daemon=True,
            name=f"mh-job-{job_id[:8]}",
        )
        _LOCAL_THREADS[job_id] = thread
        thread.start()
        return True
