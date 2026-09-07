from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from uuid import UUID


class AuthRealm(StrEnum):
    SELLER = "seller"
    AGENCY = "agency"
    PLATFORM = "platform"


@dataclass(frozen=True, slots=True)
class AuthUser:
    id: UUID
    login: str
    display_name: str
    password_hash: str
    active: bool
    realms: frozenset[AuthRealm]


@dataclass(frozen=True, slots=True)
class StoredSession:
    id: UUID
    user_id: UUID
    token_hash: str
    created_at: datetime
    expires_at: datetime
    revoked_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class AuthenticatedSession:
    session_id: UUID
    user_id: UUID
    login: str
    display_name: str
    realm: AuthRealm
    expires_at: datetime


@dataclass(frozen=True, slots=True)
class IssuedSession:
    token: str
    principal: AuthenticatedSession
