from __future__ import annotations

import argparse
import json
import os
import shutil
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from services.database_config import CONFIG_PATH, load_database_config, postgresql_settings
from services.postgresql_backend import translate_sql

SOURCE_DB = ROOT / "data" / "marketplace_hub.db"
BACKUP_DIR = ROOT / "data" / "backups"


def _now_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")


def _require_psycopg():
    try:
        import psycopg  # type: ignore
        from psycopg import sql  # type: ignore
        from psycopg.rows import dict_row  # type: ignore
    except ImportError as error:
        raise RuntimeError(
            'Psycopg non è installato. Esegui: python -m pip install "psycopg[binary,pool]>=3.2,<4"'
        ) from error
    return psycopg, sql, dict_row


def backup_sqlite(source: Path) -> Path:
    if not source.exists():
        raise FileNotFoundError(f"Database SQLite non trovato: {source}")
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    destination = BACKUP_DIR / f"marketplace_hub_before_postgresql_{_now_stamp()}.db"
    with sqlite3.connect(source) as src, sqlite3.connect(destination) as dst:
        src.backup(dst)
    return destination


def _source_schema(connection: sqlite3.Connection) -> list[dict[str, Any]]:
    connection.row_factory = sqlite3.Row
    return [
        dict(row)
        for row in connection.execute(
            """
            SELECT rowid,type,name,tbl_name,sql
            FROM sqlite_master
            WHERE sql IS NOT NULL AND name NOT LIKE 'sqlite_%'
            ORDER BY CASE type WHEN 'table' THEN 0 WHEN 'index' THEN 1 ELSE 2 END,rowid
            """
        ).fetchall()
    ]


def _source_tables(connection: sqlite3.Connection) -> list[str]:
    return [
        str(row[0])
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY rowid"
        ).fetchall()
    ]


def _source_columns(connection: sqlite3.Connection, table: str) -> list[str]:
    safe = table.replace('"', '""')
    return [str(row[1]) for row in connection.execute(f'PRAGMA table_info("{safe}")').fetchall()]


def _source_count(connection: sqlite3.Connection, table: str) -> int:
    safe = table.replace('"', '""')
    return int(connection.execute(f'SELECT COUNT(*) FROM "{safe}"').fetchone()[0])


def _target_tables(connection) -> list[str]:
    return [
        str(row["table_name"])
        for row in connection.execute(
            """
            SELECT table_name FROM information_schema.tables
            WHERE table_schema=current_schema() AND table_type='BASE TABLE'
            ORDER BY table_name
            """
        ).fetchall()
    ]


def _target_count(connection, sql_module, table: str) -> int:
    query = sql_module.SQL("SELECT COUNT(*) AS count FROM {}").format(sql_module.Identifier(table))
    return int(connection.execute(query).fetchone()["count"])


def _target_columns(connection, table: str) -> list[str]:
    return [
        str(row["column_name"])
        for row in connection.execute(
            """
            SELECT column_name FROM information_schema.columns
            WHERE table_schema=current_schema() AND table_name=%s
            ORDER BY ordinal_position
            """,
            (table,),
        ).fetchall()
    ]


def _make_create_if_not_exists(ddl: str) -> str:
    import re

    return re.sub(
        r"^\s*CREATE\s+TABLE\s+(?!IF\s+NOT\s+EXISTS)",
        "CREATE TABLE IF NOT EXISTS ",
        ddl,
        count=1,
        flags=re.IGNORECASE,
    )


def _make_index_if_not_exists(ddl: str) -> str:
    import re

    return re.sub(
        r"^\s*CREATE\s+(UNIQUE\s+)?INDEX\s+(?!IF\s+NOT\s+EXISTS)",
        lambda match: f"CREATE {match.group(1) or ''}INDEX IF NOT EXISTS ",
        ddl,
        count=1,
        flags=re.IGNORECASE,
    )


def _reset_target_schema(connection) -> None:
    connection.execute("DROP SCHEMA IF EXISTS public CASCADE")
    connection.execute("CREATE SCHEMA public")
    connection.commit()


def _assert_target_safe(connection, sql_module, *, reset_target: bool) -> None:
    existing = _target_tables(connection)
    populated = []
    for table in existing:
        count = _target_count(connection, sql_module, table)
        if count:
            populated.append((table, count))
    if populated and not reset_target:
        sample = ", ".join(f"{name}={count}" for name, count in populated[:8])
        raise RuntimeError(
            "Il database PostgreSQL di destinazione contiene già dati. "
            "Migrazione interrotta per sicurezza. Usa --reset-target soltanto se questo "
            f"database è dedicato a Marketplace Hub. Tabelle: {sample}"
        )
    if reset_target and existing:
        _reset_target_schema(connection)


