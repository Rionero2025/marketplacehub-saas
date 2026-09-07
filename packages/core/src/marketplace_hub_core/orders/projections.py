from decimal import Decimal, InvalidOperation


def number(value):
    try:
        result = Decimal(str(value))
        return result if result.is_finite() and abs(result) < Decimal("1e30") else None
    except (InvalidOperation, ValueError, TypeError):
        return None


def project_order(item):
    details = item.get("details") or {}
    return {
        "currency": str(item.get("currency") or "").strip(),
        "carrier": str(details.get("carrier") or "").strip(),
        "has_tracking": bool(str(details.get("tracking") or "").strip()),
        "has_commission": number(item.get("commission_amount")) is not None,
        "sale_eur": number(item.get("sale_amount_eur")),
        "commission_eur": number(item.get("commission_amount_eur")),
        "payout_eur": number(item.get("payout_amount_eur")),
        "purchase_eur": number(item.get("purchase_cost_eur", item.get("purchase_cost"))),
        "profit_eur": number(item.get("profit_amount_eur", item.get("profit_amount"))),
        "quantity": max(1, int(number(item.get("quantity")) or 1)),
        "excluded": str(item.get("status") or "").casefold() in {"cancelled", "canceled"}
        or bool(details.get("excluded_from_totals")),
        "catalog_cost": str(item.get("purchase_cost_source") or "").startswith(
            "Listino pubblicato"),
    }
