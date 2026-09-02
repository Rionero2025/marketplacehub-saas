from __future__ import annotations

import json
import threading
from typing import Any, Mapping, Sequence

from services.db import connect, json_text, now_iso, row
from services.packlink import clean_text, packlink_package_signature

_SCHEMA_LOCK = threading.RLock()
_SCHEMA_READY = False


def ensure_schema() -> None:
    """Persistent cache + idempotency guards for Packlink mass operations."""
    global _SCHEMA_READY
    if _SCHEMA_READY:
        return
    with _SCHEMA_LOCK:
        if _SCHEMA_READY:
            return
        with connect() as con:
            con.executescript(
                """
                CREATE TABLE IF NOT EXISTS packlink_mass_quote_cache (
                    seller_id INTEGER NOT NULL,
                    order_key TEXT NOT NULL,
                    package_signature TEXT NOT NULL,
                    origin_country TEXT NOT NULL DEFAULT '',
                    origin_zip TEXT NOT NULL DEFAULT '',
                    destination_country TEXT NOT NULL DEFAULT '',
                    destination_zip TEXT NOT NULL DEFAULT '',
                    package_json TEXT NOT NULL DEFAULT '{}',
                    services_json TEXT NOT NULL DEFAULT '[]',
                    error TEXT NOT NULL DEFAULT '',
                    quoted_at TEXT NOT NULL,
                    PRIMARY KEY(seller_id,order_key,package_signature)
                );
                CREATE INDEX IF NOT EXISTS idx_packlink_mass_quote_seller
                ON packlink_mass_quote_cache(seller_id,quoted_at);

                CREATE TABLE IF NOT EXISTS packlink_draft_guards (
                    seller_id INTEGER NOT NULL,
                    marketplace_account_id INTEGER NOT NULL,
                    marketplace TEXT NOT NULL,
                    order_id TEXT NOT NULL,
                    operation_key TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'creating',
                    job_id TEXT NOT NULL DEFAULT '',
                    shipment_reference TEXT NOT NULL DEFAULT '',
                    error TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY(
                        seller_id,marketplace_account_id,marketplace,order_id,operation_key
                    )
                );
                CREATE INDEX IF NOT EXISTS idx_packlink_draft_guards_job
                ON packlink_draft_guards(job_id,status,updated_at);
                """
            )
        _SCHEMA_READY = True


def save_quote_result(
    seller_id: int,
    *,
    order_key: str,
    package: Mapping[str, Any],
    origin_country: str,
    origin_zip: str,
    destination_country: str,
    destination_zip: str,
    services: Sequence[Mapping[str, Any]] | None = None,
    error: str = "",
) -> None:
    ensure_schema()
    signature = packlink_package_signature(package)
    with connect() as con:
        con.execute(
            """INSERT INTO packlink_mass_quote_cache(
                seller_id,order_key,package_signature,origin_country,origin_zip,
                destination_country,destination_zip,package_json,services_json,error,quoted_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(seller_id,order_key,package_signature) DO UPDATE SET
                origin_country=excluded.origin_country,origin_zip=excluded.origin_zip,
                destination_country=excluded.destination_country,destination_zip=excluded.destination_zip,
                package_json=excluded.package_json,services_json=excluded.services_json,
                error=excluded.error,quoted_at=excluded.quoted_at""",
            (
                int(seller_id), clean_text(order_key), signature,
                clean_text(origin_country).upper(), clean_text(origin_zip),
                clean_text(destination_country).upper(), clean_text(destination_zip),
                json_text(dict(package)), json_text([dict(item) for item in (services or [])]),
                clean_text(error), now_iso(),
            ),
        )


def quote_result(seller_id: int, *, order_key: str, package: Mapping[str, Any]) -> dict[str, Any] | None:
    ensure_schema()
    signature = packlink_package_signature(package)
    item = row(
        """SELECT * FROM packlink_mass_quote_cache
        WHERE seller_id=? AND order_key=? AND package_signature=?""",
        (int(seller_id), clean_text(order_key), signature),
    )
    if not item:
        return None
    result = dict(item)
    for source, target, fallback in (
        ("package_json", "package", {}),
        ("services_json", "services", []),
    ):
        try:
            result[target] = json.loads(result.get(source) or json.dumps(fallback))
        except (TypeError, ValueError, json.JSONDecodeError):
            result[target] = fallback
    return result


