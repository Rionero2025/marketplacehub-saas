from __future__ import annotations

import hashlib
import json
from collections import defaultdict
from datetime import datetime, timezone
from typing import Any, Callable

from services import db


PAGE_LIMIT = 100


def ensure_schema() -> None:
    with db.connect() as con:
        con.executescript(
            """
            CREATE TABLE IF NOT EXISTS kaufland_live_units (
                seller_id INTEGER NOT NULL REFERENCES sellers(id) ON DELETE CASCADE,
                marketplace_account_id INTEGER NOT NULL REFERENCES marketplace_accounts(id) ON DELETE CASCADE,
                environment TEXT NOT NULL CHECK(environment IN ('test','live')),
                storefront TEXT NOT NULL,
                id_unit INTEGER NOT NULL,
                id_offer TEXT NOT NULL DEFAULT '',
                id_product INTEGER,
                ean TEXT NOT NULL DEFAULT '',
                title TEXT NOT NULL DEFAULT '',
                manufacturer TEXT NOT NULL DEFAULT '',
                listing_price_cents INTEGER,
                minimum_price_cents INTEGER,
                amount INTEGER,
                status TEXT NOT NULL DEFAULT '',
                condition_code TEXT NOT NULL DEFAULT '',
                handling_time INTEGER,
                warehouse_id TEXT NOT NULL DEFAULT '',
                shipping_group_id TEXT NOT NULL DEFAULT '',
                date_lastchange_iso TEXT NOT NULL DEFAULT '',
                fingerprint TEXT NOT NULL DEFAULT '',
                is_present INTEGER NOT NULL DEFAULT 1,
                first_seen_at TEXT NOT NULL,
                last_seen_at TEXT NOT NULL,
                removed_at TEXT NOT NULL DEFAULT '',
                PRIMARY KEY(marketplace_account_id,environment,storefront,id_unit)
            );
            CREATE INDEX IF NOT EXISTS idx_kaufland_live_units_scope
            ON kaufland_live_units(
                seller_id,marketplace_account_id,environment,storefront,is_present
            );
            CREATE INDEX IF NOT EXISTS idx_kaufland_live_units_offer
            ON kaufland_live_units(
                marketplace_account_id,environment,storefront,id_offer
            );
            CREATE INDEX IF NOT EXISTS idx_kaufland_live_units_change
            ON kaufland_live_units(
                marketplace_account_id,environment,storefront,date_lastchange_iso,id_unit
            );
            CREATE TABLE IF NOT EXISTS kaufland_inventory_syncs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                seller_id INTEGER NOT NULL REFERENCES sellers(id) ON DELETE CASCADE,
                marketplace_account_id INTEGER NOT NULL REFERENCES marketplace_accounts(id) ON DELETE CASCADE,
                environment TEXT NOT NULL CHECK(environment IN ('test','live')),
                storefront TEXT NOT NULL,
                mode TEXT NOT NULL DEFAULT 'incremental',
                status TEXT NOT NULL,
                seen INTEGER NOT NULL DEFAULT 0,
                inserted INTEGER NOT NULL DEFAULT 0,
                updated INTEGER NOT NULL DEFAULT 0,
                unchanged INTEGER NOT NULL DEFAULT 0,
                missing INTEGER NOT NULL DEFAULT 0,
                error TEXT NOT NULL DEFAULT '',
                started_at TEXT NOT NULL,
                completed_at TEXT NOT NULL DEFAULT ''
            );
            CREATE INDEX IF NOT EXISTS idx_kaufland_inventory_syncs_scope
            ON kaufland_inventory_syncs(
                marketplace_account_id,environment,storefront,started_at
            );
            CREATE TABLE IF NOT EXISTS kaufland_inventory_cursors (
                seller_id INTEGER NOT NULL REFERENCES sellers(id) ON DELETE CASCADE,
                marketplace_account_id INTEGER NOT NULL REFERENCES marketplace_accounts(id) ON DELETE CASCADE,
                environment TEXT NOT NULL CHECK(environment IN ('test','live')),
                storefront TEXT NOT NULL,
                last_status TEXT NOT NULL DEFAULT '',
                scan_mode TEXT NOT NULL DEFAULT 'incremental',
                scan_token TEXT NOT NULL DEFAULT '',
                resume_offset INTEGER NOT NULL DEFAULT 0,
                page_limit INTEGER NOT NULL DEFAULT 100,
                expected_total INTEGER,
                partial_seen INTEGER NOT NULL DEFAULT 0,
                partial_inserted INTEGER NOT NULL DEFAULT 0,
                partial_updated INTEGER NOT NULL DEFAULT 0,
                partial_unchanged INTEGER NOT NULL DEFAULT 0,
                last_id_unit INTEGER,
                last_offer_id TEXT NOT NULL DEFAULT '',
                last_offer_change_iso TEXT NOT NULL DEFAULT '',
                last_completed_at TEXT NOT NULL DEFAULT '',
                last_attempt_at TEXT NOT NULL DEFAULT '',
                last_error TEXT NOT NULL DEFAULT '',
                updated_at TEXT NOT NULL DEFAULT '',
                PRIMARY KEY(marketplace_account_id,environment,storefront)
            );
            CREATE INDEX IF NOT EXISTS idx_kaufland_inventory_cursors_scope
            ON kaufland_inventory_cursors(
                seller_id,marketplace_account_id,environment,storefront
            );
            """
        )


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _scan_token() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def _text(value: Any) -> str:
    return str(value or "").strip()


