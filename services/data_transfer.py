from __future__ import annotations

import base64
import hashlib
import io
import json
import os
import shutil
import sqlite3
import tempfile
import zipfile
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any

from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from services.database_config import database_engine, load_database_config
from services.db import DB_PATH, DATA_DIR

ROOT = Path(__file__).resolve().parents[1]
SECRETS_PATH = ROOT / ".streamlit" / "secrets.toml"
BACKUP_ROOT = ROOT / "migration_backups"
VERSION_PATH = ROOT / "VERSION.txt"

TRANSFER_FORMAT = "marketplace-hub-transfer"
TRANSFER_FORMAT_VERSION = 2
SUPPORTED_FORMAT_VERSIONS = {1, 2}
TRANSFER_SERVICE_VERSION = 263
TRANSFER_MAGIC_V1 = b"MHUBBACKUP\x01"
TRANSFER_MAGIC = b"MHUBBACKUP\x02"
PBKDF2_ITERATIONS = 600_000
MAX_ARCHIVE_FILES = 20_000
MAX_UNCOMPRESSED_BYTES = 8 * 1024 * 1024 * 1024

_SQLITE_TRANSIENT_NAMES = {
    "marketplace_hub.db",
    "marketplace_hub.db-wal",
    "marketplace_hub.db-shm",
    "marketplace_hub.db-journal",
}


class TransferError(RuntimeError):
    """Raised when a Marketplace Hub transfer package is invalid or unsafe."""


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _timestamp_slug() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S")


def _current_release() -> int:
    try:
        return int(VERSION_PATH.read_text(encoding="utf-8-sig").strip())
    except Exception:
        return 0


def _derive_raw_key(password: str, salt: bytes, iterations: int) -> bytes:
    if not isinstance(password, str) or len(password) < 8:
        raise TransferError("La password del backup deve contenere almeno 8 caratteri.")
    return hashlib.pbkdf2_hmac(
        "sha256",
        password.encode("utf-8"),
        salt,
        int(iterations),
        dklen=32,
    )


def _derive_key(password: str, salt: bytes, iterations: int) -> bytes:
    # Compatibilita' con i backup v260 cifrati con Fernet.
    return base64.urlsafe_b64encode(_derive_raw_key(password, salt, iterations))


def _encrypt_payload(payload: bytes, password: str) -> bytes:
    """Encrypt v261 packages without Fernet's ~33% Base64 size overhead."""
    salt = os.urandom(16)
    nonce = os.urandom(12)
    iterations = int(PBKDF2_ITERATIONS)
    header = TRANSFER_MAGIC + iterations.to_bytes(4, "big", signed=False) + salt + nonce
    key = _derive_raw_key(password, salt, iterations)
    ciphertext = AESGCM(key).encrypt(nonce, payload, header)
    return header + ciphertext


