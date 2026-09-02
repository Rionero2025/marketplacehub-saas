from __future__ import annotations

import math
import threading
from collections.abc import Iterable, Mapping
from datetime import date, datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

from services.accounting import (
    apply_accounting_manual_overrides,
    computed_profit_values,
    computed_values,
    fetch_kaufland_accounting_orders,
    fetch_worten_accounting_orders,
    totals,
    upsert_accounting_rows,
)
from services.cecotec_orders import clean_text
from services.db import connect, rows, sellers
from services.profit_sharing import seller_profit_settings, split_profit
from services.security import decrypt_dict

DEFAULT_DASHBOARD_TIMEZONE = "Europe/Rome"
DEFAULT_AUTO_SYNC_SECONDS = 300
DEFAULT_AUTO_SYNC_DAYS = 7
DEFAULT_AUTO_SYNC_MAX_ROWS = 100
DEFAULT_SYNC_STALE_SECONDS = 20 * 60

_SYNC_THREAD_LOCK = threading.Lock()
_SYNC_THREAD: threading.Thread | None = None


def _dashboard_timezone(name: str = DEFAULT_DASHBOARD_TIMEZONE):
    try:
        return ZoneInfo(name)
    except Exception:  # pragma: no cover - only for systems without tzdata
        return timezone.utc


def order_local_date(value: Any, timezone_name: str = DEFAULT_DASHBOARD_TIMEZONE) -> date | None:
    """Return an order date in the dashboard timezone.

    Accounting rows are normally ISO-8601 timestamps, but older imports can contain
    only a date or an Italian date. Invalid values are kept in the cumulative totals
    and excluded only from day/week/month windows.
    """
    text = clean_text(value)
    if not text:
        return None
    target_timezone = _dashboard_timezone(timezone_name)
    candidates = [text, text.replace("Z", "+00:00")]
    for candidate in candidates:
        try:
            parsed = datetime.fromisoformat(candidate)
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=target_timezone)
            return parsed.astimezone(target_timezone).date()
        except ValueError:
            pass
    for pattern in ("%Y-%m-%d", "%d/%m/%Y", "%Y/%m/%d"):
        try:
            return datetime.strptime(text[:10], pattern).date()
        except ValueError:
            pass
    return None


