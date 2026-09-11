from datetime import UTC, datetime, timedelta

import pytest
from marketplace_hub_core.catalogs.progress import forecast_job

NOW = datetime(2026, 9, 11, 16, tzinfo=UTC)


def job(**values):
    return {
        "id": "current", "organization_id": "org", "seller_id": "seller",
        "price_list_id": "list", "source_config_revision": 1, "status": "running",
        "processed_bytes": 50_000_000, "total_bytes": 100_000_000,
        "created_at": NOW - timedelta(seconds=150),
        "started_at": NOW - timedelta(seconds=100), "updated_at": NOW,
        "message": "Download del listino in corso.", **values,
    }


def test_download_forecast_changes_with_measured_work_and_speed():
    first = forecast_job(job(), now=NOW)
    assert first["percent"] == 35
    assert first["remaining_seconds"] == 186
    faster = forecast_job(job(processed_bytes=75_000_000), now=NOW)
    assert faster["percent"] == 52
    assert faster["remaining_seconds"] < first["remaining_seconds"]
    slower = forecast_job(job(started_at=NOW - timedelta(seconds=200)), now=NOW)
    assert slower["remaining_seconds"] > first["remaining_seconds"]


def test_unknown_size_uses_only_prior_same_configuration_sizes():
    current = job(total_bytes=None)
    old = job(id="old", created_at=NOW - timedelta(days=1))
    estimate = forecast_job(current, [old], now=NOW)
    assert estimate["percent"] == 35
    assert estimate["basis"] == "previous-size"
    for key in ["organization_id", "seller_id", "price_list_id", "source_config_revision"]:
        foreign = {**old, key: "different"}
        missing = forecast_job(current, [foreign], now=NOW)
        assert missing["state"] == "estimating"
        assert missing["remaining_seconds"] is None
    assert forecast_job(current, [job()], now=NOW)["basis"] == "learning"
    larger = forecast_job({**current, "processed_bytes": 110_000_000}, [old], now=NOW)
    assert larger["percent"] == 69
    assert larger["state"] == "recalculating"
    assert larger["remaining_seconds"] is None


@pytest.mark.parametrize(("message", "minimum", "maximum"), [
    ("Elaborazione prodotti in corso.", 70, 89),
    ("Salvataggio prodotti in corso.", 90, 99),
])
def test_phase_forecasts_never_announce_success_or_negative_time(message, minimum, maximum):
    row = job(message=message, updated_at=NOW - timedelta(seconds=5))
    first = forecast_job(row, now=NOW)
    assert minimum <= first["percent"] <= maximum
    assert first["remaining_seconds"] > 0
    overdue = forecast_job(row, now=NOW + timedelta(hours=1))
    assert overdue["percent"] == maximum
    assert overdue["state"] == "recalculating"
    assert overdue["remaining_seconds"] is None


def test_stale_and_terminal_jobs_do_not_show_a_false_countdown():
    stalled = forecast_job(job(updated_at=NOW - timedelta(seconds=31)), now=NOW)
    assert stalled["state"] == "stalled"
    assert stalled["remaining_seconds"] is None
    done = forecast_job(job(status="done"), now=NOW)
    assert done["percent"] == 100 and done["remaining_seconds"] == 0
    error = forecast_job(job(status="error"), now=NOW)
    assert error["percent"] is None and error["remaining_seconds"] is None
    waiting = forecast_job(job(status="queued", started_at=None), now=NOW)
    assert waiting["percent"] == 0 and waiting["remaining_seconds"] is None