def _decrypt_payload(package_bytes: bytes, password: str) -> bytes:
    if package_bytes.startswith(TRANSFER_MAGIC):
        offset = len(TRANSFER_MAGIC)
        if len(package_bytes) < offset + 32 + 16:
            raise TransferError("Backup incompleto o danneggiato.")
        iterations = int.from_bytes(package_bytes[offset : offset + 4], "big", signed=False)
        salt = package_bytes[offset + 4 : offset + 20]
        nonce = package_bytes[offset + 20 : offset + 32]
        ciphertext = package_bytes[offset + 32 :]
        if iterations < 100_000 or iterations > 2_000_000:
            raise TransferError("Parametri di cifratura del backup non validi.")
        header = package_bytes[: offset + 32]
        key = _derive_raw_key(password, salt, iterations)
        try:
            return AESGCM(key).decrypt(nonce, ciphertext, header)
        except Exception as exc:
            raise TransferError("Password errata oppure backup danneggiato.") from exc

    # Retrocompatibilita': i backup creati dalla v260 usavano Fernet e quindi
    # risultavano circa il 33% piu' grandi per la codifica Base64 interna.
    if package_bytes.startswith(TRANSFER_MAGIC_V1):
        offset = len(TRANSFER_MAGIC_V1)
        if len(package_bytes) < offset + 20:
            raise TransferError("Backup incompleto o danneggiato.")
        iterations = int.from_bytes(package_bytes[offset : offset + 4], "big", signed=False)
        salt = package_bytes[offset + 4 : offset + 20]
        token = package_bytes[offset + 20 :]
        if iterations < 100_000 or iterations > 2_000_000:
            raise TransferError("Parametri di cifratura del backup non validi.")
        key = _derive_key(password, salt, iterations)
        try:
            return Fernet(key).decrypt(token)
        except InvalidToken as exc:
            raise TransferError("Password errata oppure backup danneggiato.") from exc

    raise TransferError("Il file selezionato non è un backup Marketplace Hub valido.")


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _copy_file_to_zip(zf: zipfile.ZipFile, source: Path, arcname: str) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    info = zipfile.ZipInfo(arcname)
    try:
        stat = source.stat()
        dt = datetime.fromtimestamp(stat.st_mtime)
        if dt.year >= 1980:
            info.date_time = dt.timetuple()[:6]
    except (OSError, ValueError):
        pass
    info.compress_type = zipfile.ZIP_DEFLATED
    with source.open("rb") as src, zf.open(info, "w") as dst:
        while True:
            block = src.read(1024 * 1024)
            if not block:
                break
            digest.update(block)
            size += len(block)
            dst.write(block)
    return digest.hexdigest(), size


def _is_transferable_data_file(path: Path) -> bool:
    try:
        relative = path.relative_to(DATA_DIR)
    except ValueError:
        return False
    if not path.is_file() or path.is_symlink():
        return False
    if relative.name in _SQLITE_TRANSIENT_NAMES:
        return False
    if relative.as_posix() == "database.toml":
        # A canonical active database configuration is written separately so
        # environment-based PostgreSQL setups can be moved to another PC too.
        return False
    lowered = relative.name.lower()
    if lowered.endswith((".tmp", ".part")) or lowered.startswith(".write_probe_"):
        return False
    return True


def _sqlite_snapshot(target: Path) -> None:
    if not DB_PATH.exists():
        raise TransferError("Database SQLite non trovato: avvia prima Marketplace Hub almeno una volta.")
    target.parent.mkdir(parents=True, exist_ok=True)
    source = sqlite3.connect(str(DB_PATH), timeout=60.0)
    destination = sqlite3.connect(str(target), timeout=60.0)
    try:
        source.execute("PRAGMA busy_timeout=60000")
        source.backup(destination)
        destination.commit()
        integrity = destination.execute("PRAGMA integrity_check").fetchone()
        if not integrity or str(integrity[0]).lower() != "ok":
            raise TransferError("La copia di sicurezza SQLite non ha superato il controllo di integrità.")
    finally:
        destination.close()
        source.close()


def _sqlite_summary(path: Path) -> dict[str, int]:
    summary: dict[str, int] = {}
    wanted = {
        "sellers": "sellers",
        "marketplace_accounts": "marketplace_accounts",
        "suppliers": "suppliers",
        "price_lists": "price_lists",
        "accounting_order_lines": "accounting_order_lines",
        "kaufland_order_units": "kaufland_order_units",
        "cecotec_order_cache": "cecotec_order_cache",
        "canonical_products": "canonical_products",
        "publication_jobs": "publication_jobs",
    }
    con = sqlite3.connect(str(path), timeout=30.0)
    try:
        tables = {
            str(row[0])
            for row in con.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        }
        for key, table in wanted.items():
            if table not in tables:
                summary[key] = 0
                continue
            try:
                summary[key] = int(con.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0])
            except sqlite3.Error:
                summary[key] = 0
        summary["tables"] = len([name for name in tables if not name.startswith("sqlite_")])
    finally:
        con.close()
    return summary