def _finite_number(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _order_identity(item: Mapping[str, Any], fallback_index: int) -> str:
    """Return a stable identity so multi-line orders count only once."""
    account_id = clean_text(item.get("marketplace_account_id") or item.get("account_id"))
    marketplace = clean_text(item.get("marketplace")).lower()
    order_id = clean_text(item.get("order_id"))
    if order_id:
        return f"{account_id}|{marketplace}|{order_id}"
    row_key = clean_text(item.get("row_key"))
    if row_key:
        return f"row|{row_key}"
    return f"fallback|{fallback_index}"


def period_totals(records: Iterable[Mapping[str, Any]]) -> dict[str, float | int]:
    """Aggregate net sales, profit and unique order count.

    Sales/profit follow the same accounting rules used by the Contabilità page.
    Order counts include every status, while cancelled/refunded/no-stock orders
    continue to have zero economic values.
    """
    values = [dict(item) for item in records]
    summary = totals(values)
    missing_profit_rows = 0
    order_ids: set[str] = set()
    for index, item in enumerate(values):
        order_ids.add(_order_identity(item, index))
        line_summary = totals([item])
        if abs(float(line_summary["sale"])) < 0.005:
            continue
        if computed_values(item).get("net_revenue_eur") is None:
            missing_profit_rows += 1
    return {
        "sales": round(float(summary["sale"]), 2),
        "profit": round(float(summary["net_revenue"]), 2),
        "missing_profit_rows": missing_profit_rows,
        "rows": len(values),
        "orders": len(order_ids),
    }


def date_range_totals(
    records: Iterable[Mapping[str, Any]],
    date_from: date,
    date_to: date,
    *,
    timezone_name: str = DEFAULT_DASHBOARD_TIMEZONE,
) -> dict[str, float | int]:
    """Aggregate accounting rows whose local order date is inside the range.

    Both boundaries are inclusive. Invalid or missing order dates remain available
    in the cumulative dashboard period but are excluded from an explicit date
    interval, because they cannot be placed reliably on a calendar.
    """
    start = min(date_from, date_to)
    end = max(date_from, date_to)
    selected: list[dict[str, Any]] = []
    for source in records:
        item = dict(source)
        item_date = order_local_date(item.get("order_created"), timezone_name)
        if item_date is not None and start <= item_date <= end:
            selected.append(item)
    return period_totals(selected)


def seller_dashboard_detail_rows(
    seller: Mapping[str, Any],
    records: Iterable[Mapping[str, Any]],
    *,
    date_from: date,
    date_to: date,
    timezone_name: str = DEFAULT_DASHBOARD_TIMEZONE,
) -> list[dict[str, Any]]:
    """Return accounting rows behind the dashboard cards for one Seller.

    The values are deliberately derived from the same manual overrides, zeroing
    rules and profit-sharing formulas used by the headline Dashboard totals.
    Keeping the detail model here prevents a clickable card from drifting away
    from the number that opened it.
    """
    start = min(date_from, date_to)
    end = max(date_from, date_to)
    settings = seller_profit_settings(seller)
    seller_id = int(seller.get("id") or 0)
    seller_name = clean_text(seller.get("name")) or f"Seller {seller_id}"
    partner_name = clean_text(settings.get("partner_name")) or seller_name
    output: list[dict[str, Any]] = []

    for source in records:
        item = dict(source)
        item_date = order_local_date(item.get("order_created"), timezone_name)
        if item_date is None or not (start <= item_date <= end):
            continue

        line_totals = totals([item])
        formulas = computed_profit_values(
            item, settings["our_pct"], settings["partner_pct"]
        )
        missing_reason = ""
        if formulas.get("net_revenue_eur") is None and abs(float(line_totals["sale"])) >= 0.005:
            purchase_missing = _finite_number(item.get("purchase_cost_eur")) is None
            payout_missing = _finite_number(item.get("payout_eur")) is None
            if purchase_missing and payout_missing:
                missing_reason = "Costo acquisto e da ricevere mancanti"
            elif purchase_missing:
                missing_reason = "Costo acquisto mancante"
            elif payout_missing:
                missing_reason = "Da ricevere mancante"
            else:
                missing_reason = "Margine non determinabile"

        purchase_value: float | None = float(line_totals["purchase"])
        payout_value: float | None = float(line_totals["payout"])
        if formulas.get("net_revenue_eur") is None:
            if _finite_number(item.get("purchase_cost_eur")) is None:
                purchase_value = None
            if _finite_number(item.get("payout_eur")) is None:
                payout_value = None

        extra_cost_value = _finite_number(item.get("extra_cost_eur")) or 0.0
        if (
            formulas.get("net_revenue_eur") == 0.0
            and abs(float(line_totals["sale"])) < 0.005
            and abs(float(line_totals["purchase"])) < 0.005
            and abs(float(line_totals["payout"])) < 0.005
        ):
            # Cancelled/refunded/no-stock rows are economically zero in Accounting.
            extra_cost_value = 0.0

        output.append({
            "seller_id": seller_id,
            "seller_name": seller_name,
            "partner_name": partner_name,
            "marketplace_account_id": int(
                item.get("marketplace_account_id") or item.get("account_id") or 0
            ),
            "marketplace": clean_text(item.get("marketplace")).title(),
            "order_id": clean_text(item.get("order_id")),
            "row_key": clean_text(item.get("row_key")),
            "order_created": clean_text(item.get("order_created")),
            "order_date": item_date,
            "status": clean_text(item.get("status_label") or item.get("raw_status")),
            "supplier": clean_text(item.get("supplier")),
            "country_code": clean_text(item.get("country_code")).upper(),
            "market_label": clean_text(item.get("market_label")),
            "composite_sku": clean_text(item.get("composite_sku")),
            "product_title": clean_text(item.get("product_title")),
            "ean": clean_text(item.get("ean")),
            "quantity": _finite_number(item.get("quantity")) or 0.0,
            "sale_eur": float(line_totals["sale"]),
            "purchase_cost_eur": purchase_value,
            "commission_eur": float(line_totals["commission"]),
            "refund_eur": float(line_totals["refund"]),
            "payout_eur": payout_value,
            "extra_cost_eur": extra_cost_value,
            "gross_margin_eur": formulas.get("gross_margin_eur"),
            "net_revenue_eur": formulas.get("net_revenue_eur"),
            "our_pct": float(settings["our_pct"]),
            "partner_pct": float(settings["partner_pct"]),
            "our_share_eur": formulas.get("our_share_eur"),
            "partner_share_eur": formulas.get("partner_share_eur"),
            "missing_reason": missing_reason,
        })
    return output


DASHBOARD_ACCOUNTING_COLUMNS = (
    "seller_id,marketplace_account_id,marketplace,row_key,order_id,order_created,"
    "country_code,market_label,raw_status,status_label,supplier,composite_sku,"
    "product_title,ean,quantity,sale_eur,purchase_cost_eur,commission_eur,"
    "refund_eur,payout_eur,extra_cost_eur,note,supplier_order_number,synced_at"
)


def _dashboard_source_data(
    *,
    today: date | None = None,
    selected_from: date | None = None,
    selected_to: date | None = None,
    timezone_name: str = DEFAULT_DASHBOARD_TIMEZONE,
) -> dict[str, Any]:
    """Load Dashboard data once and build both summaries and detail rows.

    Before v302 the page executed two full accounting reads on every refresh:
    one for the KPI summaries and another for Top Products/detail cards.  The
    shared snapshot halves the heavy DB transfer and deliberately excludes
    ``raw_json`` and other large columns unused by the Dashboard.
    """
    seller_rows = sellers()
    if not seller_rows:
        return {"summaries": [], "detail_rows": [], "rows_loaded": 0}

    seller_ids = [int(item["id"]) for item in seller_rows]
    placeholders = ",".join("?" for _ in seller_ids)
    accounting_records = apply_accounting_manual_overrides(rows(
        f"""SELECT {DASHBOARD_ACCOUNTING_COLUMNS}
        FROM accounting_order_lines
        WHERE seller_id IN ({placeholders})
        ORDER BY seller_id,order_created,id""",
        tuple(seller_ids),
    ))
    grouped: dict[int, list[dict[str, Any]]] = {seller_id: [] for seller_id in seller_ids}
    for item in accounting_records:
        grouped.setdefault(int(item.get("seller_id") or 0), []).append(item)

    summaries = [
        seller_dashboard_summary(
            seller,
            grouped.get(int(seller["id"]), []),
            today=today,
            selected_from=selected_from,
            selected_to=selected_to,
            timezone_name=timezone_name,
        )
        for seller in seller_rows
    ]
    detail_rows: list[dict[str, Any]] = []
    if selected_from is not None and selected_to is not None:
        for seller in seller_rows:
            detail_rows.extend(
                seller_dashboard_detail_rows(
                    seller,
                    grouped.get(int(seller["id"]), []),
                    date_from=selected_from,
                    date_to=selected_to,
                    timezone_name=timezone_name,
                )
            )
    return {
        "summaries": summaries,
        "detail_rows": detail_rows,
        "rows_loaded": len(accounting_records),
    }


def dashboard_snapshot(
    *,
    today: date | None = None,
    selected_from: date | None = None,
    selected_to: date | None = None,
    timezone_name: str = DEFAULT_DASHBOARD_TIMEZONE,
) -> dict[str, Any]:
    """Public v302 snapshot used by Streamlit now and FastAPI later."""
    return _dashboard_source_data(
        today=today,
        selected_from=selected_from,
        selected_to=selected_to,
        timezone_name=timezone_name,
    )


def dashboard_detail_rows(
    *,
    selected_from: date,
    selected_to: date,
    timezone_name: str = DEFAULT_DASHBOARD_TIMEZONE,
) -> list[dict[str, Any]]:
    """Compatibility wrapper. Prefer ``dashboard_snapshot`` for new callers."""
    return list(
        dashboard_snapshot(
            selected_from=selected_from,
            selected_to=selected_to,
            timezone_name=timezone_name,
        )["detail_rows"]
    )

def dashboard_order_detail_rows(
    detail_rows: Iterable[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """Collapse line-level dashboard detail into exactly one row per marketplace order."""
    grouped: dict[str, dict[str, Any]] = {}
    for index, source in enumerate(detail_rows):
        item = dict(source)
        seller_id = int(item.get("seller_id") or 0)
        account_id = int(item.get("marketplace_account_id") or 0)
        marketplace = clean_text(item.get("marketplace")).lower()
        order_id = clean_text(item.get("order_id"))
        row_key = clean_text(item.get("row_key"))
        identity = (
            f"{seller_id}|{account_id}|{marketplace}|{order_id}"
            if order_id
            else f"row|{seller_id}|{row_key or index}"
        )
        target = grouped.get(identity)
        if target is None:
            target = {
                "seller_name": clean_text(item.get("seller_name")),
                "marketplace": clean_text(item.get("marketplace")),
                "order_id": order_id,
                "order_date": item.get("order_date"),
                "status": clean_text(item.get("status")),
                "products": 0,
                "sale_eur": 0.0,
                "net_revenue_eur": 0.0,
                "our_share_eur": 0.0,
                "partner_share_eur": 0.0,
                "missing_profit_rows": 0,
            }
            grouped[identity] = target
        target["products"] += 1
        target["sale_eur"] += float(item.get("sale_eur") or 0.0)
        if item.get("net_revenue_eur") is None and abs(float(item.get("sale_eur") or 0.0)) >= 0.005:
            target["missing_profit_rows"] += 1
        else:
            target["net_revenue_eur"] += float(item.get("net_revenue_eur") or 0.0)
            target["our_share_eur"] += float(item.get("our_share_eur") or 0.0)
            target["partner_share_eur"] += float(item.get("partner_share_eur") or 0.0)

    output: list[dict[str, Any]] = []
    for item in grouped.values():
        missing = int(item["missing_profit_rows"] or 0)
        item["sale_eur"] = round(float(item["sale_eur"]), 2)
        if missing:
            item["net_revenue_eur"] = None
            item["our_share_eur"] = None
            item["partner_share_eur"] = None
        else:
            item["net_revenue_eur"] = round(float(item["net_revenue_eur"]), 2)
            item["our_share_eur"] = round(float(item["our_share_eur"]), 2)
            item["partner_share_eur"] = round(float(item["partner_share_eur"]), 2)
        output.append(item)
    output.sort(
        key=lambda item: (
            item.get("order_date") or date.min,
            clean_text(item.get("order_id")),
        ),
        reverse=True,
    )
    return output


def dashboard_missing_detail_rows(
    detail_rows: Iterable[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """Return the same unresolved rows counted by ``missing_profit_rows``."""
    return [
        dict(item)
        for item in detail_rows
        if abs(float(item.get("sale_eur") or 0.0)) >= 0.005
        and item.get("net_revenue_eur") is None
    ]


def seller_dashboard_summary(
    seller: Mapping[str, Any],
    records: Iterable[Mapping[str, Any]],
    *,
    today: date | None = None,
    selected_from: date | None = None,
    selected_to: date | None = None,
    timezone_name: str = DEFAULT_DASHBOARD_TIMEZONE,
) -> dict[str, Any]:
    today = today or datetime.now(_dashboard_timezone(timezone_name)).date()
    week_start = today - timedelta(days=today.weekday())
    month_start = today.replace(day=1)
    values = [dict(item) for item in records]

    dated: list[tuple[dict[str, Any], date | None]] = [
        (item, order_local_date(item.get("order_created"), timezone_name))
        for item in values
    ]

    def between(start: date, end: date) -> list[dict[str, Any]]:
        return [item for item, item_date in dated if item_date is not None and start <= item_date <= end]

    periods = {
        "today": period_totals(between(today, today)),
        "week": period_totals(between(week_start, today)),
        "month": period_totals(between(month_start, today)),
        "all": period_totals(values),
    }
    if selected_from is not None and selected_to is not None:
        periods["selected"] = date_range_totals(
            values,
            selected_from,
            selected_to,
            timezone_name=timezone_name,
        )
    profit_settings = seller_profit_settings(seller)
    for period in periods.values():
        period.update(
            split_profit(
                period.get("profit"),
                profit_settings["our_pct"],
                profit_settings["partner_pct"],
            )
        )
    last_synced = max(
        (clean_text(item.get("synced_at")) for item in values if clean_text(item.get("synced_at"))),
        default="",
    )
    return {
        "seller_id": int(seller.get("id") or 0),
        "seller_name": clean_text(seller.get("name")) or f"Seller {seller.get('id', '')}",
        "legal_name": clean_text(seller.get("legal_name")),
        "periods": periods,
        "our_profit_pct": profit_settings["our_pct"],
        "partner_profit_pct": profit_settings["partner_pct"],
        "partner_name": profit_settings["partner_name"],
        "profit_split_configured": profit_settings["configured"],
        "last_synced": last_synced,
        "total_rows": len(values),
    }


def dashboard_summaries(
    *,
    today: date | None = None,
    selected_from: date | None = None,
    selected_to: date | None = None,
    timezone_name: str = DEFAULT_DASHBOARD_TIMEZONE,
) -> list[dict[str, Any]]:
    """Compatibility wrapper. Prefer ``dashboard_snapshot`` for new callers."""
    return list(
        dashboard_snapshot(
            today=today,
            selected_from=selected_from,
            selected_to=selected_to,
            timezone_name=timezone_name,
        )["summaries"]
    )

def combined_dashboard_period(summaries: Iterable[Mapping[str, Any]], period_key: str) -> dict[str, float | int]:
    """Combine one period across sellers for the headline metrics."""
    values = [item.get("periods", {}).get(period_key, {}) for item in summaries]
    return {
        "sales": round(sum(float(item.get("sales") or 0) for item in values), 2),
        "profit": round(sum(float(item.get("profit") or 0) for item in values), 2),
        "our_amount": round(sum(float(item.get("our_amount") or 0) for item in values), 2),
        "partner_amount": round(sum(float(item.get("partner_amount") or 0) for item in values), 2),
        "orders": sum(int(item.get("orders") or 0) for item in values),
        "missing_profit_rows": sum(int(item.get("missing_profit_rows") or 0) for item in values),
    }


def ensure_dashboard_sync_schema() -> None:
    with connect() as con:
        con.execute(
            """CREATE TABLE IF NOT EXISTS dashboard_sync_state (
                id INTEGER PRIMARY KEY CHECK(id=1),
                last_started_at TEXT NOT NULL DEFAULT '',
                last_completed_at TEXT NOT NULL DEFAULT '',
                last_error TEXT NOT NULL DEFAULT '',
                rows_saved INTEGER NOT NULL DEFAULT 0,
                accounts_synced INTEGER NOT NULL DEFAULT 0
            )"""
        )
        con.execute("INSERT INTO dashboard_sync_state(id) VALUES(1) ON CONFLICT DO NOTHING")


def dashboard_sync_state() -> dict[str, Any]:
    ensure_dashboard_sync_schema()
    result = rows("SELECT * FROM dashboard_sync_state WHERE id=1")
    return result[0] if result else {}


def _parse_utc(value: Any) -> datetime | None:
    text = clean_text(value)
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    except ValueError:
        return None


def dashboard_sync_in_progress(
    state: Mapping[str, Any] | None = None,
    *,
    now: datetime | None = None,
    stale_after_seconds: int = DEFAULT_SYNC_STALE_SECONDS,
) -> bool:
    """Return True only while a recent synchronization lease is active.

    Old v131 runs could remain marked as started if the page was closed while an API
    call was blocked. Those leases become stale automatically and never keep the new
    Dashboard locked forever.
    """
    current = dict(state or dashboard_sync_state())
    started = _parse_utc(current.get("last_started_at"))
    completed = _parse_utc(current.get("last_completed_at"))
    if not started or (completed and completed >= started):
        return False
    reference_now = now or datetime.now(timezone.utc)
    return (reference_now - started).total_seconds() < max(60, int(stale_after_seconds))


def _claim_dashboard_sync(*, interval_seconds: int, force: bool) -> bool:
    """Acquire a database lease without ever waiting for marketplace APIs."""
    ensure_dashboard_sync_schema()
    now = datetime.now(timezone.utc)
    with connect() as con:
        current = con.execute("SELECT * FROM dashboard_sync_state WHERE id=1").fetchone()
        current_dict = dict(current) if current else {}
        last_started = _parse_utc(current_dict.get("last_started_at"))
        last_completed = _parse_utc(current_dict.get("last_completed_at"))

        # Force bypasses only the five-minute interval; it never launches a second
        # synchronization while a recent one is already active.
        if dashboard_sync_in_progress(current_dict, now=now):
            return False
        if not force:
            reference = max(
                (value for value in (last_started, last_completed) if value is not None),
                default=None,
            )
            if reference and (now - reference).total_seconds() < max(60, interval_seconds):
                return False

        con.execute(
            """UPDATE dashboard_sync_state
            SET last_started_at=?,last_error='',rows_saved=0,accounts_synced=0
            WHERE id=1""",
            (now.isoformat(timespec="seconds"),),
        )
    return True


def _finish_dashboard_sync(
    *,
    total_saved: int,
    accounts_synced: int,
    errors: Iterable[str] = (),
) -> dict[str, Any]:
    completed = datetime.now(timezone.utc).isoformat(timespec="seconds")
    error_text = " | ".join(clean_text(item) for item in errors if clean_text(item))
    with connect() as con:
        con.execute(
            """UPDATE dashboard_sync_state
            SET last_completed_at=?,last_error=?,rows_saved=?,accounts_synced=?
            WHERE id=1""",
            (completed, error_text, int(total_saved), int(accounts_synced)),
        )
    return {
        "ran": True,
        "skipped": False,
        "last_completed_at": completed,
        "last_error": error_text,
        "rows_saved": int(total_saved),
        "accounts_synced": int(accounts_synced),
    }


def _run_claimed_dashboard_sync(
    *,
    lookback_days: int,
    timezone_name: str,
    max_rows_per_account: int,
) -> dict[str, Any]:
    """Execute a lightweight recent-order synchronization after a lease is held."""
    target_timezone = _dashboard_timezone(timezone_name)
    today = datetime.now(target_timezone).date()
    date_from = today - timedelta(days=max(1, int(lookback_days)))
    account_rows = rows(
        """SELECT * FROM marketplace_accounts
        WHERE active=1 AND marketplace IN ('kaufland','worten')
        ORDER BY seller_id,marketplace,id"""
    )
    total_saved = 0
    accounts_synced = 0
    errors: list[str] = []
    safe_max_rows = max(50, min(1000, int(max_rows_per_account)))

    try:
        for account in account_rows:
            marketplace = clean_text(account.get("marketplace")).lower()
            seller_id = int(account.get("seller_id") or 0)
            account_id = int(account.get("id") or 0)
            label = clean_text(account.get("account_name")) or f"Account {account_id}"
            try:
                credentials = decrypt_dict(clean_text(account.get("credentials_encrypted")))
                if marketplace == "kaufland":
                    fresh = fetch_kaufland_accounting_orders(
                        credentials,
                        account_id=account_id,
                        seller_id=seller_id,
                        date_from=date_from,
                        date_to=today,
                        max_rows=safe_max_rows,
                        include_order_details=False,
                        request_timeout=12,
                        max_attempts=2,
                    )
                else:
                    fresh = fetch_worten_accounting_orders(
                        credentials,
                        account_id=account_id,
                        seller_id=seller_id,
                        date_from=date_from,
                        date_to=today,
                        max_rows=safe_max_rows,
                        request_timeout=20,
                    )
                total_saved += upsert_accounting_rows(
                    seller_id, account_id, marketplace, fresh
                )
                accounts_synced += 1
            except Exception as exc:  # one account must not block the others
                errors.append(f"{marketplace.title()} · {label}: {exc}")
        return _finish_dashboard_sync(
            total_saved=total_saved,
            accounts_synced=accounts_synced,
            errors=errors,
        )
    except BaseException as exc:
        errors.append(f"Sincronizzazione interrotta: {exc}")
        return _finish_dashboard_sync(
            total_saved=total_saved,
            accounts_synced=accounts_synced,
            errors=errors,
        )


def sync_dashboard_orders(
    *,
    force: bool = False,
    interval_seconds: int = DEFAULT_AUTO_SYNC_SECONDS,
    lookback_days: int = DEFAULT_AUTO_SYNC_DAYS,
    timezone_name: str = DEFAULT_DASHBOARD_TIMEZONE,
    max_rows_per_account: int = DEFAULT_AUTO_SYNC_MAX_ROWS,
) -> dict[str, Any]:
    """Synchronize recent orders synchronously.

    This remains available for tests and maintenance commands. The Streamlit
    Dashboard uses :func:`start_dashboard_sync_background` so page rendering is
    never held hostage by a marketplace request.
    """
    if not _claim_dashboard_sync(interval_seconds=interval_seconds, force=force):
        state = dashboard_sync_state()
        return {"ran": False, "skipped": True, **state}
    return _run_claimed_dashboard_sync(
        lookback_days=lookback_days,
        timezone_name=timezone_name,
        max_rows_per_account=max_rows_per_account,
    )


def _background_sync_target(
    *,
    lookback_days: int,
    timezone_name: str,
    max_rows_per_account: int,
) -> None:
    try:
        _run_claimed_dashboard_sync(
            lookback_days=lookback_days,
            timezone_name=timezone_name,
            max_rows_per_account=max_rows_per_account,
        )
    finally:
        global _SYNC_THREAD
        with _SYNC_THREAD_LOCK:
            _SYNC_THREAD = None


def start_dashboard_sync_background(
    *,
    force: bool = False,
    interval_seconds: int = DEFAULT_AUTO_SYNC_SECONDS,
    lookback_days: int = DEFAULT_AUTO_SYNC_DAYS,
    timezone_name: str = DEFAULT_DASHBOARD_TIMEZONE,
    max_rows_per_account: int = DEFAULT_AUTO_SYNC_MAX_ROWS,
) -> dict[str, Any]:
    """Launch API polling in a daemon thread and return immediately.

    The database lease also protects installations served by multiple Streamlit
    sessions or processes. A stale lease from an interrupted old version is ignored.
    """
    global _SYNC_THREAD
    with _SYNC_THREAD_LOCK:
        if _SYNC_THREAD is not None and _SYNC_THREAD.is_alive():
            return {"started": False, "reason": "running", **dashboard_sync_state()}
        if not _claim_dashboard_sync(interval_seconds=interval_seconds, force=force):
            state = dashboard_sync_state()
            reason = "running" if dashboard_sync_in_progress(state) else "not_due"
            return {"started": False, "reason": reason, **state}
        try:
            _SYNC_THREAD = threading.Thread(
                target=_background_sync_target,
                kwargs={
                    "lookback_days": lookback_days,
                    "timezone_name": timezone_name,
                    "max_rows_per_account": max_rows_per_account,
                },
                name="marketplace-dashboard-sync",
                daemon=True,
            )
            _SYNC_THREAD.start()
        except Exception as exc:
            _SYNC_THREAD = None
            result = _finish_dashboard_sync(
                total_saved=0,
                accounts_synced=0,
                errors=[f"Impossibile avviare la sincronizzazione in background: {exc}"],
            )
            return {"started": False, "reason": "start_error", **result}
    return {"started": True, "reason": "started", **dashboard_sync_state()}