def _int_or_none(value: Any) -> int | None:
    try:
        if value in (None, ""):
            return None
        return int(value)
    except (TypeError, ValueError):
        return None


def _date_key(value: Any) -> tuple[int, str]:
    raw = _text(value)
    if not raw:
        return (0, "")
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return (1, parsed.astimezone(timezone.utc).isoformat())
    except ValueError:
        return (0, raw)


def _watermark_key(item: dict[str, Any]) -> tuple[tuple[int, str], int, str]:
    return (
        _date_key(item.get("date_lastchange_iso")),
        int(item.get("id_unit") or 0),
        _text(item.get("id_offer")),
    )


def _merge_watermark(
    current: dict[str, Any] | None,
    candidates: list[dict[str, Any]],
) -> dict[str, Any]:
    values: list[dict[str, Any]] = []
    if current and current.get("last_id_unit"):
        values.append(
            {
                "id_unit": current.get("last_id_unit"),
                "id_offer": current.get("last_offer_id"),
                "date_lastchange_iso": current.get("last_offer_change_iso"),
            }
        )
    values.extend(candidates)
    if not values:
        return {"id_unit": None, "id_offer": "", "date_lastchange_iso": ""}
    best = max(values, key=_watermark_key)
    return {
        "id_unit": _int_or_none(best.get("id_unit")),
        "id_offer": _text(best.get("id_offer")),
        "date_lastchange_iso": _text(best.get("date_lastchange_iso")),
    }


def _extract_product(unit: dict[str, Any]) -> dict[str, Any]:
    product = unit.get("product")
    if not isinstance(product, dict):
        product = {}
    ean = (
        _text(unit.get("ean"))
        or _text(product.get("ean"))
        or _text(product.get("ean_main"))
    )
    if not ean:
        eans = product.get("eans")
        if isinstance(eans, list) and eans:
            first = eans[0]
            ean = _text(first.get("ean") if isinstance(first, dict) else first)
    title = (
        _text(product.get("title"))
        or _text(product.get("name"))
        or _text(product.get("product_name"))
        or _text(unit.get("title"))
    )
    manufacturer = (
        _text(product.get("manufacturer"))
        or _text(product.get("brand"))
        or _text(product.get("manufacturer_name"))
    )
    return {"ean": ean, "title": title, "manufacturer": manufacturer}


