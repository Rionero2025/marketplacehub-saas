from datetime import datetime

from marketplace_hub_worker.jobs import foundation_probe


def test_worker_probe_returns_a_timezone_aware_completion() -> None:
    result = foundation_probe()

    assert result["status"] == "ok"
    assert datetime.fromisoformat(result["completed_at"]).tzinfo is not None
