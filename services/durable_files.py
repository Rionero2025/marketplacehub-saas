from __future__ import annotations

import mimetypes
import re
from pathlib import Path
from typing import Any, Mapping

from services.db import DATA_DIR
from services.object_storage import object_store, sha256_bytes, storage_config

CACHE_DIR = DATA_DIR / "object_cache" / "files"


def safe_filename(value: str, default: str = "file.bin") -> str:
    name = Path(str(value or "").replace("\\", "/")).name.strip()
    name = re.sub(r"[^A-Za-z0-9._-]+", "_", name).strip("._")
    return name or default


def content_type_for(name: str, fallback: str = "application/octet-stream") -> str:
    guessed, _ = mimetypes.guess_type(str(name or ""))
    return guessed or fallback


def durable_key(namespace: str, identity: str, filename: str, digest: str) -> str:
    ns = "/".join(part for part in str(namespace or "files").replace("\\", "/").split("/") if part)
    ident = re.sub(r"[^A-Za-z0-9._-]+", "_", str(identity or "item")).strip("._") or "item"
    name = safe_filename(filename)
    return f"{ns}/{ident}/{digest[:16]}_{name}"


def put_bytes(
    *, namespace: str, identity: str, filename: str, content: bytes,
    content_type: str | None = None,
) -> dict[str, Any]:
    payload = bytes(content or b"")
    if not payload:
        raise ValueError("Il file da archiviare è vuoto")
    digest = sha256_bytes(payload)
    key = durable_key(namespace, identity, filename, digest)
    object_store().put_bytes(key, payload, content_type=content_type or content_type_for(filename))
    return {
        "storage_key": key,
        "storage_backend": storage_config().backend,
        "sha256": digest,
        "size_bytes": len(payload),
        "filename": safe_filename(filename),
    }


def read_bytes(
    *, local_path: str | Path | None = None, storage_key: str = "", expected_sha256: str = "",
) -> bytes:
    path = Path(str(local_path or "")) if str(local_path or "").strip() else None
    if path is not None and path.is_file():
        payload = path.read_bytes()
    elif str(storage_key or "").strip():
        payload = object_store().get_bytes(str(storage_key).strip())
    else:
        raise FileNotFoundError("File non disponibile né in cache locale né nello storage")
    expected = str(expected_sha256 or "").strip().lower()
    if expected and sha256_bytes(payload).lower() != expected:
        raise ValueError("Integrità file non valida: SHA-256 differente")
    return payload


def materialize(
    *, namespace: str, identity: str, filename: str, storage_key: str,
    expected_sha256: str = "", preferred_path: str | Path | None = None,
) -> Path:
    preferred = Path(str(preferred_path or "")) if str(preferred_path or "").strip() else None
    if preferred is not None and preferred.is_file():
        return preferred
    payload = read_bytes(storage_key=storage_key, expected_sha256=expected_sha256)
    target = CACHE_DIR / safe_filename(namespace.replace("/", "_"), "files") / re.sub(
        r"[^A-Za-z0-9._-]+", "_", str(identity or "item")
    ) / safe_filename(filename)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix(target.suffix + ".tmp")
    temporary.write_bytes(payload)
    temporary.replace(target)
    return target


def delete(storage_key: str) -> None:
    key = str(storage_key or "").strip()
    if not key:
        return
    try:
        object_store().delete(key)
    except Exception:
        pass
