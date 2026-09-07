import base64
import hashlib
import json

from cryptography.fernet import Fernet, InvalidToken
from pydantic import SecretStr

from marketplace_hub_core.seller_settings.security import CredentialStorageUnavailableError


def decrypt_credentials(encrypted: str, master_key: SecretStr) -> dict[str, str]:
    master = master_key.get_secret_value().strip()
    if not master:
        raise CredentialStorageUnavailableError("Credenziali temporaneamente non disponibili.")
    key = base64.urlsafe_b64encode(hashlib.sha256(master.encode("utf-8")).digest())
    try:
        value = json.loads(Fernet(key).decrypt(encrypted.encode("ascii")).decode("utf-8"))
        if not isinstance(value, dict) or any(
            not isinstance(k, str) or not isinstance(v, str) for k, v in value.items()
        ):
            raise ValueError
        return value
    except (InvalidToken, UnicodeError, ValueError, AttributeError):
        raise CredentialStorageUnavailableError(
            "Credenziali temporaneamente non disponibili."
        ) from None


def credential_mask(marketplace: str, encrypted: str, master_key: SecretStr) -> str:
    try:
        value = decrypt_credentials(encrypted, master_key)
    except CredentialStorageUnavailableError:
        return "—"
    key = value.get("client_key" if marketplace == "kaufland" else "api_key", "")
    return "••••••••" + key[-4:] if key else "—"
