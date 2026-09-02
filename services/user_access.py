from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
from datetime import datetime, timezone
from typing import Iterable

from services.db import execute, row, rows
from services.shared_cache import cache_get_or_set

# Le chiavi sono stabili: vengono salvate nel DB e usate da app.py per costruire
# il menu consentito al singolo utente.
MENU_AREAS: tuple[tuple[str, str], ...] = (
    ("dashboard", "Dashboard"),
    ("seller_management", "Gestione Seller"),
    ("suppliers_lists", "Fornitori e Listini"),
    ("ai_provider", "Provider IA"),
    ("work_lists", "Lavora sui Listini"),
    ("product_creation", "Creazione Prodotti"),
    ("marketplace_publication", "Pubblicazione sui Marketplace"),
    ("buybox", "Controllo Buy Box"),
    ("marketplace_orders", "Ordini Marketplace"),
    ("top_products", "Prodotti più venduti"),
    ("cecotec_orders", "Creazione Ordini Cecotec"),
    ("innpro_orders", "Creazione Ordini INNPRO"),
    ("packlink", "Packlink PRO"),
    ("tracking", "Tracciabilità ordini"),
    ("accounting", "Contabilità"),
    ("support", "Ticket e messaggi"),
    ("marketplace_deletion", "Cancellazione dai Marketplace"),
    ("history", "Storico"),
    ("backup_transfer", "Backup e trasferimento"),
    ("database", "Database"),
)

ALL_MENU_KEYS = frozenset(key for key, _ in MENU_AREAS)
_SCHEMA_READY = False
_PASSWORD_ITERATIONS = 390_000


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def ensure_user_schema() -> None:
    """Create/upgrade the application-user table without touching existing users.

    ``seller_ids_json = null`` is intentionally used for users created by v291:
    it means "all sellers" until an administrator explicitly saves a restricted
    selection. This avoids locking out existing accounts during the upgrade.
    """
    global _SCHEMA_READY
    if _SCHEMA_READY:
        return
    execute(
        """
        CREATE TABLE IF NOT EXISTS app_users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT NOT NULL UNIQUE,
            display_name TEXT NOT NULL DEFAULT '',
            email TEXT NOT NULL DEFAULT '',
            password_hash TEXT NOT NULL,
            is_admin INTEGER NOT NULL DEFAULT 0,
            active INTEGER NOT NULL DEFAULT 1,
            permissions_json TEXT NOT NULL DEFAULT '[]',
            seller_ids_json TEXT NOT NULL DEFAULT 'null',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            last_login_at TEXT NOT NULL DEFAULT ''
        )
        """
    )
    # Upgrade v291 installations. A SELECT is portable across SQLite/PostgreSQL;
    # if the column doesn't exist, add it. Each db.execute call is transactional.
    try:
        row("SELECT seller_ids_json FROM app_users LIMIT 1")
    except Exception:
        execute("ALTER TABLE app_users ADD COLUMN seller_ids_json TEXT NOT NULL DEFAULT 'null'")
    try:
        row("SELECT email FROM app_users LIMIT 1")
    except Exception:
        execute("ALTER TABLE app_users ADD COLUMN email TEXT NOT NULL DEFAULT ''")
    _SCHEMA_READY = True


def normalize_username(value: str) -> str:
    return str(value or "").strip()


def _b64(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def _unb64(value: str) -> bytes:
    text = str(value or "")
    text += "=" * (-len(text) % 4)
    return base64.urlsafe_b64decode(text.encode("ascii"))


def hash_password(password: str) -> str:
    password = str(password or "")
    if len(password) < 8:
        raise ValueError("La password deve contenere almeno 8 caratteri.")
    salt = secrets.token_bytes(18)
    digest = hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), salt, _PASSWORD_ITERATIONS
    )
    return f"pbkdf2_sha256${_PASSWORD_ITERATIONS}${_b64(salt)}${_b64(digest)}"


