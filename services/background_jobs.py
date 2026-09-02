from __future__ import annotations

import json
import os
import socket
import threading
import uuid
from datetime import date, datetime, timezone
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
    job_id = uuid.uuid4().hex
    payload = _json_safe(request.payload)
    with connect() as con:
        con.execute(
            """INSERT INTO background_jobs(
                id,kind,tenant_id,seller_id,status,payload_json,created_at
            ) VALUES(?,?,?,?,?,?,?)""",
            (
                job_id,
                str(request.kind or "").strip(),
                str(request.tenant_id or ""),
                request.seller_id,
                "queued",
                json_text(payload),
                now_iso(),
            ),
        )
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


def update_job_progress(job_id: str, current: int, total: int, message: str = "") -> None:
    total_i = max(0, int(total or 0))
    current_i = max(0, int(current or 0))
    pct = min(100.0, (current_i / total_i * 100.0) if total_i else 0.0)
    with connect() as con:
        con.execute(
            """UPDATE background_jobs SET progress_current=?,progress_total=?,progress_pct=?,
               message=?,heartbeat_at=? WHERE id=? AND status='running'""",
            (current_i, total_i, pct, str(message or ""), now_iso(), job_id),
        )


def complete_job(job_id: str, result: Any = None, message: str = "Completato") -> None:
    with connect() as con:
        con.execute(
            """UPDATE background_jobs SET status='done',progress_pct=100,message=?,
               result_json=?,heartbeat_at=?,finished_at=? WHERE id=?""",
            (str(message or "Completato"), json_text(_json_safe(result or {})), now_iso(), now_iso(), job_id),
        )


def fail_job(job_id: str, error: BaseException | str) -> None:
    with connect() as con:
        con.execute(
            """UPDATE background_jobs SET status='error',error=?,message='Errore',
               heartbeat_at=?,finished_at=? WHERE id=?""",
            (str(error), now_iso(), now_iso(), job_id),
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
        update_job_progress(str(job["id"]), done, total, label)

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
        update_job_progress(str(job["id"]), done, total, f"Buy Box: {kind}")

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


def execute_claimed_job(job: dict[str, Any]) -> dict[str, Any]:
    kind = str(job.get("kind") or "")
    handlers: dict[str, Callable[[dict[str, Any]], dict[str, Any]]] = {
        "orders.kaufland.sync": _run_orders_kaufland,
        "buybox.kaufland.quick": _run_buybox_kaufland_quick,
    }
    handler = handlers.get(kind)
    if handler is None:
        raise RuntimeError(f"Job non supportato dal worker v305: {kind}")
    return handler(job)


def run_job(job_id: str) -> bool:
    job = claim_job(job_id)
    if not job:
        return False
    try:
        result = execute_claimed_job(job)
        complete_job(job_id, result)
    except Exception as error:
        fail_job(job_id, error)
    return True


def run_next_job(*, kind_prefix: str = "") -> bool:
    job = claim_next_job(kind_prefix=kind_prefix)
    if not job:
        return False
    try:
        result = execute_claimed_job(job)
        complete_job(str(job["id"]), result)
    except Exception as error:
        fail_job(str(job["id"]), error)
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
