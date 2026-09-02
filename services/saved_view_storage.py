from __future__ import annotations

import io
from pathlib import Path
from typing import Any, Mapping

import pandas as pd

from services.db import DATA_DIR, connect, execute, now_iso, row, rows
from services.lists import safe_name
from services.object_storage import object_store, sha256_bytes, storage_config, storage_status


CACHE_DIR = DATA_DIR / "object_cache" / "saved_views"
LEGACY_DIR = DATA_DIR / "saved_views"


def ensure_saved_view_storage_schema() -> None:
    with connect() as con:
        columns = {str(item["name"]) for item in con.execute("PRAGMA table_info(saved_views)").fetchall()}
        migrations = {
            "snapshot_storage_key": "TEXT NOT NULL DEFAULT ''",
            "snapshot_storage_backend": "TEXT NOT NULL DEFAULT ''",
            "snapshot_sha256": "TEXT NOT NULL DEFAULT ''",
            "snapshot_size_bytes": "INTEGER NOT NULL DEFAULT 0",
        }
        for column, declaration in migrations.items():
            if column not in columns:
                con.execute(f"ALTER TABLE saved_views ADD COLUMN {column} {declaration}")


def _view_row(view: int | Mapping[str, Any]) -> dict[str, Any]:
    ensure_saved_view_storage_schema()
    if isinstance(view, Mapping):
        data = dict(view)
        view_id = int(data.get("id") or 0)
        if view_id and "snapshot_storage_key" not in data:
            fresh = row("SELECT * FROM saved_views WHERE id=?", (view_id,))
            if fresh:
                data.update(fresh)
        return data
    result = row("SELECT * FROM saved_views WHERE id=?", (int(view),))
    if not result:
        raise KeyError(f"Vista salvata non trovata: {view}")
    return result


def _serialize_frame(frame: pd.DataFrame) -> bytes:
    buffer = io.BytesIO()
    frame.to_pickle(buffer)
    return buffer.getvalue()


def _object_key(seller_id: int, view_id: int, name: str) -> str:
    label = safe_name(name or f"view_{view_id}") or f"view_{view_id}"
    return f"saved_views/seller_{int(seller_id)}/view_{int(view_id)}/{label}.pkl"


def save_saved_view_frame(
    *,
    view_id: int,
    seller_id: int,
    name: str,
    frame: pd.DataFrame,
    keep_local_compatibility_copy: bool = True,
) -> dict[str, Any]:
    """Persist one saved view to the configured object store.

    ``snapshot_path`` remains populated with a local compatibility/cache copy so
    old code continues to work during the migration.  The authoritative durable
    reference is ``snapshot_storage_key``; if the process restarts the file can
    be materialized again from object storage.
    """
    ensure_saved_view_storage_schema()
    payload = _serialize_frame(frame)
    digest = sha256_bytes(payload)
    key = _object_key(seller_id, view_id, name)
    current = _view_row(view_id)
    previous_key = str(current.get("snapshot_storage_key") or "")

    store = object_store()
    store.put_bytes(key, payload, content_type="application/octet-stream")

    local_path = LEGACY_DIR / str(int(seller_id)) / f"{int(view_id)}_{safe_name(name)}.pkl"
    if keep_local_compatibility_copy:
        local_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = local_path.with_suffix(local_path.suffix + ".tmp")
        temporary.write_bytes(payload)
        temporary.replace(local_path)
    else:
        local_path = CACHE_DIR / f"{int(view_id)}_{digest[:12]}.pkl"
        local_path.parent.mkdir(parents=True, exist_ok=True)
        local_path.write_bytes(payload)

    execute(
        """UPDATE saved_views SET snapshot_path=?,snapshot_storage_key=?,
           snapshot_storage_backend=?,snapshot_sha256=?,snapshot_size_bytes=?,updated_at=?
           WHERE id=? AND seller_id=?""",
        (
            str(local_path), key, storage_config().backend, digest, len(payload),
            now_iso(), int(view_id), int(seller_id),
        ),
    )
    if previous_key and previous_key != key:
        try:
            store.delete(previous_key)
        except Exception:
            pass
    return {
        "view_id": int(view_id), "path": str(local_path), "storage_key": key,
        "backend": storage_config().backend, "sha256": digest, "size_bytes": len(payload),
    }


