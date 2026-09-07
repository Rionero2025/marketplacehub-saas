from __future__ import annotations

import re
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]


def test_compose_and_render_keep_web_api_worker_and_datastores_separate() -> None:
    compose = yaml.safe_load((ROOT / "compose.yaml").read_text(encoding="utf-8"))
    assert set(compose["services"]) == {
        "app-web",
        "marketing-web",
        "api",
        "worker",
        "postgres",
        "redis",
    }
    for service in ("app-web", "marketing-web", "api", "worker"):
        dockerfile = compose["services"][service]["build"]["dockerfile"]
        assert (ROOT / dockerfile).is_file()

    blueprint = yaml.safe_load((ROOT / "render.yaml").read_text(encoding="utf-8"))
    types = {service["type"] for service in blueprint["services"]}
    assert {"web", "worker", "keyvalue"}.issubset(types)
    assert blueprint["databases"][0]["postgresMajorVersion"] == "17"


def test_tracked_configuration_has_no_known_secret_shapes() -> None:
    candidates = [
        ROOT / ".env.example",
        ROOT / "compose.yaml",
        ROOT / "render.yaml",
        *ROOT.glob("Dockerfile.*"),
    ]
    forbidden = re.compile(r"(?:sk-[A-Za-z0-9_-]{20,}|-----BEGIN (?:RSA |EC )?PRIVATE KEY-----)")
    for path in candidates:
        assert forbidden.search(path.read_text(encoding="utf-8")) is None, path