_MANAGED_DATA_ROOTS = {
    "price_lists",
    "saved_views",
    "accounting_exports",
    "cecotec_orders",
    "innpro_orders",
    "catalog_intelligence",
}

_PATH_TABLE_SPECS = (
    ("price_lists", "local_path"),
    ("saved_views", "snapshot_path"),
    ("accounting_exports", "file_path"),
    ("cecotec_order_exports", "file_path"),
    ("innpro_order_exports", "file_path"),
    ("publication_artifacts", "local_path"),
)

_ROOT_PATH_SPEC = {
    "price_lists": ("price_lists", "local_path"),
    "saved_views": ("saved_views", "snapshot_path"),
    "accounting_exports": ("accounting_exports", "file_path"),
    "cecotec_orders": ("cecotec_order_exports", "file_path"),
    "innpro_orders": ("innpro_order_exports", "file_path"),
    "catalog_intelligence": ("publication_artifacts", "local_path"),
}


def _data_relative_from_value(value: Any) -> Path | None:
    text = str(value or "").strip()
    if not text:
        return None
    raw = Path(text)
    candidates: list[Path] = []
    if raw.is_absolute():
        candidates.append(raw)
    else:
        candidates.extend((ROOT / raw, DATA_DIR / raw))
    for candidate in candidates:
        try:
            resolved = candidate.resolve(strict=False)
            return resolved.relative_to(DATA_DIR.resolve(strict=False))
        except (OSError, ValueError):
            continue

    # A database moved manually can still contain an absolute path from the old
    # PC. Recover the portable suffix after the last ``data`` component.
    normalized = text.replace("\\", "/")
    parts = [part for part in normalized.split("/") if part]
    lowered = [part.lower() for part in parts]
    if "data" in lowered:
        index = len(lowered) - 1 - lowered[::-1].index("data")
        suffix = parts[index + 1 :]
        if suffix:
            return Path(*suffix)
    return None


def _sqlite_path_plan(path: Path) -> tuple[set[str], list[dict[str, str]], set[str]]:
    """Return referenced data files, rewrites, and roots safe to compact."""
    referenced: set[str] = set()
    rewrites: list[dict[str, str]] = []
    compact_roots: set[str] = set()
    con = sqlite3.connect(str(path), timeout=30.0)
    try:
        tables = {
            str(row[0])
            for row in con.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        }
        table_columns: dict[str, set[str]] = {}
        for table in tables:
            try:
                table_columns[table] = {
                    str(row[1]) for row in con.execute(f'PRAGMA table_info("{table}")').fetchall()
                }
            except sqlite3.Error:
                table_columns[table] = set()
        for root, (table, column) in _ROOT_PATH_SPEC.items():
            if table in tables and column in table_columns.get(table, set()):
                compact_roots.add(root)

        for table, column in _PATH_TABLE_SPECS:
            if table not in tables or column not in table_columns.get(table, set()):
                continue
            try:
                values = con.execute(
                    f"SELECT DISTINCT \"{column}\" FROM \"{table}\" WHERE \"{column}\" IS NOT NULL AND TRIM(\"{column}\")<>''"
                ).fetchall()
            except sqlite3.Error:
                continue
            for (value,) in values:
                relative = _data_relative_from_value(value)
                if relative is None or relative.is_absolute() or ".." in relative.parts:
                    continue
                relative_posix = relative.as_posix()
                source = DATA_DIR / relative
                if source.is_file() and not source.is_symlink():
                    referenced.add(relative_posix)
                    rewrites.append(
                        {
                            "table": table,
                            "column": column,
                            "old_value": str(value),
                            "relative": relative_posix,
                        }
                    )
    finally:
        con.close()
    return referenced, rewrites, compact_roots


