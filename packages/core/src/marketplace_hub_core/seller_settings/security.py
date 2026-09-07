import base64
import hashlib
import json

from cryptography.fernet import Fernet, InvalidToken
from pydantic import SecretStr


class CredentialStorageUnavailableError(RuntimeError):
    pass


def encrypt_credentials(credentials: dict[str, str], master_key: SecretStr) -> str:
    """Keep the original SHA256 master-key derivation and Fernet JSON format."""
    master = master_key.get_secret_value().strip()
    if not master:
        raise CredentialStorageUnavailableError(
            "Salvataggio credenziali temporaneamente non disponibile."
        )
    key = base64.urlsafe_b64encode(hashlib.sha256(master.encode("utf-8")).digest())
    raw = json.dumps(credentials, ensure_ascii=False).encode("utf-8")
    return Fernet(key).encrypt(raw).decode("ascii")


def masked_client_key(encrypted: str, master_key: SecretStr) -> str:
    """Only expose the original eight-bullet/final-four display, never raw keys."""
    master = master_key.get_secret_value().strip()
    if not encrypted or not master:
        return "—"
    key = base64.urlsafe_b64encode(hashlib.sha256(master.encode("utf-8")).digest())
    try:
        values = json.loads(Fernet(key).decrypt(encrypted.encode("ascii")).decode("utf-8"))
        client_key = str(values.get("client_key") or "")
    except (InvalidToken, UnicodeError, ValueError, AttributeError):
        return "—"
    return "••••••••" + client_key[-4:] if client_key else "—"