def _existing_draft_reference(
    seller_id: int, marketplace_account_id: int, marketplace: str, order_id: str
) -> str:
    item = row(
        """SELECT shipment_reference FROM packlink_order_drafts
        WHERE seller_id=? AND marketplace_account_id=? AND marketplace=? AND order_id=?
        LIMIT 1""",
        (
            int(seller_id), int(marketplace_account_id), clean_text(marketplace).lower(),
            clean_text(order_id),
        ),
    )
    return clean_text(item.get("shipment_reference")) if item else ""


def begin_draft_attempt(
    *,
    seller_id: int,
    marketplace_account_id: int,
    marketplace: str,
    order_id: str,
    job_id: str,
    forced: bool,
) -> dict[str, Any]:
    """Acquire an idempotency guard immediately before POST /shipments.

    A non-forced order can never be posted twice automatically. A forced mass job
    receives a job-scoped operation key, so rerunning the same worker record is
    also idempotent. If a POST raises after the guard was acquired we later mark
    the attempt ``uncertain`` and intentionally do not auto-retry it: Packlink may
    have accepted the request even when the HTTP response was lost.
    """
    ensure_schema()
    account_id = int(marketplace_account_id)
    market = clean_text(marketplace).lower()
    oid = clean_text(order_id)
    if not forced:
        reference = _existing_draft_reference(seller_id, account_id, market, oid)
        if reference:
            return {
                "allowed": False, "status": "created", "shipment_reference": reference,
                "reason": "Ordine già organizzato su Packlink",
            }
    operation_key = f"forced:{clean_text(job_id)}" if forced else "normal"
    timestamp = now_iso()
    with connect() as con:
        con.execute(
            """INSERT INTO packlink_draft_guards(
                seller_id,marketplace_account_id,marketplace,order_id,operation_key,
                status,job_id,created_at,updated_at
            ) VALUES(?,?,?,?,?,'creating',?,?,?)
            ON CONFLICT(seller_id,marketplace_account_id,marketplace,order_id,operation_key)
            DO NOTHING""",
            (
                int(seller_id), account_id, market, oid, operation_key,
                clean_text(job_id), timestamp, timestamp,
            ),
        )
        item = con.execute(
            """SELECT * FROM packlink_draft_guards
            WHERE seller_id=? AND marketplace_account_id=? AND marketplace=?
              AND order_id=? AND operation_key=?""",
            (int(seller_id), account_id, market, oid, operation_key),
        ).fetchone()
    current = dict(item) if item else {}
    # Only the job that inserted the guard may perform the POST. A worker retry of
    # the same job sees the existing 'creating/uncertain/created' row and stops.
    allowed = (
        clean_text(current.get("status")) == "creating"
        and clean_text(current.get("job_id")) == clean_text(job_id)
        and clean_text(current.get("created_at")) == timestamp
    )
    return {
        "allowed": bool(allowed),
        "status": clean_text(current.get("status")),
        "shipment_reference": clean_text(current.get("shipment_reference")),
        "reason": (
            "Tentativo già registrato: retry automatico bloccato per evitare duplicati"
            if not allowed else ""
        ),
        "operation_key": operation_key,
    }


def finish_draft_attempt(
    *,
    seller_id: int,
    marketplace_account_id: int,
    marketplace: str,
    order_id: str,
    operation_key: str,
    shipment_reference: str,
) -> None:
    ensure_schema()
    with connect() as con:
        con.execute(
            """UPDATE packlink_draft_guards SET status='created',shipment_reference=?,
               error='',updated_at=? WHERE seller_id=? AND marketplace_account_id=?
               AND marketplace=? AND order_id=? AND operation_key=?""",
            (
                clean_text(shipment_reference), now_iso(), int(seller_id),
                int(marketplace_account_id), clean_text(marketplace).lower(),
                clean_text(order_id), clean_text(operation_key),
            ),
        )


def mark_draft_uncertain(
    *,
    seller_id: int,
    marketplace_account_id: int,
    marketplace: str,
    order_id: str,
    operation_key: str,
    error: str,
) -> None:
    ensure_schema()
    with connect() as con:
        con.execute(
            """UPDATE packlink_draft_guards SET status='uncertain',error=?,updated_at=?
               WHERE seller_id=? AND marketplace_account_id=? AND marketplace=?
                 AND order_id=? AND operation_key=?""",
            (
                clean_text(error)[:2000], now_iso(), int(seller_id),
                int(marketplace_account_id), clean_text(marketplace).lower(),
                clean_text(order_id), clean_text(operation_key),
            ),
        )