def _initialize_current_schema(config: dict[str, Any]) -> None:
    # Force only this migration process onto PostgreSQL. The local app remains
    # on SQLite until the final verified activation step writes database.toml.
    old_engine = os.environ.get("MARKETPLACE_HUB_DB_ENGINE")
    os.environ["MARKETPLACE_HUB_DB_ENGINE"] = "postgresql"
    try:
        from services import db

        db.init_db()
    finally:
        if old_engine is None:
            os.environ.pop("MARKETPLACE_HUB_DB_ENGINE", None)
        else:
            os.environ["MARKETPLACE_HUB_DB_ENGINE"] = old_engine


def _create_source_only_schema(source: sqlite3.Connection, target) -> None:
    schema = _source_schema(source)
    for item in schema:
        kind = str(item.get("type") or "")
        ddl = str(item.get("sql") or "").strip()
        if not ddl:
            continue
        if kind == "table":
            ddl = _make_create_if_not_exists(translate_sql(ddl))
        elif kind == "index":
            ddl = _make_index_if_not_exists(translate_sql(ddl))
        else:
            continue
        try:
            target.execute(ddl)
        except Exception as error:
            raise RuntimeError(f"Errore creazione {kind} {item.get('name')}: {error}\nSQL: {ddl}") from error
    target.commit()


def _sanitize_value(value: Any, table: str, column: str) -> Any:
    if isinstance(value, str) and "\x00" in value:
        raise RuntimeError(
            f"Il valore {table}.{column} contiene un byte NUL non accettato da PostgreSQL. "
            "Il dato non è stato modificato: correggilo in SQLite e ripeti la migrazione."
        )
    return value


def _copy_table(source: sqlite3.Connection, target, sql_module, table: str) -> int:
    source_columns = _source_columns(source, table)
    target_columns = set(_target_columns(target, table))
    columns = [column for column in source_columns if column in target_columns]
    if not columns:
        return 0
    safe_table = table.replace('"', '""')
    quoted_columns = ",".join(f'"{column.replace(chr(34), chr(34)*2)}"' for column in columns)
    source_cursor = source.execute(f'SELECT {quoted_columns} FROM "{safe_table}"')
    copy_sql = sql_module.SQL("COPY {} ({}) FROM STDIN").format(
        sql_module.Identifier(table),
        sql_module.SQL(",").join(sql_module.Identifier(column) for column in columns),
    )
    copied = 0
    with target.cursor().copy(copy_sql) as copy:
        while True:
            rows = source_cursor.fetchmany(2000)
            if not rows:
                break
            for source_row in rows:
                values = tuple(
                    _sanitize_value(source_row[index], table, column)
                    for index, column in enumerate(columns)
                )
                copy.write_row(values)
                copied += 1
    target.commit()
    return copied


def _reset_identity_sequences(target, sql_module) -> None:
    identities = target.execute(
        """
        SELECT table_name,column_name
        FROM information_schema.columns
        WHERE table_schema=current_schema() AND is_identity='YES'
        ORDER BY table_name,column_name
        """
    ).fetchall()
    for item in identities:
        table = str(item["table_name"])
        column = str(item["column_name"])
        maximum_query = sql_module.SQL("SELECT MAX({}) AS maximum FROM {}").format(
            sql_module.Identifier(column), sql_module.Identifier(table)
        )
        maximum = target.execute(maximum_query).fetchone()["maximum"]
        if maximum is None:
            continue
        sequence = target.execute(
            "SELECT pg_get_serial_sequence(%s,%s) AS sequence", (table, column)
        ).fetchone()["sequence"]
        if sequence:
            target.execute("SELECT setval(%s,%s,true)", (sequence, int(maximum)))
    target.commit()


def _verify(source: sqlite3.Connection, target, sql_module) -> dict[str, Any]:
    results: dict[str, Any] = {}
    mismatches = []
    for table in _source_tables(source):
        source_count = _source_count(source, table)
        if table not in _target_tables(target):
            results[table] = {"sqlite": source_count, "postgresql": None, "ok": False}
            mismatches.append(table)
            continue
        target_count = _target_count(target, sql_module, table)
        ok = source_count == target_count
        results[table] = {"sqlite": source_count, "postgresql": target_count, "ok": ok}
        if not ok:
            mismatches.append(table)
    return {"tables": results, "mismatches": mismatches, "ok": not mismatches}


def _quote_toml(value: Any) -> str:
    return json.dumps(str(value), ensure_ascii=False)