def normalize_unit(unit: dict[str, Any], storefront: str) -> dict[str, Any]:
    product = _extract_product(unit)
    row = {
        "storefront": _text(storefront).lower(),
        "id_unit": _int_or_none(unit.get("id_unit")),
        "id_offer": _text(unit.get("id_offer")),
        "id_product": _int_or_none(unit.get("id_product")),
        "ean": product["ean"],
        "title": product["title"],
        "manufacturer": product["manufacturer"],
        "listing_price_cents": _int_or_none(
            unit.get("listing_price") if unit.get("listing_price") is not None else unit.get("price")
        ),
        "minimum_price_cents": _int_or_none(unit.get("minimum_price")),
        "amount": _int_or_none(
            unit.get("amount") if unit.get("amount") is not None else unit.get("count")
        ),
        "status": _text(unit.get("status")),
        "condition_code": _text(unit.get("condition")),
        "handling_time": _int_or_none(unit.get("handling_time")),
        "warehouse_id": _text(unit.get("id_warehouse")),
        "shipping_group_id": _text(unit.get("id_shipping_group")),
        "date_lastchange_iso": _text(
            unit.get("date_lastchange_iso")
            or unit.get("ts_updated_iso")
            or unit.get("updated_at")
        ),
    }
    if not row["id_unit"]:
        raise ValueError("L'offerta Kaufland non contiene id_unit.")
    fingerprint_payload = {key: row[key] for key in row if key not in {"ean", "title", "manufacturer"}}
    row["fingerprint"] = hashlib.sha256(
        json.dumps(fingerprint_payload, sort_keys=True, ensure_ascii=False).encode("utf-8")
    ).hexdigest()
    return row


def cached_units(
    seller_id: int,
    account_id: int,
    environment: str,
    storefronts: list[str] | None = None,
    *,
    present_only: bool = True,
) -> list[dict[str, Any]]:
    ensure_schema()
    clauses = [
        "seller_id=?",
        "marketplace_account_id=?",
        "environment=?",
    ]
    params: list[Any] = [seller_id, account_id, environment]
    if present_only:
        clauses.append("is_present=1")
    cleaned = [str(value).strip().lower() for value in (storefronts or []) if str(value).strip()]
    if cleaned:
        placeholders = ",".join("?" for _ in cleaned)
        clauses.append(f"storefront IN ({placeholders})")
        params.extend(cleaned)
    return db.rows(
        f"""
        SELECT * FROM kaufland_live_units
        WHERE {' AND '.join(clauses)}
        ORDER BY storefront,id_offer,id_unit
        """,
        tuple(params),
    )


def inventory_cursor(
    seller_id: int,
    account_id: int,
    environment: str,
    storefront: str,
) -> dict[str, Any]:
    ensure_schema()
    row = db.row(
        """
        SELECT * FROM kaufland_inventory_cursors
        WHERE seller_id=? AND marketplace_account_id=? AND environment=? AND storefront=?
        """,
        (seller_id, account_id, environment, _text(storefront).lower()),
    )
    return dict(row or {})


def inventory_cursors(
    seller_id: int,
    account_id: int,
    environment: str,
) -> dict[str, dict[str, Any]]:
    ensure_schema()
    return {
        _text(item.get("storefront")).lower(): dict(item)
        for item in db.rows(
            """
            SELECT * FROM kaufland_inventory_cursors
            WHERE seller_id=? AND marketplace_account_id=? AND environment=?
            """,
            (seller_id, account_id, environment),
        )
        if _text(item.get("storefront"))
    }


def cached_summary(seller_id: int, account_id: int, environment: str) -> list[dict[str, Any]]:
    ensure_schema()
    unit_rows = db.rows(
        """
        SELECT u.storefront,
               SUM(CASE WHEN u.is_present=1 THEN 1 ELSE 0 END) AS active_count,
               MAX(u.last_seen_at) AS last_seen_at
        FROM kaufland_live_units u
        WHERE u.seller_id=? AND u.marketplace_account_id=? AND u.environment=?
        GROUP BY u.storefront
        ORDER BY u.storefront
        """,
        (seller_id, account_id, environment),
    )
    cursors = inventory_cursors(seller_id, account_id, environment)
    result: dict[str, dict[str, Any]] = {
        _text(item.get("storefront")).lower(): dict(item) for item in unit_rows
    }
    for code, cursor in cursors.items():
        row = result.setdefault(code, {"storefront": code, "active_count": 0, "last_seen_at": ""})
        row.update(
            {
                "last_sync_at": cursor.get("last_completed_at") or "",
                "last_attempt_at": cursor.get("last_attempt_at") or "",
                "last_status": cursor.get("last_status") or "",
                "last_offer_id": cursor.get("last_offer_id") or "",
                "last_id_unit": cursor.get("last_id_unit"),
                "last_offer_change_iso": cursor.get("last_offer_change_iso") or "",
                "resume_offset": int(cursor.get("resume_offset") or 0),
                "expected_total": cursor.get("expected_total"),
                "last_error": cursor.get("last_error") or "",
            }
        )
    return [result[code] for code in sorted(result)]


