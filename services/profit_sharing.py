from __future__ import annotations

import math
from typing import Any, Mapping

DEFAULT_OUR_PROFIT_PCT = 0.0
DEFAULT_PARTNER_PROFIT_PCT = 100.0


def _finite(value: Any, default: float) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return number if math.isfinite(number) else default


def clamp_percentage(value: Any, default: float = 0.0) -> float:
    return round(min(100.0, max(0.0, _finite(value, default))), 4)


def normalized_percentages(our_pct: Any, partner_pct: Any) -> tuple[float, float]:
    """Return a valid 100% profit split.

    The management page prevents invalid values. This normalization is also applied
    while reading older databases so a malformed legacy row can never duplicate or
    lose part of the margin in Dashboard/Contabilità totals.
    """
    our = clamp_percentage(our_pct, DEFAULT_OUR_PROFIT_PCT)
    partner = clamp_percentage(partner_pct, DEFAULT_PARTNER_PROFIT_PCT)
    if abs((our + partner) - 100.0) > 0.01:
        partner = round(100.0 - our, 4)
    return our, partner


def seller_profit_settings(seller: Mapping[str, Any] | None) -> dict[str, Any]:
    source = dict(seller or {})
    our_pct, partner_pct = normalized_percentages(
        source.get("our_profit_pct"), source.get("partner_profit_pct")
    )
    name = str(source.get("name") or "Partner").strip() or "Partner"
    return {
        "our_pct": our_pct,
        "partner_pct": partner_pct,
        "partner_name": name,
        "configured": our_pct > 0.0,
    }


def split_profit(amount: Any, our_pct: Any, partner_pct: Any) -> dict[str, float]:
    value = _finite(amount, 0.0)
    our, partner = normalized_percentages(our_pct, partner_pct)
    our_amount = round(value * our / 100.0, 2)
    # Assign the residual to the partner so rounded values always reconcile exactly.
    partner_amount = round(value - our_amount, 2)
    return {
        "profit": round(value, 2),
        "our_pct": our,
        "partner_pct": partner,
        "our_amount": our_amount,
        "partner_amount": partner_amount,
    }
