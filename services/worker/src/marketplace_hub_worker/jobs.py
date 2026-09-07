from __future__ import annotations

from datetime import UTC, datetime


def foundation_probe() -> dict[str, str]:
    """Small deterministic job used by readiness and deployment checks."""
    return {"status": "ok", "completed_at": datetime.now(UTC).isoformat()}