def _existing_map(account_id: int, environment: str, storefront: str) -> dict[int, dict[str, Any]]:
    return {
        int(item["id_unit"]): item
        for item in db.rows(
            """
            SELECT * FROM kaufland_live_units
            WHERE marketplace_account_id=? AND environment=? AND storefront=?
            """,
            (account_id, environment, storefront),
        )
    }


def _merge_product_fields(
    normalized: dict[str, Any],
    existing: dict[str, Any] | None,
) -> dict[str, Any]:
    if existing:
        for field in ("ean", "title", "manufacturer"):
            if not normalized.get(field):
                normalized[field] = _text(existing.get(field))
    return normalized


def _page_units(response: Any) -> tuple[list[dict[str, Any]], int | None]:
    if not isinstance(response, dict):
        return [], None
    raw_page = response.get("data", [])
    page = [dict(item) for item in raw_page if isinstance(item, dict)] if isinstance(raw_page, list) else []
    embedded_payload = response.get("embedded", {})
    products = embedded_payload.get("products") if isinstance(embedded_payload, dict) else None
    product_rows: list[dict[str, Any]] = []
    if isinstance(products, list):
        product_rows = [item for item in products if isinstance(item, dict)]
    elif isinstance(products, dict):
        nested = products.get("data")
        if isinstance(nested, list):
            product_rows = [item for item in nested if isinstance(item, dict)]
        elif products.get("id_product") not in (None, ""):
            product_rows = [products]
        else:
            product_rows = [item for item in products.values() if isinstance(item, dict)]
    by_id = {
        _text(item.get("id_product")): item
        for item in product_rows
        if _text(item.get("id_product"))
    }
    only = product_rows[0] if len(product_rows) == 1 else None
    for item in page:
        if not isinstance(item.get("product"), dict):
            product = by_id.get(_text(item.get("id_product"))) or only
            if isinstance(product, dict):
                item["product"] = dict(product)
    pagination = response.get("pagination", {})
    total = _int_or_none(pagination.get("total")) if isinstance(pagination, dict) else None
    return page, total


def _fetch_units_page(client, storefront: str, *, limit: int, offset: int, embedded: str) -> Any:
    page_method = getattr(client, "units_page", None)
    if callable(page_method):
        return page_method(storefront, limit=limit, offset=offset, embedded=embedded)
    # Compatibility with older/custom clients. They cannot resume by offset,
    # therefore the complete list is exposed as a single synthetic page.
    if offset > 0:
        return {"data": [], "pagination": {"offset": offset, "limit": limit, "total": offset}}
    all_method = getattr(client, "all_units", None)
    if not callable(all_method):
        raise AttributeError("Il client Kaufland non espone units_page né all_units.")
    data = all_method(storefront, embedded=embedded) or []
    rows = data if isinstance(data, list) else []
    return {"data": rows, "pagination": {"offset": 0, "limit": max(limit, len(rows)), "total": len(rows)}}