def _write_config(config: dict[str, Any], *, engine: str) -> None:
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    content = "\n".join(
        [
            "# Marketplace Hub database configuration - generated automatically",
            f"engine = {_quote_toml(engine)}",
            f"postgresql_host = {_quote_toml(config.get('postgresql_host', '127.0.0.1'))}",
            f"postgresql_port = {int(config.get('postgresql_port', 5432))}",
            f"postgresql_database = {_quote_toml(config.get('postgresql_database', 'marketplace_hub'))}",
            f"postgresql_user = {_quote_toml(config.get('postgresql_user', 'marketplace_hub'))}",
            f"postgresql_password = {_quote_toml(config.get('postgresql_password', ''))}",
            f"postgresql_sslmode = {_quote_toml(config.get('postgresql_sslmode', 'prefer'))}",
            f"postgresql_pool_min = {int(config.get('postgresql_pool_min', 2))}",
            f"postgresql_pool_max = {int(config.get('postgresql_pool_max', 12))}",
            f"postgresql_connect_timeout = {int(config.get('postgresql_connect_timeout', 10))}",
            "",
        ]
    )
    CONFIG_PATH.write_text(content, encoding="utf-8")


def migrate(source_path: Path, *, reset_target: bool, activate: bool) -> dict[str, Any]:
    psycopg, sql_module, dict_row = _require_psycopg()
    config = load_database_config()
    settings = postgresql_settings(config)
    if not str(settings.get("password") or ""):
        raise RuntimeError(
            f"Password PostgreSQL non configurata in {CONFIG_PATH}. "
            "Esegui prima CONFIGURA_POSTGRESQL_WINDOWS.ps1."
        )
    backup = backup_sqlite(source_path)
    report: dict[str, Any] = {
        "started_at": datetime.now(timezone.utc).isoformat(),
        "source": str(source_path),
        "backup": str(backup),
        "target": {
            "host": settings["host"], "port": settings["port"],
            "database": settings["dbname"], "user": settings["user"],
        },
        "copied": {},
    }

    with sqlite3.connect(source_path) as source:
        source.row_factory = sqlite3.Row
        integrity = source.execute("PRAGMA integrity_check").fetchone()[0]
        if str(integrity).lower() != "ok":
            raise RuntimeError(f"PRAGMA integrity_check SQLite non superato: {integrity}")
        foreign_key_issues = source.execute("PRAGMA foreign_key_check").fetchall()
        report["sqlite_integrity"] = "ok"
        report["sqlite_foreign_key_issues"] = len(foreign_key_issues)
        with psycopg.connect(**settings, row_factory=dict_row) as target:
            _assert_target_safe(target, sql_module, reset_target=reset_target)

        # init_db uses its own PostgreSQL pool/connection and creates the current
        # Marketplace Hub schema before legacy/lazy tables are restored.
        _initialize_current_schema(config)

        with psycopg.connect(**settings, row_factory=dict_row) as target:
            _create_source_only_schema(source, target)
            for table in _source_tables(source):
                if table not in _target_tables(target):
                    raise RuntimeError(f"Tabella PostgreSQL non creata: {table}")
                report["copied"][table] = _copy_table(source, target, sql_module, table)
                print(f"{table}: {report['copied'][table]:,} righe migrate")
            _reset_identity_sequences(target, sql_module)
            verification = _verify(source, target, sql_module)
            report["verification"] = verification
            if not verification["ok"]:
                raise RuntimeError(
                    "Verifica finale non superata. Tabelle diverse: "
                    + ", ".join(verification["mismatches"])
                )

    report["completed_at"] = datetime.now(timezone.utc).isoformat()
    report["activated"] = bool(activate)
    report_path = ROOT / "data" / f"postgresql_migration_report_{_now_stamp()}.json"
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    report["report_path"] = str(report_path)
    if activate:
        _write_config(config, engine="postgresql")
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description="Migra Marketplace Hub da SQLite a PostgreSQL")
    parser.add_argument("--source", type=Path, default=SOURCE_DB)
    parser.add_argument(
        "--reset-target", action="store_true",
        help="Svuota lo schema public del DB PostgreSQL prima della migrazione. Usare solo sul DB dedicato.",
    )
    parser.add_argument(
        "--activate", action="store_true",
        help="Attiva PostgreSQL in data/database.toml solo dopo verifica riuscita.",
    )
    args = parser.parse_args()
    try:
        result = migrate(args.source, reset_target=args.reset_target, activate=args.activate)
    except Exception as error:
        print(f"\nMIGRAZIONE NON COMPLETATA: {error}", file=sys.stderr)
        print("SQLite rimane il database attivo e non viene cancellato.", file=sys.stderr)
        return 1
    print("\nMigrazione completata e verificata.")
    print("Backup SQLite:", result["backup"])
    print("Report:", result["report_path"])
    if result["activated"]:
        print("PostgreSQL è ora il database attivo. Riavvia Marketplace Hub.")
    else:
        print("PostgreSQL non è stato ancora attivato (manca --activate).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