def resolve_saved_view_path(view: int | Mapping[str, Any], *, refresh: bool = False) -> Path:
    """Return a readable local path, downloading from object storage if needed."""
    item = _view_row(view)
    view_id = int(item.get("id") or 0)
    local = Path(str(item.get("snapshot_path") or ""))
    expected_hash = str(item.get("snapshot_sha256") or "")

    if not refresh and local.is_file():
        return local

    key = str(item.get("snapshot_storage_key") or "").strip()
    if not key:
        if local.is_file():
            return local
        raise FileNotFoundError(
            f"Vista salvata {view_id} senza file locale e senza copia object storage"
        )

    payload = object_store().get_bytes(key)
    actual_hash = sha256_bytes(payload)
    if expected_hash and actual_hash != expected_hash:
        raise ValueError(f"Integrità snapshot non valida per la vista {view_id}")

    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    target = CACHE_DIR / f"{view_id}_{actual_hash[:12]}.pkl"
    temporary = target.with_suffix(target.suffix + ".tmp")
    temporary.write_bytes(payload)
    temporary.replace(target)
    execute("UPDATE saved_views SET snapshot_path=? WHERE id=?", (str(target), view_id))
    return target


def load_saved_view_frame(view: int | Mapping[str, Any]) -> pd.DataFrame:
    path = resolve_saved_view_path(view)
    try:
        frame = pd.read_pickle(path)
    except Exception as error:
        raise ValueError(f"La vista salvata non è leggibile: {error}") from error
    if not isinstance(frame, pd.DataFrame):
        raise ValueError("La vista salvata non contiene una tabella prodotti valida")
    return frame


def mirror_saved_view_to_storage(view: int | Mapping[str, Any]) -> dict[str, Any]:
    item = _view_row(view)
    if str(item.get("snapshot_storage_key") or "").strip():
        return {
            "view_id": int(item.get("id") or 0),
            "storage_key": str(item.get("snapshot_storage_key") or ""),
            "already_mirrored": True,
        }
    path = Path(str(item.get("snapshot_path") or ""))
    if not path.is_file():
        raise FileNotFoundError(f"Snapshot locale non disponibile: {path}")
    frame = pd.read_pickle(path)
    if not isinstance(frame, pd.DataFrame):
        raise ValueError("Snapshot non valido")
    return save_saved_view_frame(
        view_id=int(item["id"]), seller_id=int(item["seller_id"]),
        name=str(item.get("name") or f"view_{item['id']}"), frame=frame,
    )


def migrate_saved_views_to_storage(seller_id: int | None = None) -> dict[str, Any]:
    ensure_saved_view_storage_schema()
    sql = "SELECT * FROM saved_views"
    params: tuple[Any, ...] = ()
    if seller_id is not None:
        sql += " WHERE seller_id=?"
        params = (int(seller_id),)
    sql += " ORDER BY id"
    candidates = rows(sql, params)
    migrated = 0
    skipped = 0
    failed: list[dict[str, Any]] = []
    for item in candidates:
        if str(item.get("snapshot_storage_key") or "").strip():
            skipped += 1
            continue
        try:
            mirror_saved_view_to_storage(item)
            migrated += 1
        except Exception as error:
            failed.append({"id": int(item.get("id") or 0), "error": str(error)})
    return {"migrated": migrated, "skipped": skipped, "failed": failed, "storage": storage_status()}


def delete_saved_view_object(storage_key: str) -> None:
    key = str(storage_key or "").strip()
    if not key:
        return
    try:
        object_store().delete(key)
    except Exception:
        pass