def _upsert_cursor(
    *,
    seller_id: int,
    account_id: int,
    environment: str,
    storefront: str,
    values: dict[str, Any],
) -> None:
    defaults = {
        "last_status": "",
        "scan_mode": "incremental",
        "scan_token": "",
        "resume_offset": 0,
        "page_limit": PAGE_LIMIT,
        "expected_total": None,
        "partial_seen": 0,
        "partial_inserted": 0,
        "partial_updated": 0,
        "partial_unchanged": 0,
        "last_id_unit": None,
        "last_offer_id": "",
        "last_offer_change_iso": "",
        "last_completed_at": "",
        "last_attempt_at": "",
        "last_error": "",
        "updated_at": _now(),
    }
    defaults.update(values)
    db.execute(
        """
        INSERT INTO kaufland_inventory_cursors(
            seller_id,marketplace_account_id,environment,storefront,last_status,
            scan_mode,scan_token,resume_offset,page_limit,expected_total,partial_seen,
            partial_inserted,partial_updated,partial_unchanged,last_id_unit,last_offer_id,
            last_offer_change_iso,last_completed_at,last_attempt_at,last_error,updated_at
        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(marketplace_account_id,environment,storefront)
        DO UPDATE SET
            seller_id=excluded.seller_id,
            last_status=excluded.last_status,
            scan_mode=excluded.scan_mode,
            scan_token=excluded.scan_token,
            resume_offset=excluded.resume_offset,
            page_limit=excluded.page_limit,
            expected_total=excluded.expected_total,
            partial_seen=excluded.partial_seen,
            partial_inserted=excluded.partial_inserted,
            partial_updated=excluded.partial_updated,
            partial_unchanged=excluded.partial_unchanged,
            last_id_unit=excluded.last_id_unit,
            last_offer_id=excluded.last_offer_id,
            last_offer_change_iso=excluded.last_offer_change_iso,
            last_completed_at=excluded.last_completed_at,
            last_attempt_at=excluded.last_attempt_at,
            last_error=excluded.last_error,
            updated_at=excluded.updated_at
        """,
        (
            seller_id,
            account_id,
            environment,
            storefront,
            defaults["last_status"],
            defaults["scan_mode"],
            defaults["scan_token"],
            int(defaults["resume_offset"] or 0),
            int(defaults["page_limit"] or PAGE_LIMIT),
            defaults["expected_total"],
            int(defaults["partial_seen"] or 0),
            int(defaults["partial_inserted"] or 0),
            int(defaults["partial_updated"] or 0),
            int(defaults["partial_unchanged"] or 0),
            defaults["last_id_unit"],
            _text(defaults["last_offer_id"]),
            _text(defaults["last_offer_change_iso"]),
            _text(defaults["last_completed_at"]),
            _text(defaults["last_attempt_at"]),
            _text(defaults["last_error"])[:2000],
            _text(defaults["updated_at"]) or _now(),
        ),
    )