def _select_transferable_data_files(
    referenced: set[str], compact_roots: set[str]
) -> tuple[list[Path], int, int]:
    """Keep current persistent state but drop obsolete duplicate managed files."""
    selected: list[Path] = []
    excluded_files = 0
    excluded_bytes = 0
    if not DATA_DIR.is_dir():
        return selected, excluded_files, excluded_bytes
    for path in sorted(DATA_DIR.rglob("*")):
        if not _is_transferable_data_file(path):
            continue
        relative = path.relative_to(DATA_DIR)
        relative_posix = relative.as_posix()
        managed_root = relative.parts[0] if relative.parts else ""
        managed = managed_root in _MANAGED_DATA_ROOTS and managed_root in compact_roots
        if managed and relative_posix not in referenced:
            excluded_files += 1
            try:
                excluded_bytes += int(path.stat().st_size)
            except OSError:
                pass
            continue
        selected.append(path)
    return selected, excluded_files, excluded_bytes


def _legacy_path_rewrites_from_staged(path: Path, staged_data: Path) -> list[dict[str, str]]:
    """Build portable rewrites for v260 backups that did not store a path map."""
    rewrites: list[dict[str, str]] = []
    con = sqlite3.connect(str(path), timeout=30.0)
    try:
        tables = {
            str(row[0])
            for row in con.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        }
        for table, column in _PATH_TABLE_SPECS:
            if table not in tables:
                continue
            columns = {str(row[1]) for row in con.execute(f'PRAGMA table_info("{table}")').fetchall()}
            if column not in columns:
                continue
            values = con.execute(
                f"SELECT DISTINCT \"{column}\" FROM \"{table}\" WHERE \"{column}\" IS NOT NULL AND TRIM(\"{column}\")<>''"
            ).fetchall()
            for (value,) in values:
                relative = _data_relative_from_value(value)
                if relative is None or relative.is_absolute() or ".." in relative.parts:
                    continue
                if not (staged_data / relative).is_file():
                    continue
                rewrites.append(
                    {
                        "table": table,
                        "column": column,
                        "old_value": str(value),
                        "relative": relative.as_posix(),
                    }
                )
    finally:
        con.close()
    return rewrites


def _rewrite_staged_sqlite_paths(path: Path, rewrites: list[dict[str, str]]) -> int:
    if not rewrites:
        return 0
    allowed = set(_PATH_TABLE_SPECS)
    con = sqlite3.connect(str(path), timeout=30.0)
    changed = 0
    try:
        for entry in rewrites:
            table = str(entry.get("table") or "")
            column = str(entry.get("column") or "")
            if (table, column) not in allowed:
                continue
            relative = PurePosixPath(str(entry.get("relative") or ""))
            if relative.is_absolute() or ".." in relative.parts or not relative.parts:
                continue
            target = DATA_DIR.joinpath(*relative.parts)
            old_value = str(entry.get("old_value") or "")
            if not old_value:
                continue
            cur = con.execute(
                f'UPDATE "{table}" SET "{column}"=? WHERE "{column}"=?',
                (str(target), old_value),
            )
            changed += max(0, int(cur.rowcount or 0))
        con.commit()
    finally:
        con.close()
    return changed


def _toml_string(value: Any) -> str:
    return json.dumps(str(value or ""), ensure_ascii=False)


def _portable_database_config_bytes(engine: str) -> bytes:
    if engine != "postgresql":
        return b'engine = "sqlite"\n'
    cfg = load_database_config()
    lines = [
        'engine = "postgresql"',
        f'postgresql_host = {_toml_string(cfg.get("postgresql_host") or "127.0.0.1")}',
        f'postgresql_port = {int(cfg.get("postgresql_port") or 5432)}',
        f'postgresql_database = {_toml_string(cfg.get("postgresql_database") or "marketplace_hub")}',
        f'postgresql_user = {_toml_string(cfg.get("postgresql_user") or "marketplace_hub")}',
        f'postgresql_password = {_toml_string(cfg.get("postgresql_password") or "")}',
        f'postgresql_sslmode = {_toml_string(cfg.get("postgresql_sslmode") or "prefer")}',
        f'postgresql_pool_min = {int(cfg.get("postgresql_pool_min") or 2)}',
        f'postgresql_pool_max = {int(cfg.get("postgresql_pool_max") or 12)}',
        f'postgresql_connect_timeout = {int(cfg.get("postgresql_connect_timeout") or 10)}',
        "",
    ]
    return "\n".join(lines).encode("utf-8")

