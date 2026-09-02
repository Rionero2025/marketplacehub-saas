from __future__ import annotations

import base64
import hashlib
import json
import os

from cryptography.fernet import Fernet, InvalidToken

_RUNTIME_MASTER_KEY = ""


def _fernet_from_master(master: str) -> Fernet:
    master = str(master or "").strip()
    if not master:
        raise RuntimeError("Inserisci una chiave master valida.")
    key = base64.urlsafe_b64encode(hashlib.sha256(master.encode("utf-8")).digest())
    return Fernet(key)


def active_master_key() -> str:
    """Return the in-memory override first, then the configured environment key."""
    runtime = str(_RUNTIME_MASTER_KEY or "").strip()
    if runtime:
        return runtime
    return os.getenv("MARKETPLACE_HUB_MASTER_KEY", "").strip()


def runtime_master_key_active() -> bool:
    return bool(str(_RUNTIME_MASTER_KEY or "").strip())


def set_runtime_master_key(master: str) -> None:
    """Use a master key in process memory only; never persist or log it."""
    value = str(master or "").strip()
    if not value:
        raise RuntimeError("Inserisci la chiave master.")
    global _RUNTIME_MASTER_KEY
    _RUNTIME_MASTER_KEY = value


def clear_runtime_master_key() -> None:
    global _RUNTIME_MASTER_KEY
    _RUNTIME_MASTER_KEY = ""


def validate_master_key(master: str, encrypted_values) -> tuple[bool, int]:
    """Validate a candidate against encrypted JSON payloads without changing state."""
    candidate = _fernet_from_master(master)
    checked = 0
    for encrypted in encrypted_values:
        value = str(encrypted or "").strip()
        if not value:
            continue
        try:
            raw = candidate.decrypt(value.encode("ascii")).decode("utf-8")
            decoded = json.loads(raw)
            if not isinstance(decoded, dict):
                return False, checked
            checked += 1
        except (InvalidToken, UnicodeDecodeError, json.JSONDecodeError, ValueError):
            return False, checked
    return checked > 0, checked


def _fernet() -> Fernet:
    master = active_master_key()
    if not master:
        raise RuntimeError("Configura MARKETPLACE_HUB_MASTER_KEY nei secrets o nelle variabili d'ambiente.")
    return _fernet_from_master(master)


def encrypt_dict(value: dict) -> str:
    raw = json.dumps(value, ensure_ascii=False).encode("utf-8")
    return _fernet().encrypt(raw).decode("ascii")


def decrypt_dict(value: str) -> dict:
    if not value:
        return {}
    try:
        return json.loads(_fernet().decrypt(value.encode("ascii")).decode("utf-8"))
    except InvalidToken as exc:
        raise RuntimeError("Chiave master errata: impossibile decifrare le credenziali.") from exc


def masked(value: str) -> str:
    value = str(value or "")
    return "••••••••" + value[-4:] if value else "—"
