"""Read normalization from Streamlit services/profit_sharing.py; no new formula."""

import math
from typing import Any


def _finite(value: Any, default: float) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return number if math.isfinite(number) else default


def clamp_percentage(value: Any, default: float = 0.0) -> float:
    return round(min(100.0, max(0.0, _finite(value, default))), 4)


def normalized_percentages(our_pct: Any, partner_pct: Any) -> tuple[float, float]:
    our = clamp_percentage(our_pct, 0.0)
    partner = clamp_percentage(partner_pct, 100.0)
    if abs((our + partner) - 100.0) > 0.01:
        partner = round(100.0 - our, 4)
    return our, partner
