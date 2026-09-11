"""Explicit estimates, never completion signals: download 70%, parse 20%, save 10%."""

from datetime import UTC, datetime
from math import ceil
from statistics import median


def _seconds(end, start):
    if end is None or start is None:
        return 0.0
    end = end.astimezone(UTC) if end.tzinfo else end.replace(tzinfo=UTC)
    start = start.astimezone(UTC) if start.tzinfo else start.replace(tzinfo=UTC)
    return max(0.0, (end - start).total_seconds())


def forecast_job(row, history=(), *, now=None):
    now = now or datetime.now(UTC)

    def result(percent, seconds=None, state="available", basis="phase"):
        return {
            "percent": percent,
            "remaining_seconds": max(1, ceil(seconds)) if seconds is not None else None,
            "state": state,
            "basis": basis,
        }

    if row["status"] == "done":
        return {**result(100, state="done"), "remaining_seconds": 0}
    if row["status"] == "error":
        return result(None, state="error")
    if row["status"] != "running":
        return result(0, state="waiting")
    started = row.get("started_at")
    elapsed = _seconds(now, started)
    message = row.get("message")
    if message in {"Elaborazione prodotti in corso.", "Salvataggio prodotti in corso."}:
        saving = message == "Salvataggio prodotti in corso."
        base, weight = (90, 10) if saving else (70, 20)
        phase_elapsed = _seconds(now, row["updated_at"])
        previous_elapsed = _seconds(row["updated_at"], started)
        # Use the observed time of the finished phases to calibrate the remaining work.
        budget = previous_elapsed * weight / base
        if budget < 1:
            return result(base, state="estimating")
        fraction = min(0.99, phase_elapsed / budget)
        percent = min(99, int(base + weight * fraction))
        if phase_elapsed >= budget:
            return result(percent, state="recalculating")
        remaining = budget - phase_elapsed + (0 if saving else previous_elapsed * 10 / base)
        return result(percent, remaining)

    processed = int(row["processed_bytes"])
    total = row.get("total_bytes")
    basis = "measured-size"
    if not total:
        # Only prior attempts at this exact Seller/list/configuration can inform size.
        sizes = [int(old["total_bytes"]) for old in history if old.get("total_bytes")
                 and old["id"] != row["id"]
                 and old["created_at"] < row["created_at"]
                 and all(old[key] == row[key] for key in (
                     "organization_id", "seller_id", "price_list_id", "source_config_revision"
                 ))][:5]
        total = median(sizes) if sizes else None
        basis = "previous-size"
    if not total:
        return result(0, state="estimating", basis="learning")
    fraction = processed / total
    percent = min(69, int(70 * fraction))
    if _seconds(now, row["updated_at"]) > 30:
        return result(percent, state="stalled", basis=basis)
    if fraction >= 1:
        return result(percent, state="recalculating", basis=basis)
    if elapsed < 2 or processed <= 0:
        return result(percent, state="estimating", basis=basis)
    expected_duration = elapsed / fraction / 0.7
    return result(percent, expected_duration - elapsed, basis=basis)