def _secret_bytes() -> tuple[bytes | None, str]:
    if SECRETS_PATH.is_file():
        try:
            return SECRETS_PATH.read_bytes(), "secrets.toml"
        except OSError as exc:
            raise TransferError(f"Impossibile leggere .streamlit/secrets.toml: {exc}") from exc
    master = str(os.getenv("MARKETPLACE_HUB_MASTER_KEY") or "").strip()
    if master:
        # JSON string escaping is compatible with TOML basic string escaping for
        # the characters emitted by the launcher-generated key.
        value = json.dumps(master, ensure_ascii=False)
        return f"MARKETPLACE_HUB_MASTER_KEY = {value}\n".encode("utf-8"), "environment"
    return None, "missing"


def create_transfer_package(password: str) -> tuple[bytes, dict[str, Any]]:
    """Create one compact encrypted Marketplace Hub transfer package.

    Core state lives in the database. For managed local folders we include only
    files still referenced by the database, avoiding old list downloads,
    temporary merge files and orphaned exports that made v260 backups balloon.
    """
    if not isinstance(password, str) or len(password) < 8:
        raise TransferError("La password del backup deve contenere almeno 8 caratteri.")
    engine = str(database_engine() or "sqlite").strip().lower()
    source_release = _current_release()
    checksums: dict[str, str] = {}
    total_uncompressed = 0
    data_file_count = 0
    database_summary: dict[str, int] = {}
    path_rewrites: list[dict[str, str]] = []
    referenced: set[str] = set()
    compact_roots: set[str] = set()
    excluded_files = 0
    excluded_bytes = 0

    with tempfile.TemporaryDirectory(prefix="mhub_export_") as temp_dir:
        temp_root = Path(temp_dir)
        snapshot = temp_root / "marketplace_hub.db"
        if engine == "sqlite":
            _sqlite_snapshot(snapshot)
            database_summary = _sqlite_summary(snapshot)
            referenced, path_rewrites, compact_roots = _sqlite_path_plan(snapshot)

        selected_files, excluded_files, excluded_bytes = _select_transferable_data_files(
            referenced, compact_roots
        )
        # PostgreSQL has no local SQLite snapshot to tell us which managed files
        # are current. Preserve all local data rather than risk dropping a file.
        if engine == "postgresql":
            selected_files = [
                path for path in sorted(DATA_DIR.rglob("*"))
                if _is_transferable_data_file(path)
            ] if DATA_DIR.is_dir() else []
            excluded_files = 0
            excluded_bytes = 0

        zip_buffer = io.BytesIO()
        with zipfile.ZipFile(zip_buffer, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as zf:
            if engine == "sqlite":
                checksum, size = _copy_file_to_zip(zf, snapshot, "database/marketplace_hub.db")
                checksums["database/marketplace_hub.db"] = checksum
                total_uncompressed += size

            for path in selected_files:
                relative = path.relative_to(DATA_DIR).as_posix()
                arcname = f"data/{relative}"
                checksum, size = _copy_file_to_zip(zf, path, arcname)
                checksums[arcname] = checksum
                total_uncompressed += size
                data_file_count += 1

            database_config_payload = _portable_database_config_bytes(engine)
            zf.writestr("data/database.toml", database_config_payload)
            checksums["data/database.toml"] = _sha256_bytes(database_config_payload)
            total_uncompressed += len(database_config_payload)
            data_file_count += 1

            secret_payload, secret_source = _secret_bytes()
            if secret_payload is not None:
                zf.writestr("config/secrets.toml", secret_payload)
                checksums["config/secrets.toml"] = _sha256_bytes(secret_payload)
                total_uncompressed += len(secret_payload)

            manifest: dict[str, Any] = {
                "format": TRANSFER_FORMAT,
                "format_version": TRANSFER_FORMAT_VERSION,
                "created_at_utc": _utc_now(),
                "source_release": source_release,
                "source_engine": engine,
                "encryption": "AES-256-GCM",
                "includes": {
                    "sqlite_database": engine == "sqlite",
                    "persistent_data_files": data_file_count,
                    "secrets": secret_payload is not None,
                    "secret_source": secret_source,
                    "db_referenced_files": len(referenced),
                },
                "compaction": {
                    "excluded_orphan_files": excluded_files,
                    "excluded_orphan_bytes": excluded_bytes,
                },
                "database_summary": database_summary,
                "total_uncompressed_bytes": total_uncompressed,
                "path_rewrites": path_rewrites,
                "checksums_sha256": checksums,
            }
            manifest_bytes = json.dumps(
                manifest, ensure_ascii=False, indent=2, sort_keys=True
            ).encode("utf-8")
            zf.writestr("manifest.json", manifest_bytes)

        compressed = zip_buffer.getvalue()
        encrypted = _encrypt_payload(compressed, password)
        manifest["compressed_zip_bytes"] = len(compressed)
        manifest["package_bytes"] = len(encrypted)
    return encrypted, manifest


def _safe_member_name(name: str) -> bool:
    pure = PurePosixPath(name)
    if pure.is_absolute() or ".." in pure.parts or "" in pure.parts:
        return False
    if name == "manifest.json" or name == "database/marketplace_hub.db" or name == "config/secrets.toml":
        return True
    return len(pure.parts) >= 2 and pure.parts[0] == "data"


def _read_and_validate_archive(package_bytes: bytes, password: str, *, verify_checksums: bool) -> tuple[bytes, dict[str, Any]]:
    raw_zip = _decrypt_payload(package_bytes, password)
    try:
        zf = zipfile.ZipFile(io.BytesIO(raw_zip), "r")
    except zipfile.BadZipFile as exc:
        raise TransferError("Contenuto del backup non valido o danneggiato.") from exc
    with zf:
        infos = zf.infolist()
        if len(infos) > MAX_ARCHIVE_FILES:
            raise TransferError("Il backup contiene un numero anomalo di file.")
        total_size = sum(max(0, int(info.file_size)) for info in infos)
        if total_size > MAX_UNCOMPRESSED_BYTES:
            raise TransferError("Il backup supera la dimensione massima gestibile in sicurezza.")
        names = {info.filename for info in infos if not info.is_dir()}
        if "manifest.json" not in names:
            raise TransferError("Manifest del backup mancante.")
        invalid = sorted(name for name in names if not _safe_member_name(name))
        if invalid:
            raise TransferError(f"Percorso non consentito nel backup: {invalid[0]}")
        try:
            manifest = json.loads(zf.read("manifest.json").decode("utf-8"))
        except Exception as exc:
            raise TransferError("Manifest del backup illeggibile.") from exc
        if manifest.get("format") != TRANSFER_FORMAT:
            raise TransferError("Formato backup non riconosciuto.")
        format_version = int(manifest.get("format_version") or 0)
        if format_version not in SUPPORTED_FORMAT_VERSIONS:
            raise TransferError("Versione del formato backup non supportata.")
        source_release = int(manifest.get("source_release") or 0)
        current_release = _current_release()
        if source_release > current_release:
            raise TransferError(
                f"Il backup proviene da Marketplace Hub v{source_release}, mentre questo PC ha v{current_release}. "
                "Aggiorna prima il programma sul PC di destinazione."
            )
        engine = str(manifest.get("source_engine") or "sqlite").lower()
        if engine not in {"sqlite", "postgresql"}:
            raise TransferError(f"Motore database sorgente non supportato: {engine}")
        if engine == "sqlite" and "database/marketplace_hub.db" not in names:
            raise TransferError("Il backup SQLite non contiene il database principale.")
        expected_checksums = manifest.get("checksums_sha256") or {}
        if not isinstance(expected_checksums, dict):
            raise TransferError("Elenco checksum del backup non valido.")
        payload_names = names - {"manifest.json"}
        if set(expected_checksums) != payload_names:
            raise TransferError("Indice dei file del backup non coerente con il manifest.")
        if verify_checksums:
            for name, expected in expected_checksums.items():
                if name not in names:
                    raise TransferError(f"File dichiarato nel backup ma mancante: {name}")
                actual = _sha256_bytes(zf.read(name))
                if actual.lower() != str(expected).lower():
                    raise TransferError(f"Checksum non valido: {name}")
    return raw_zip, manifest


def inspect_transfer_package(package_bytes: bytes, password: str) -> dict[str, Any]:
    """Decrypt and fully verify a backup without modifying the current installation."""
    _, manifest = _read_and_validate_archive(package_bytes, password, verify_checksums=True)
    return manifest


def _write_member(zf: zipfile.ZipFile, member: str, destination: Path, expected: str | None) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256()
    temporary = destination.with_name(destination.name + ".importing")
    try:
        with zf.open(member, "r") as src, temporary.open("wb") as dst:
            while True:
                block = src.read(1024 * 1024)
                if not block:
                    break
                digest.update(block)
                dst.write(block)
        if expected and digest.hexdigest().lower() != str(expected).lower():
            raise TransferError(f"Checksum non valido durante l'importazione: {member}")
        os.replace(temporary, destination)
    finally:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass


def _verify_staged_sqlite(path: Path) -> None:
    if not path.is_file():
        raise TransferError("Database SQLite importato mancante.")
    con = sqlite3.connect(str(path), timeout=30.0)
    try:
        result = con.execute("PRAGMA integrity_check").fetchone()
        if not result or str(result[0]).lower() != "ok":
            raise TransferError("Il database importato non supera il controllo di integrità SQLite.")
    finally:
        con.close()


def restore_transfer_package(package_bytes: bytes, password: str) -> dict[str, Any]:
    """Restore an encrypted transfer package, preserving the previous PC state.

    The current ``data/`` directory is moved atomically into ``migration_backups``
    before the staged import replaces it. The previous ``secrets.toml`` is copied
    there as well. A restart is intentionally required so Streamlit reloads the
    imported master key and database configuration cleanly.
    """
    raw_zip, manifest = _read_and_validate_archive(package_bytes, password, verify_checksums=True)
    checksums: dict[str, str] = dict(manifest.get("checksums_sha256") or {})
    source_engine = str(manifest.get("source_engine") or "sqlite").lower()
    path_rewrites = manifest.get("path_rewrites") or []
    if not isinstance(path_rewrites, list):
        path_rewrites = []
    rewritten_paths = 0

    BACKUP_ROOT.mkdir(parents=True, exist_ok=True)
    safety_dir = BACKUP_ROOT / f"pre_import_{_timestamp_slug()}"
    suffix = 1
    while safety_dir.exists():
        safety_dir = BACKUP_ROOT / f"pre_import_{_timestamp_slug()}_{suffix}"
        suffix += 1
    safety_dir.mkdir(parents=True, exist_ok=False)

    staging = Path(tempfile.mkdtemp(prefix="mhub_import_", dir=str(ROOT)))
    staged_data = staging / "data"
    staged_data.mkdir(parents=True, exist_ok=True)
    staged_secrets = staging / "secrets.toml"
    data_swapped = False
    secret_replaced = False
    secret_existed_before = SECRETS_PATH.exists()
    old_data_backup = safety_dir / "data"
    previous_secret_backup = safety_dir / "secrets.toml"

    try:
        with zipfile.ZipFile(io.BytesIO(raw_zip), "r") as zf:
            names = {info.filename for info in zf.infolist() if not info.is_dir()}
            for name in sorted(names):
                if name in {"manifest.json", "config/secrets.toml", "database/marketplace_hub.db"}:
                    continue
                if not name.startswith("data/"):
                    continue
                relative = PurePosixPath(name).relative_to("data")
                destination = staged_data.joinpath(*relative.parts)
                _write_member(zf, name, destination, checksums.get(name))

            if source_engine == "sqlite":
                database_target = staged_data / "marketplace_hub.db"
                _write_member(
                    zf,
                    "database/marketplace_hub.db",
                    database_target,
                    checksums.get("database/marketplace_hub.db"),
                )
                _verify_staged_sqlite(database_target)
                effective_rewrites = path_rewrites or _legacy_path_rewrites_from_staged(
                    database_target, staged_data
                )
                rewritten_paths = _rewrite_staged_sqlite_paths(database_target, effective_rewrites)
                _verify_staged_sqlite(database_target)
                # Transient WAL/SHM files are intentionally not restored.
                for transient in ("marketplace_hub.db-wal", "marketplace_hub.db-shm", "marketplace_hub.db-journal"):
                    try:
                        (staged_data / transient).unlink(missing_ok=True)
                    except OSError:
                        pass

            if "config/secrets.toml" in names:
                _write_member(
                    zf,
                    "config/secrets.toml",
                    staged_secrets,
                    checksums.get("config/secrets.toml"),
                )

        # Swap the persistent data directory only after the whole package has
        # been staged and validated.
        if DATA_DIR.exists():
            os.replace(DATA_DIR, old_data_backup)
        else:
            old_data_backup.mkdir(parents=True, exist_ok=True)
        try:
            os.replace(staged_data, DATA_DIR)
            data_swapped = True
        except Exception:
            if DATA_DIR.exists():
                shutil.rmtree(DATA_DIR, ignore_errors=True)
            if old_data_backup.exists():
                os.replace(old_data_backup, DATA_DIR)
            raise

        if staged_secrets.is_file():
            SECRETS_PATH.parent.mkdir(parents=True, exist_ok=True)
            if SECRETS_PATH.exists():
                shutil.copy2(SECRETS_PATH, previous_secret_backup)
            temp_secret = SECRETS_PATH.with_name(SECRETS_PATH.name + ".importing")
            shutil.copy2(staged_secrets, temp_secret)
            os.replace(temp_secret, SECRETS_PATH)
            secret_replaced = True

        report = {
            "restored_at_utc": _utc_now(),
            "source_release": int(manifest.get("source_release") or 0),
            "source_engine": source_engine,
            "safety_backup": str(safety_dir),
            "database_summary": dict(manifest.get("database_summary") or {}),
            "secrets_restored": staged_secrets.is_file(),
            "rewritten_local_paths": rewritten_paths,
            "restart_required": True,
        }
        (safety_dir / "IMPORT_REPORT.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        return report
    except Exception:
        # If the secrets were already replaced, restore the previous local key.
        if secret_replaced:
            try:
                if previous_secret_backup.exists():
                    SECRETS_PATH.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(previous_secret_backup, SECRETS_PATH)
                elif not secret_existed_before:
                    SECRETS_PATH.unlink(missing_ok=True)
            except OSError:
                pass
        # If the data swap succeeded but a later step failed, roll the data back.
        if data_swapped and old_data_backup.exists():
            try:
                if DATA_DIR.exists():
                    failed_dir = safety_dir / "failed_import_data"
                    if failed_dir.exists():
                        shutil.rmtree(failed_dir, ignore_errors=True)
                    os.replace(DATA_DIR, failed_dir)
                os.replace(old_data_backup, DATA_DIR)
            except OSError:
                pass
        raise
    finally:
        shutil.rmtree(staging, ignore_errors=True)
