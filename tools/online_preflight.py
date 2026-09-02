from __future__ import annotations

import os
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from services.database_config import database_config_public, database_engine
from services.db import database_storage_status, init_db


def truthy(value: str | None) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def fail(message: str) -> None:
    print(f"[ONLINE PREFLIGHT] ERROR: {message}", file=sys.stderr, flush=True)
    raise SystemExit(1)


def main() -> None:
    print("[ONLINE PREFLIGHT] Marketplace Hub startup check", flush=True)

    if truthy(os.getenv("MARKETPLACE_HUB_REQUIRE_AUTH")):
        if not os.getenv("MARKETPLACE_HUB_ADMIN_USERNAME"):
            fail("MARKETPLACE_HUB_ADMIN_USERNAME mancante")
        if not os.getenv("MARKETPLACE_HUB_ADMIN_PASSWORD"):
            fail("MARKETPLACE_HUB_ADMIN_PASSWORD mancante")

    if not str(os.getenv("MARKETPLACE_HUB_MASTER_KEY") or "").strip():
        fail("MARKETPLACE_HUB_MASTER_KEY mancante")

    if database_engine() != "postgresql":
        fail("Il deploy online deve usare PostgreSQL")

    public = database_config_public()
    print(
        "[ONLINE PREFLIGHT] PostgreSQL "
        f"host={public['postgresql_host']} db={public['postgresql_database']} "
        f"user={public['postgresql_user']} sslmode={public['postgresql_sslmode']}",
        flush=True,
    )
    init_db()
    status = database_storage_status()
    if not status.get("ok"):
        fail(f"Connessione database non valida: {status.get('error') or status}")
    print("[ONLINE PREFLIGHT] Database pronto", flush=True)


if __name__ == "__main__":
    main()
