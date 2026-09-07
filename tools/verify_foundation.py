from __future__ import annotations

import json

from fastapi.testclient import TestClient
from marketplace_hub_api.main import create_app
from marketplace_hub_core.settings import Settings
from marketplace_hub_worker.jobs import foundation_probe


def main() -> None:
    app = create_app(
        settings=Settings(environment="test"),
        readiness_checks={"database": lambda: None, "redis": lambda: None},
    )
    client = TestClient(app)
    live = client.get("/health/live")
    ready = client.get("/health/ready")
    assert live.status_code == 200
    assert ready.status_code == 200
    print(json.dumps({"api": ready.json(), "worker": foundation_probe()}, indent=2))


if __name__ == "__main__":
    main()
