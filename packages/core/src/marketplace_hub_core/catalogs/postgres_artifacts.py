from __future__ import annotations

from uuid import uuid4

from sqlalchemy import LargeBinary, column, select, table

COPY_THRESHOLD_BYTES = 1024 * 1024


def artifact_write_value(connection, content: bytes):
    """Keep large bytea values out of PostgreSQL INSERT/UPDATE query plans.

    COPY stores the bytes in a transaction-local, TOAST-backed temporary table.
    Subsequent statements reference that row server-side instead of binding and
    copying a large constant into each plan. The table disappears on commit or
    rollback; no supplier artifact is left on the worker's ephemeral filesystem.
    SQLite and small artifacts retain the ordinary bound-parameter path.
    """
    if connection.dialect.name != "postgresql" or len(content) < COPY_THRESHOLD_BYTES:
        return content

    # Generated identifier only: never a supplier-controlled table name.
    name = f"mh_catalog_artifact_{uuid4().hex}"
    connection.exec_driver_sql(
        f'CREATE TEMP TABLE "{name}" (content bytea NOT NULL) ON COMMIT DROP'
    )
    driver = connection.connection.driver_connection
    with driver.cursor() as cursor:
        with cursor.copy(f'COPY "{name}" (content) FROM STDIN (FORMAT BINARY)') as copy:
            copy.set_types(["bytea"])
            copy.write_row((content,))

    staged = table(name, column("content", LargeBinary()), schema="pg_temp")
    return select(staged.c.content).scalar_subquery()
