from __future__ import annotations

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError

_hasher = PasswordHasher(time_cost=3, memory_cost=65536, parallelism=4)


class WeakPasswordError(ValueError):
    pass


def validate_password(password: str) -> None:
    if len(password) < 12:
        raise WeakPasswordError("La password deve contenere almeno 12 caratteri.")
    if not any(character.isupper() for character in password):
        raise WeakPasswordError("La password deve contenere almeno una lettera maiuscola.")
    if not any(character.islower() for character in password):
        raise WeakPasswordError("La password deve contenere almeno una lettera minuscola.")
    if not any(character.isdigit() for character in password):
        raise WeakPasswordError("La password deve contenere almeno un numero.")


def hash_password(password: str) -> str:
    validate_password(password)
    return _hasher.hash(password)


def verify_password(password_hash: str, password: str) -> bool:
    try:
        return _hasher.verify(password_hash, password)
    except (InvalidHashError, VerificationError, VerifyMismatchError):
        return False
