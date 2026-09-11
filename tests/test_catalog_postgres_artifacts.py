from __future__ import annotations

import os
from uuid import uuid4

import pytest
from marketplace_hub_core.catalogs.postgres_artifacts import artifact_write_value
from sqlalchemy import LargeBinary, column, create_engine, func, select, table


def verify_postgres_artifact_roundtrip(engine):
    """Exercise real COPY, server-side insert/update, commit and rollback cleanup.

    Only transaction-local temporary tables are touched. This can also be run
    against the staging engine without inserting any application records.
    """
    raw = bytes(range(256)) * (4 * 1024 * 1024 // 256)
    with engine.connect() as connection:
        for commit in (True, False):
            transaction = connection.begin()
            name = f"mh_artifact_test_{uuid4().hex}"
            connection.exec_driver_sql(
                f'CREATE TEMP TABLE "{name}" (content bytea NOT NULL) ON COMMIT DROP'
            )
            target = table(name, column("content", LargeBinary()), schema="pg_temp")
            value = artifact_write_value(connection, raw)
            statement = target.insert().values(content=value)
            assert not any(isinstance(v, bytes) for v in statement.compile().params.values())
            connection.execute(statement)
            assert connection.scalar(select(func.octet_length(target.c.content))) == len(raw)
            # A database-side comparison avoids returning a large bytea as hex.
            assert connection.scalar(select(target.c.content == value)) is True
            changed = artifact_write_value(connection, raw[::-1])
            connection.execute(target.update().values(content=changed))
            assert connection.scalar(select(target.c.content == changed)) is True
            staged_names = [name, value.element.get_final_froms()[0].name,
                            changed.element.get_final_froms()[0].name]
            if commit:
                transaction.commit()
            else:
                transaction.rollback()
            for temporary_name in staged_names:
                relation = connection.scalar(select(func.to_regclass(f"pg_temp.{temporary_name}")))
                assert relation is None
            connection.rollback()


def test_postgres_copy_roundtrip_and_cleanup():
    url = os.environ.get("MH_TEST_POSTGRES_URL")
    if not url:
        pytest.skip("Set MH_TEST_POSTGRES_URL to exercise PostgreSQL COPY")
    engine = create_engine(url)
    try:
        verify_postgres_artifact_roundtrip(engine)
    finally:
        engine.dispose()


def test_small_artifact_and_sqlite_keep_bound_parameter_path():
    engine = create_engine("sqlite://")
    try:
        with engine.begin() as connection:
            raw = b"catalog" * 200_000
            assert artifact_write_value(connection, raw) is raw
    finally:
        engine.dispose()
