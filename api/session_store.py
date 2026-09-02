from __future__ import annotations

import hashlib
import os
import secrets
import time
from dataclasses import dataclass
from typing import Any

from services.db import connect, row
from services.user_access import get_user, permissions_from_record, seller_ids_from_record

_SCHEMA_READY = False


def _now() -> int:
    return int(time.time())


def _token_hash(token: str) -> str:
    return hashlib.sha256(str(token or "").encode("utf-8")).hexdigest()


def _ttl_seconds(remember: bool) -> int:
    if remember:
        days = max(1, int(os.getenv("MARKETPLACE_HUB_API_REMEMBER_DAYS", "30")))
        return days * 24 * 60 * 60
    hours = max(1, int(os.getenv("MARKETPLACE_HUB_API_SESSION_HOURS", "12")))
    return hours * 60 * 60


@dataclass(frozen=True, slots=True)
class ApiSession:
    token: str
    user_id: int
    expires_at: int


def ensure_api_session_schema() -> None:
    global _SCHEMA_READY
    if _SCHEMA_READY:
        return
    with connect() as con:
        con.executescript(
            """
            CREATE TABLE IF NOT EXISTS api_sessions (
                token_hash TEXT PRIMARY KEY,
                user_id INTEGER NOT NULL,
                created_at_epoch INTEGER NOT NULL,
                expires_at_epoch INTEGER NOT NULL,
                revoked_at_epoch INTEGER NOT NULL DEFAULT 0,
                last_seen_at_epoch INTEGER NOT NULL DEFAULT 0
            );
            CREATE INDEX IF NOT EXISTS idx_api_sessions_user
            ON api_sessions(user_id,expires_at_epoch);
            CREATE INDEX IF NOT EXISTS idx_api_sessions_expiry
            ON api_sessions(expires_at_epoch,revoked_at_epoch);
            """
        )
    _SCHEMA_READY = True


def cleanup_sessions() -> None:
    ensure_api_session_schema()
    cutoff = _now()
    with connect() as con:
        con.execute(
            "DELETE FROM api_sessions WHERE expires_at_epoch<? OR revoked_at_epoch>0",
            (cutoff,),
        )


def issue_session(user_id: int, *, remember: bool = False) -> ApiSession:
    ensure_api_session_schema()
    user_id = int(user_id)
    if user_id <= 0:
        raise ValueError("user_id non valido")
    token = secrets.token_urlsafe(48)
    created = _now()
    expires = created + _ttl_seconds(bool(remember))
    with connect() as con:
        con.execute(
            """INSERT INTO api_sessions(
                token_hash,user_id,created_at_epoch,expires_at_epoch,last_seen_at_epoch
            ) VALUES(?,?,?,?,?)""",
            (_token_hash(token), user_id, created, expires, created),
        )
    # Bounded cleanup; no user-visible side effect if an old session remains.
    try:
        cleanup_sessions()
    except Exception:
        pass
    return ApiSession(token=token, user_id=user_id, expires_at=expires)


def revoke_session(token: str) -> bool:
    ensure_api_session_schema()
    token = str(token or "").strip()
    if not token:
        return False
    with connect() as con:
        cur = con.execute(
            "UPDATE api_sessions SET revoked_at_epoch=? WHERE token_hash=? AND revoked_at_epoch=0",
            (_now(), _token_hash(token)),
        )
        return bool(int(getattr(cur, "rowcount", 0) or 0))


def session_user(token: str) -> dict[str, Any] | None:
    ensure_api_session_schema()
    token = str(token or "").strip()
    if not token:
        return None
    now = _now()
    session = row(
        """SELECT user_id,expires_at_epoch,last_seen_at_epoch
           FROM api_sessions
           WHERE token_hash=? AND revoked_at_epoch=0 AND expires_at_epoch>?""",
        (_token_hash(token), now),
    )
    if not session:
        return None
    record = get_user(int(session.get("user_id") or 0))
    if not record or int(record.get("active") or 0) != 1:
        try:
            revoke_session(token)
        except Exception:
            pass
        return None
    # Avoid a DB write on every API call.
    last_seen = int(session.get("last_seen_at_epoch") or 0)
    if now - last_seen >= 60:
        try:
            with connect() as con:
                con.execute(
                    "UPDATE api_sessions SET last_seen_at_epoch=? WHERE token_hash=?",
                    (now, _token_hash(token)),
                )
        except Exception:
            pass
    is_admin = bool(int(record.get("is_admin") or 0))
    return {
        "id": int(record.get("id") or 0),
        "username": str(record.get("username") or ""),
        "display_name": str(record.get("display_name") or ""),
        "is_admin": is_admin,
        "permissions": permissions_from_record(record),
        "seller_ids": None if is_admin else seller_ids_from_record(record),
        "expires_at": int(session.get("expires_at_epoch") or 0),
    }