def verify_password(password: str, encoded: str) -> bool:
    try:
        algorithm, iterations_text, salt_text, digest_text = str(encoded).split("$", 3)
        if algorithm != "pbkdf2_sha256":
            return False
        iterations = int(iterations_text)
        salt = _unb64(salt_text)
        expected = _unb64(digest_text)
        actual = hashlib.pbkdf2_hmac(
            "sha256", str(password or "").encode("utf-8"), salt, iterations
        )
        return hmac.compare_digest(actual, expected)
    except (ValueError, TypeError, base64.binascii.Error):
        return False


def normalize_permissions(values: Iterable[str] | None) -> list[str]:
    selected = {str(value) for value in (values or [])}
    return [key for key, _ in MENU_AREAS if key in selected]


def permissions_from_record(record: dict | None) -> list[str]:
    if not record:
        return []
    if int(record.get("is_admin") or 0) == 1:
        return list(ALL_MENU_KEYS)
    try:
        parsed = json.loads(record.get("permissions_json") or "[]")
    except (TypeError, ValueError, json.JSONDecodeError):
        parsed = []
    if not isinstance(parsed, list):
        parsed = []
    return normalize_permissions(parsed)


def normalize_seller_ids(values: Iterable[int | str] | None) -> list[int]:
    result: list[int] = []
    seen: set[int] = set()
    for value in values or []:
        try:
            seller_id = int(value)
        except (TypeError, ValueError):
            continue
        if seller_id <= 0 or seller_id in seen:
            continue
        seen.add(seller_id)
        result.append(seller_id)
    return result


def seller_ids_from_record(record: dict | None) -> list[int] | None:
    """Return explicit seller IDs or ``None`` for unrestricted/all sellers."""
    if not record:
        return []
    if int(record.get("is_admin") or 0) == 1:
        return None
    raw = record.get("seller_ids_json")
    if raw in (None, "", "null"):
        return None  # legacy v291 user: keep previous all-seller behaviour
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError, json.JSONDecodeError):
        return []
    if parsed is None:
        return None
    if not isinstance(parsed, list):
        return []
    return normalize_seller_ids(parsed)


def get_user(user_id: int) -> dict | None:
    ensure_user_schema()
    user_id = int(user_id)

    def load() -> dict | None:
        found = row("SELECT * FROM app_users WHERE id=?", (user_id,))
        if found:
            found["permissions"] = permissions_from_record(found)
            found["seller_ids"] = seller_ids_from_record(found)
        return found

    # Shared across Streamlit sessions/worker processes when Redis is configured.
    # Writes to app_users invalidate the whole users namespace centrally.
    return cache_get_or_set("users", f"id:{user_id}", load, ttl_seconds=30)


def find_user(username: str) -> dict | None:
    ensure_user_schema()
    wanted = normalize_username(username).lower()
    if not wanted:
        return None
    found = row("SELECT * FROM app_users WHERE lower(username)=?", (wanted,))
    if found:
        found["permissions"] = permissions_from_record(found)
        found["seller_ids"] = seller_ids_from_record(found)
    return found


def list_users() -> list[dict]:
    ensure_user_schema()

    def load() -> list[dict]:
        result = rows("SELECT * FROM app_users ORDER BY lower(username),id")
        for item in result:
            item["permissions"] = permissions_from_record(item)
            item["seller_ids"] = seller_ids_from_record(item)
        return result

    return cache_get_or_set("users", "list", load, ttl_seconds=15)


def _seller_json(is_admin: bool, seller_ids: Iterable[int | str] | None) -> str:
    if is_admin:
        return "null"
    selected = normalize_seller_ids(seller_ids)
    if not selected:
        raise ValueError("Seleziona almeno un Seller visibile per questo utente.")
    return json.dumps(selected, separators=(",", ":"))


