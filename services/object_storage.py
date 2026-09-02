from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Protocol

from services.db import DATA_DIR


class ObjectStorageError(RuntimeError):
    pass


class ObjectStorageBackend(Protocol):
    name: str

    def put_bytes(self, key: str, data: bytes, *, content_type: str | None = None) -> str: ...
    def get_bytes(self, key: str) -> bytes: ...
    def exists(self, key: str) -> bool: ...
    def delete(self, key: str) -> None: ...


def _clean_key(value: str) -> str:
    pieces = [part for part in str(value or "").replace("\\", "/").split("/") if part not in ("", ".")]
    if not pieces or any(part == ".." for part in pieces):
        raise ValueError("Chiave object storage non valida")
    return "/".join(pieces)


@dataclass(frozen=True, slots=True)
class StorageConfig:
    backend: str
    bucket: str
    prefix: str
    endpoint_url: str
    region: str
    access_key: str
    secret_key: str
    local_root: Path

    @classmethod
    def from_env(cls) -> "StorageConfig":
        backend = str(os.getenv("MARKETPLACE_HUB_STORAGE_BACKEND", "local") or "local").strip().lower()
        if backend in {"r2", "gcs-s3", "minio"}:
            backend = "s3"
        prefix = str(os.getenv("MARKETPLACE_HUB_STORAGE_PREFIX", "marketplacehub") or "marketplacehub").strip().strip("/")
        return cls(
            backend=backend,
            bucket=str(os.getenv("MARKETPLACE_HUB_STORAGE_BUCKET", "") or "").strip(),
            prefix=prefix,
            endpoint_url=str(os.getenv("MARKETPLACE_HUB_STORAGE_ENDPOINT_URL", "") or "").strip(),
            region=str(os.getenv("MARKETPLACE_HUB_STORAGE_REGION", os.getenv("AWS_DEFAULT_REGION", "auto")) or "auto").strip(),
            access_key=str(os.getenv("MARKETPLACE_HUB_STORAGE_ACCESS_KEY", os.getenv("AWS_ACCESS_KEY_ID", "")) or "").strip(),
            secret_key=str(os.getenv("MARKETPLACE_HUB_STORAGE_SECRET_KEY", os.getenv("AWS_SECRET_ACCESS_KEY", "")) or "").strip(),
            local_root=Path(os.getenv("MARKETPLACE_HUB_STORAGE_LOCAL_ROOT", str(DATA_DIR / "object_store"))).expanduser(),
        )

    def public_status(self) -> dict[str, object]:
        return {
            "backend": self.backend,
            "bucket": self.bucket,
            "prefix": self.prefix,
            "endpoint_configured": bool(self.endpoint_url),
            "credentials_configured": bool(self.access_key and self.secret_key),
            "local_root": str(self.local_root),
        }


class LocalObjectStorage:
    name = "local"

    def __init__(self, root: Path, prefix: str = "") -> None:
        self.root = Path(root)
        self.prefix = str(prefix or "").strip("/")
        self.root.mkdir(parents=True, exist_ok=True)

    def _path(self, key: str) -> Path:
        normalized = _clean_key(key)
        if self.prefix:
            normalized = f"{self.prefix}/{normalized}"
        target = self.root.joinpath(*normalized.split("/"))
        resolved_root = self.root.resolve()
        resolved_parent = target.parent.resolve()
        if resolved_root != resolved_parent and resolved_root not in resolved_parent.parents:
            raise ObjectStorageError("Percorso object storage locale non valido")
        return target

    def put_bytes(self, key: str, data: bytes, *, content_type: str | None = None) -> str:
        target = self._path(key)
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_suffix(target.suffix + ".tmp")
        temporary.write_bytes(data)
        temporary.replace(target)
        return key

    def get_bytes(self, key: str) -> bytes:
        target = self._path(key)
        if not target.is_file():
            raise FileNotFoundError(f"Oggetto non disponibile: {key}")
        return target.read_bytes()

    def exists(self, key: str) -> bool:
        return self._path(key).is_file()

    def delete(self, key: str) -> None:
        try:
            self._path(key).unlink(missing_ok=True)
        except OSError:
            pass


class S3ObjectStorage:
    name = "s3"

    def __init__(self, config: StorageConfig) -> None:
        if not config.bucket:
            raise ObjectStorageError("MARKETPLACE_HUB_STORAGE_BUCKET non configurato")
        try:
            import boto3
        except ImportError as error:  # pragma: no cover - runtime dependency
            raise ObjectStorageError("boto3 non disponibile") from error
        kwargs: dict[str, object] = {"region_name": config.region or "auto"}
        if config.endpoint_url:
            kwargs["endpoint_url"] = config.endpoint_url
        if config.access_key:
            kwargs["aws_access_key_id"] = config.access_key
        if config.secret_key:
            kwargs["aws_secret_access_key"] = config.secret_key
        self.client = boto3.client("s3", **kwargs)
        self.bucket = config.bucket
        self.prefix = config.prefix

    def _key(self, key: str) -> str:
        normalized = _clean_key(key)
        return f"{self.prefix}/{normalized}" if self.prefix else normalized

    def put_bytes(self, key: str, data: bytes, *, content_type: str | None = None) -> str:
        params: dict[str, object] = {"Bucket": self.bucket, "Key": self._key(key), "Body": data}
        if content_type:
            params["ContentType"] = content_type
        self.client.put_object(**params)
        return key

    def get_bytes(self, key: str) -> bytes:
        response = self.client.get_object(Bucket=self.bucket, Key=self._key(key))
        return response["Body"].read()

    def exists(self, key: str) -> bool:
        try:
            self.client.head_object(Bucket=self.bucket, Key=self._key(key))
            return True
        except Exception:
            return False

    def delete(self, key: str) -> None:
        try:
            self.client.delete_object(Bucket=self.bucket, Key=self._key(key))
        except Exception:
            pass


@lru_cache(maxsize=1)
def storage_config() -> StorageConfig:
    return StorageConfig.from_env()


@lru_cache(maxsize=1)
def object_store() -> ObjectStorageBackend:
    config = storage_config()
    if config.backend == "local":
        return LocalObjectStorage(config.local_root, config.prefix)
    if config.backend == "s3":
        return S3ObjectStorage(config)
    raise ObjectStorageError(f"Backend object storage non supportato: {config.backend}")


def storage_status() -> dict[str, object]:
    config = storage_config()
    result = config.public_status()
    try:
        result["ready"] = bool(object_store())
        result["error"] = ""
    except Exception as error:
        result["ready"] = False
        result["error"] = str(error)
    return result


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _reset_storage_for_tests() -> None:
    object_store.cache_clear()
    storage_config.cache_clear()
