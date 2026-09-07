from marketplace_hub_core.database import create_database_engine
from marketplace_hub_core.settings import Settings


def test_database_engine_uses_configured_pool_and_pre_ping() -> None:
    engine = create_database_engine(
        Settings(
            environment="test",
            database_url="postgresql+psycopg://user:password@localhost/example",
            db_pool_size=7,
            db_max_overflow=11,
        )
    )
    try:
        assert engine.pool.size() == 7
        assert engine.pool._pre_ping is True
    finally:
        engine.dispose()