def create_user(
    username: str,
    password: str,
    *,
    display_name: str = "",
    email: str = "",
    permissions: Iterable[str] | None = None,
    seller_ids: Iterable[int | str] | None = None,
    is_admin: bool = False,
    active: bool = True,
) -> int:
    ensure_user_schema()
    username = normalize_username(username)
    if not username:
        raise ValueError("Inserisci lo username.")
    if find_user(username):
        raise ValueError("Esiste già un utente con questo username.")
    selected = list(ALL_MENU_KEYS) if is_admin else normalize_permissions(permissions)
    if not is_admin and not selected:
        raise ValueError("Seleziona almeno un'area del menu per questo utente.")
    sellers_json = _seller_json(is_admin, seller_ids)
    stamp = now_iso()
    return execute(
        """
        INSERT INTO app_users(
            username,display_name,email,password_hash,is_admin,active,permissions_json,
            seller_ids_json,created_at,updated_at,last_login_at
) VALUES(?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            username,
            str(display_name or "").strip(),
            str(email or "").strip().lower(),
            hash_password(password),
            1 if is_admin else 0,
            1 if active else 0,
            json.dumps(selected, ensure_ascii=False, separators=(",", ":")),
            sellers_json,
            stamp,
            stamp,
            "",
        ),
    )


def update_user(
    user_id: int,
    *,
    username: str,
    display_name: str = "",
    email: str = "",
    permissions: Iterable[str] | None = None,
    seller_ids: Iterable[int | str] | None = None,
    is_admin: bool = False,
    active: bool = True,
    new_password: str = "",
) -> None:
    ensure_user_schema()
    user_id = int(user_id)
    current = get_user(user_id)
    if not current:
        raise ValueError("Utente non trovato.")
    username = normalize_username(username)
    if not username:
        raise ValueError("Inserisci lo username.")
    duplicate = find_user(username)
    if duplicate and int(duplicate["id"]) != user_id:
        raise ValueError("Esiste già un utente con questo username.")
    selected = list(ALL_MENU_KEYS) if is_admin else normalize_permissions(permissions)
    if not is_admin and not selected:
        raise ValueError("Seleziona almeno un'area del menu per questo utente.")
    sellers_json = _seller_json(is_admin, seller_ids)
    stamp = now_iso()
    params = [
        username,
        str(display_name or "").strip(),
        str(email or current.get("email") or "").strip().lower(),
        1 if is_admin else 0,
        1 if active else 0,
        json.dumps(selected, ensure_ascii=False, separators=(",", ":")),
        sellers_json,
        stamp,
    ]
    sql = """
        UPDATE app_users
        SET username=?,display_name=?,email=?,is_admin=?,active=?,permissions_json=?,
            seller_ids_json=?,updated_at=?
    """
    if str(new_password or ""):
        sql += ", password_hash=?"
        params.append(hash_password(new_password))
    sql += " WHERE id=?"
    params.append(user_id)
    execute(sql, tuple(params))


def delete_user(user_id: int) -> None:
    ensure_user_schema()
    execute("DELETE FROM app_users WHERE id=?", (int(user_id),))


def authenticate_user(username: str, password: str) -> dict | None:
    found = find_user(username)
    if not found or int(found.get("active") or 0) != 1:
        return None
    if not verify_password(password, found.get("password_hash") or ""):
        return None
    stamp = now_iso()
    execute("UPDATE app_users SET last_login_at=? WHERE id=?", (stamp, found["id"]))
    found["last_login_at"] = stamp
    found["permissions"] = permissions_from_record(found)
    found["seller_ids"] = seller_ids_from_record(found)
    return found


def session_user_payload(record: dict, *, source: str = "database") -> dict:
    is_admin = bool(int(record.get("is_admin") or 0))
    permissions = list(ALL_MENU_KEYS) if is_admin else permissions_from_record(record)
    seller_ids = None if is_admin else seller_ids_from_record(record)
    return {
        "id": int(record.get("id") or 0),
        "username": str(record.get("username") or ""),
        "display_name": str(record.get("display_name") or ""),
        "is_admin": is_admin,
        "permissions": permissions,
        "seller_ids": seller_ids,
        "source": source,
    }


def environment_admin_payload(username: str) -> dict:
    return {
        "id": 0,
        "username": str(username or "admin"),
        "display_name": "Amministratore",
        "is_admin": True,
        "permissions": list(ALL_MENU_KEYS),
        "seller_ids": None,
        "source": "environment",
    }
