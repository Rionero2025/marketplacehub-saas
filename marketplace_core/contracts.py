from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol


@dataclass(frozen=True, slots=True)
class JobRequest:
    kind: str
    tenant_id: str | None = None
    seller_id: int | None = None
    payload: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class JobReceipt:
    job_id: str
    status: str = "queued"


class JobPublisher(Protocol):
    def publish(self, request: JobRequest) -> JobReceipt: ...


class ObjectStore(Protocol):
    def put_bytes(self, key: str, data: bytes, *, content_type: str | None = None) -> str: ...
    def get_bytes(self, key: str) -> bytes: ...


class CacheStore(Protocol):
    def get(self, key: str) -> Any | None: ...
    def set(self, key: str, value: Any, *, ttl_seconds: int | None = None) -> None: ...