def sync_storefront(
    client,
    *,
    seller_id: int,
    account_id: int,
    environment: str,
    storefront: str,
    force_full: bool = False,
    progress: Callable[[int, int | None], None] | None = None,
) -> dict[str, Any]:
    """Synchronize one Kaufland storefront with persistent resume checkpoints.

    Kaufland's units endpoint exposes offset/limit pagination but no documented
    ``updated_since`` filter. Therefore a completed refresh still scans the
    lightweight unit manifest to detect removals and modifications. It writes
    only new/changed units and fetches product details only where needed.

    If a refresh is interrupted, the saved offset and scan token are reused, so
    the next attempt resumes from the last successfully downloaded page instead
    of restarting from zero.
    """
    ensure_schema()
    code = _text(storefront).lower()
    if not code:
        raise ValueError("Storefront Kaufland mancante.")

    existing = _existing_map(account_id, environment, code)
    cursor = inventory_cursor(seller_id, account_id, environment, code)
    first_sync = not existing
    resumable = (
        not force_full
        and _text(cursor.get("scan_token"))
        and int(cursor.get("resume_offset") or 0) > 0
        and _text(cursor.get("last_status")) in {"running", "error", "interrupted"}
    )
    if resumable:
        mode = _text(cursor.get("scan_mode")) or ("full" if first_sync else "incremental")
        scan_token = _text(cursor.get("scan_token"))
        offset = int(cursor.get("resume_offset") or 0)
        seen_count = int(cursor.get("partial_seen") or 0)
        inserted = int(cursor.get("partial_inserted") or 0)
        updated = int(cursor.get("partial_updated") or 0)
        unchanged = int(cursor.get("partial_unchanged") or 0)
        expected_total = _int_or_none(cursor.get("expected_total"))
        resumed_from = offset
        watermark = _merge_watermark(cursor, [])
    else:
        mode = "full" if force_full or first_sync else "incremental"
        scan_token = _scan_token()
        offset = 0
        seen_count = inserted = updated = unchanged = 0
        expected_total = None
        resumed_from = 0
        watermark = {"id_unit": None, "id_offer": "", "date_lastchange_iso": ""}

    started = _now()
    sync_id = db.execute(
        """
        INSERT INTO kaufland_inventory_syncs(
            seller_id,marketplace_account_id,environment,storefront,mode,status,started_at
        ) VALUES(?,?,?,?,?,'running',?)
        """,
        (seller_id, account_id, environment, code, mode, started),
    )
    _upsert_cursor(
        seller_id=seller_id,
        account_id=account_id,
        environment=environment,
        storefront=code,
        values={
            **cursor,
            "last_status": "running",
            "scan_mode": mode,
            "scan_token": scan_token,
            "resume_offset": offset,
            "expected_total": expected_total,
            "partial_seen": seen_count,
            "partial_inserted": inserted,
            "partial_updated": updated,
            "partial_unchanged": unchanged,
            "last_id_unit": watermark["id_unit"],
            "last_offer_id": watermark["id_offer"],
            "last_offer_change_iso": watermark["date_lastchange_iso"],
            "last_attempt_at": started,
            "last_error": "",
            "updated_at": started,
        },
    )

    try:
        embedded = "products" if mode == "full" else ""
        page_limit = PAGE_LIMIT
        while True:
            response = _fetch_units_page(
                client,
                code,
                limit=page_limit,
                offset=offset,
                embedded=embedded,
            )
            raw_page, page_total = _page_units(response)
            if page_total is not None:
                expected_total = page_total
            normalized_by_id: dict[int, dict[str, Any]] = {}
            for raw in raw_page:
                normalized = normalize_unit(raw, code)
                unit_id = int(normalized["id_unit"])
                old = existing.get(unit_id)
                normalized = _merge_product_fields(normalized, old)
                needs_product = mode == "incremental" and (
                    old is None or not normalized.get("ean") or not normalized.get("title")
                )
                if needs_product:
                    try:
                        detailed = client.unit(unit_id, code, embedded="products")
                        if isinstance(detailed, dict):
                            enriched = normalize_unit(detailed, code)
                            normalized = _merge_product_fields(enriched, normalized)
                    except Exception:
                        pass
                normalized_by_id[unit_id] = normalized
            normalized_page = list(normalized_by_id.values())

            now = _now()
            page_inserted = page_updated = page_unchanged = 0
            with db.connect() as con:
                for item in normalized_page:
                    unit_id = int(item["id_unit"])
                    old = existing.get(unit_id)
                    if old is None:
                        page_inserted += 1
                    elif (
                        _text(old.get("fingerprint")) != item["fingerprint"]
                        or int(old.get("is_present") or 0) != 1
                        or any(
                            not _text(old.get(field)) and _text(item.get(field))
                            for field in ("ean", "title", "manufacturer")
                        )
                    ):
                        page_updated += 1
                    else:
                        page_unchanged += 1
                    con.execute(
                        """
                        INSERT INTO kaufland_live_units(
                            seller_id,marketplace_account_id,environment,storefront,id_unit,
                            id_offer,id_product,ean,title,manufacturer,listing_price_cents,
                            minimum_price_cents,amount,status,condition_code,handling_time,
                            warehouse_id,shipping_group_id,date_lastchange_iso,fingerprint,
                            is_present,first_seen_at,last_seen_at,removed_at
                        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,1,?,?, '')
                        ON CONFLICT(marketplace_account_id,environment,storefront,id_unit)
                        DO UPDATE SET
                            seller_id=excluded.seller_id,
                            id_offer=excluded.id_offer,
                            id_product=excluded.id_product,
                            ean=CASE WHEN excluded.ean<>'' THEN excluded.ean ELSE kaufland_live_units.ean END,
                            title=CASE WHEN excluded.title<>'' THEN excluded.title ELSE kaufland_live_units.title END,
                            manufacturer=CASE WHEN excluded.manufacturer<>'' THEN excluded.manufacturer ELSE kaufland_live_units.manufacturer END,
                            listing_price_cents=excluded.listing_price_cents,
                            minimum_price_cents=excluded.minimum_price_cents,
                            amount=excluded.amount,
                            status=excluded.status,
                            condition_code=excluded.condition_code,
                            handling_time=excluded.handling_time,
                            warehouse_id=excluded.warehouse_id,
                            shipping_group_id=excluded.shipping_group_id,
                            date_lastchange_iso=excluded.date_lastchange_iso,
                            fingerprint=excluded.fingerprint,
                            is_present=1,
                            last_seen_at=excluded.last_seen_at,
                            removed_at=''
                        """,
                        (
                            seller_id,
                            account_id,
                            environment,
                            code,
                            unit_id,
                            item["id_offer"],
                            item["id_product"],
                            item["ean"],
                            item["title"],
                            item["manufacturer"],
                            item["listing_price_cents"],
                            item["minimum_price_cents"],
                            item["amount"],
                            item["status"],
                            item["condition_code"],
                            item["handling_time"],
                            item["warehouse_id"],
                            item["shipping_group_id"],
                            item["date_lastchange_iso"],
                            item["fingerprint"],
                            now,
                            scan_token,
                        ),
                    )
                    existing[unit_id] = {
                        **(old or {}),
                        **item,
                        "is_present": 1,
                        "last_seen_at": scan_token,
                    }

            inserted += page_inserted
            updated += page_updated
            unchanged += page_unchanged
            seen_count += len(normalized_page)
            watermark = _merge_watermark(watermark, normalized_page)
            next_offset = offset + len(raw_page)
            _upsert_cursor(
                seller_id=seller_id,
                account_id=account_id,
                environment=environment,
                storefront=code,
                values={
                    **cursor,
                    "last_status": "running",
                    "scan_mode": mode,
                    "scan_token": scan_token,
                    "resume_offset": next_offset,
                    "page_limit": page_limit,
                    "expected_total": expected_total,
                    "partial_seen": seen_count,
                    "partial_inserted": inserted,
                    "partial_updated": updated,
                    "partial_unchanged": unchanged,
                    "last_id_unit": watermark["id_unit"],
                    "last_offer_id": watermark["id_offer"],
                    "last_offer_change_iso": watermark["date_lastchange_iso"],
                    "last_completed_at": _text(cursor.get("last_completed_at")),
                    "last_attempt_at": started,
                    "last_error": "",
                    "updated_at": now,
                },
            )
            if progress:
                progress(seen_count, expected_total)

            if not raw_page:
                break
            if expected_total is not None and next_offset >= expected_total:
                offset = next_offset
                break
            if len(raw_page) < page_limit and expected_total is None:
                offset = next_offset
                break
            offset = next_offset

        completed_at = _now()
        with db.connect() as con:
            missing_rows = con.execute(
                """
                SELECT id_unit FROM kaufland_live_units
                WHERE marketplace_account_id=? AND environment=? AND storefront=?
                  AND is_present=1 AND last_seen_at<>?
                """,
                (account_id, environment, code, scan_token),
            ).fetchall()
            missing_ids = [int(row[0]) for row in missing_rows]
            if missing_ids:
                placeholders = ",".join("?" for _ in missing_ids)
                con.execute(
                    f"""
                    UPDATE kaufland_live_units
                    SET is_present=0,removed_at=?
                    WHERE marketplace_account_id=? AND environment=? AND storefront=?
                      AND id_unit IN ({placeholders})
                    """,
                    (completed_at, account_id, environment, code, *missing_ids),
                )
            con.execute(
                """
                UPDATE kaufland_inventory_syncs
                SET status='completed',seen=?,inserted=?,updated=?,unchanged=?,missing=?,completed_at=?
                WHERE id=?
                """,
                (
                    seen_count,
                    inserted,
                    updated,
                    unchanged,
                    len(missing_ids),
                    completed_at,
                    sync_id,
                ),
            )
        _upsert_cursor(
            seller_id=seller_id,
            account_id=account_id,
            environment=environment,
            storefront=code,
            values={
                **cursor,
                "last_status": "completed",
                "scan_mode": mode,
                "scan_token": "",
                "resume_offset": 0,
                "page_limit": page_limit,
                "expected_total": expected_total if expected_total is not None else seen_count,
                "partial_seen": 0,
                "partial_inserted": 0,
                "partial_updated": 0,
                "partial_unchanged": 0,
                "last_id_unit": watermark["id_unit"],
                "last_offer_id": watermark["id_offer"],
                "last_offer_change_iso": watermark["date_lastchange_iso"],
                "last_completed_at": completed_at,
                "last_attempt_at": started,
                "last_error": "",
                "updated_at": completed_at,
            },
        )
        return {
            "storefront": code,
            "mode": mode,
            "seen": seen_count,
            "inserted": inserted,
            "updated": updated,
            "unchanged": unchanged,
            "missing": len(missing_ids),
            "completed_at": completed_at,
            "resumed_from": resumed_from,
            "last_offer_id": watermark["id_offer"],
            "last_id_unit": watermark["id_unit"],
            "last_offer_change_iso": watermark["date_lastchange_iso"],
        }
    except Exception as error:
        failed_at = _now()
        db.execute(
            """
            UPDATE kaufland_inventory_syncs
            SET status='error',seen=?,inserted=?,updated=?,unchanged=?,error=?,completed_at=?
            WHERE id=?
            """,
            (
                seen_count,
                inserted,
                updated,
                unchanged,
                str(error)[:2000],
                failed_at,
                sync_id,
            ),
        )
        _upsert_cursor(
            seller_id=seller_id,
            account_id=account_id,
            environment=environment,
            storefront=code,
            values={
                **cursor,
                "last_status": "error",
                "scan_mode": mode,
                "scan_token": scan_token,
                "resume_offset": offset,
                "page_limit": PAGE_LIMIT,
                "expected_total": expected_total,
                "partial_seen": seen_count,
                "partial_inserted": inserted,
                "partial_updated": updated,
                "partial_unchanged": unchanged,
                "last_id_unit": watermark["id_unit"],
                "last_offer_id": watermark["id_offer"],
                "last_offer_change_iso": watermark["date_lastchange_iso"],
                "last_completed_at": _text(cursor.get("last_completed_at")),
                "last_attempt_at": started,
                "last_error": str(error)[:2000],
                "updated_at": failed_at,
            },
        )
        raise


def mark_units_removed(account_id: int, environment: str, keys: list[tuple[str, int]]) -> None:
    if not keys:
        return
    now = _now()
    with db.connect() as con:
        con.executemany(
            """
            UPDATE kaufland_live_units
            SET is_present=0,removed_at=?
            WHERE marketplace_account_id=? AND environment=? AND storefront=? AND id_unit=?
            """,
            [(now, account_id, environment, str(storefront).lower(), int(unit_id)) for storefront, unit_id in keys],
        )


def latest_syncs(account_id: int, environment: str) -> dict[str, dict[str, Any]]:
    ensure_schema()
    result: dict[str, dict[str, Any]] = {}
    for item in db.rows(
        """
        SELECT * FROM kaufland_inventory_syncs
        WHERE marketplace_account_id=? AND environment=?
        ORDER BY id DESC
        """,
        (account_id, environment),
    ):
        code = _text(item.get("storefront")).lower()
        if code and code not in result:
            result[code] = item
    return result


def group_counts(units: list[dict[str, Any]]) -> dict[str, int]:
    counts: dict[str, int] = defaultdict(int)
    for item in units:
        counts[_text(item.get("storefront")).lower()] += 1
    return dict(counts)
