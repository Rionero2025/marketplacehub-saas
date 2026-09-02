from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from typing import Any


def _text(value: Any) -> str:
    return str(value or "").strip()


def _is_not_found(error: BaseException) -> bool:
    message = _text(error).lower()
    return "http 404" in message or "not found" in message or "nicht gefunden" in message


@dataclass
class BulkDeleteSummary:
    total: int = 0
    processed: int = 0
    deleted: int = 0
    already_absent: int = 0
    failed: int = 0
    errors: list[dict[str, Any]] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "total": self.total,
            "processed": self.processed,
            "deleted": self.deleted,
            "already_absent": self.already_absent,
            "failed": self.failed,
            "errors": list(self.errors),
        }


def normalize_delete_targets(units: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Return unique Kaufland deletion targets.

    The same unit can appear more than once when the caller merges storefront pages
    or local cache rows.  Deleting it twice would waste API quota and create noisy
    404 responses, therefore targets are deduplicated by storefront and id_unit.
    """
    seen: set[tuple[str, str]] = set()
    result: list[dict[str, Any]] = []
    for raw in units:
        item = dict(raw)
        storefront = _text(item.get("storefront")).lower()
        id_unit = _text(item.get("id_unit"))
        if not storefront or not id_unit:
            continue
        key = (storefront, id_unit)
        if key in seen:
            continue
        seen.add(key)
        result.append(
            {
                "storefront": storefront,
                "id_unit": id_unit,
                "id_offer": _text(item.get("id_offer") or item.get("sku")),
                "ean": _text(item.get("ean")),
            }
        )
    return result


def delete_units_fast(
    client: Any,
    units: Iterable[Mapping[str, Any]],
    *,
    max_workers: int = 5,
    requests_per_second: float = 8.0,
    max_error_rows: int = 250,
    progress: Callable[[BulkDeleteSummary], None] | None = None,
) -> BulkDeleteSummary:
    """Delete many units with bounded parallelism and compact results.

    Kaufland has no REST endpoint that deletes the complete inventory in one
    request.  This helper exposes a one-click operation to the UI while sending
    the required ``DELETE /units/{id_unit}/`` calls with a seller-wide rate limit.
    Only failures are retained in memory/database; successful rows are counted.
    Running the operation again is safe because already absent units are treated
    as completed.
    """
    targets = normalize_delete_targets(units)
    summary = BulkDeleteSummary(total=len(targets))
    if not targets:
        if progress:
            progress(summary)
        return summary

    workers = max(1, min(8, int(max_workers or 1)))
    requested_rate = max(1.0, min(12.0, float(requests_per_second or 1.0)))
    previous_rate = getattr(client, "requests_per_second", None)
    if previous_rate is not None:
        try:
            client.requests_per_second = min(float(previous_rate), requested_rate)
        except Exception:
            client.requests_per_second = requested_rate

    def delete_one(target: Mapping[str, Any]) -> tuple[str, dict[str, Any], str]:
        try:
            client.delete_unit(target["id_unit"], target["storefront"])
            return "deleted", dict(target), ""
        except Exception as error:  # API/network errors are reported per unit.
            if _is_not_found(error):
                return "absent", dict(target), ""
            return "failed", dict(target), _text(error)

    try:
        with ThreadPoolExecutor(max_workers=workers) as executor:
            futures = [executor.submit(delete_one, target) for target in targets]
            for future in as_completed(futures):
                status, target, error = future.result()
                summary.processed += 1
                if status == "deleted":
                    summary.deleted += 1
                elif status == "absent":
                    summary.already_absent += 1
                else:
                    summary.failed += 1
                    if len(summary.errors) < max(0, int(max_error_rows)):
                        summary.errors.append(
                            {
                                "storefront": target.get("storefront"),
                                "id_unit": target.get("id_unit"),
                                "id_offer": target.get("id_offer"),
                                "ean": target.get("ean"),
                                "error": error,
                            }
                        )
                if progress:
                    progress(summary)
    finally:
        if previous_rate is not None:
            try:
                client.requests_per_second = previous_rate
            except Exception:
                pass

    return summary
