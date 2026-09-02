from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar, Token
from typing import Iterator

from services.database_config import database_engine

_TENANT_ID: ContextVar[int] = ContextVar("marketplace_hub_tenant_id", default=0)
_PLATFORM_BYPASS: ContextVar[bool] = ContextVar("marketplace_hub_rls_bypass", default=False)
_SCHEMA_READY = False

# Tables with a strict single-tenant ownership boundary. Shared/global catalogue
# resources are intentionally excluded: their cross-tenant sharing rules need a
# dedicated model rather than a simplistic owner_tenant policy.
RLS_TABLES = (
    "sellers",
    "marketplace_accounts",
    "commercial_rules",
    "operations",
    "saved_views",
    "seller_integrations",
    "buybox_checks",
    "buybox_price_updates",
    "worten_buybox_checks",
    "worten_buybox_views",
    "kaufland_buybox_views",
    "kaufland_buybox_account_checks",
    "kaufland_buybox_account_views",
    "kaufland_buybox_account_price_updates",
    "kaufland_order_units",
    "kaufland_order_syncs",
    "kaufland_live_units",
    "kaufland_inventory_syncs",
    "kaufland_inventory_cursors",
    "accounting_order_lines",
    "accounting_exports",
    "accounting_excel_imports",
    "accounting_supplier_document_imports",
    "accounting_sync_state",
    "cecotec_order_cache",
    "cecotec_order_exports",
    "innpro_order_exports",
    "packlink_shipments",
    "packlink_matches",
    "packlink_sync_runs",
    "packlink_order_drafts",
    "packlink_order_draft_history",
    "packlink_draft_guards",
    "packlink_mass_quote_cache",
    "packlink_sender_addresses",
    "packlink_package_profiles",
    "tracking_imports",
    "tracking_source_files",
    "tracking_matches",
    "support_threads",
    "support_syncs",
    "support_actions",
    "kaufland_support_tickets",
    "kaufland_support_syncs",
    "kaufland_support_actions",
)


def current_tenant_id() -> int:
    return max(0, int(_TENANT_ID.get() or 0))


def platform_bypass_enabled() -> bool:
    return bool(_PLATFORM_BYPASS.get())


def set_tenant_context(tenant_id: int) -> Token:
    return _TENANT_ID.set(max(0, int(tenant_id or 0)))


def reset_tenant_context(token: Token) -> None:
    _TENANT_ID.reset(token)


def clear_tenant_context() -> None:
    _TENANT_ID.set(0)


@contextmanager
def tenant_database_scope(tenant_id: int) -> Iterator[None]:
    tenant_token = _TENANT_ID.set(max(0, int(tenant_id or 0)))
    bypass_token = _PLATFORM_BYPASS.set(False)
    try:
        yield
    finally:
        _PLATFORM_BYPASS.reset(bypass_token)
        _TENANT_ID.reset(tenant_token)


@contextmanager
def platform_database_scope() -> Iterator[None]:
    bypass_token = _PLATFORM_BYPASS.set(True)
    try:
        yield
    finally:
        _PLATFORM_BYPASS.reset(bypass_token)


def apply_postgresql_connection_context(connection) -> None:
    """Bind one pooled PostgreSQL transaction to the current tenant context.

    ``set_config(..., true)`` is transaction-local, so a pooled connection can be
    safely reused by another tenant after commit/rollback without leaking state.
    """
    if database_engine() != "postgresql":
        return
    tenant = str(current_tenant_id()) if current_tenant_id() > 0 else ""
    bypass = "1" if platform_bypass_enabled() else "0"
    connection.execute(
        "SELECT set_config('marketplace_hub.tenant_id', ?, true), "
        "set_config('marketplace_hub.rls_bypass', ?, true)",
        (tenant, bypass),
    )


def _table_columns(con, table: str) -> set[str]:
    found = con.execute(
        """SELECT column_name FROM information_schema.columns
           WHERE table_schema=current_schema() AND table_name=?""",
        (table,),
    ).fetchall()
    return {str(item["column_name"]) for item in found}


def _table_exists(con, table: str) -> bool:
    return bool(
        con.execute(
            """SELECT 1 FROM information_schema.tables
               WHERE table_schema=current_schema() AND table_name=? LIMIT 1""",
            (table,),
        ).fetchone()
    )


def _seller_column(columns: set[str]) -> str:
    if "seller_id" in columns:
        return "seller_id"
    if "owner_seller_id" in columns:
        return "owner_seller_id"
    return ""


def _safe_ident(value: str) -> str:
    text = str(value or "")
    if not text.replace("_", "").isalnum() or not text[:1].isalpha():
        raise ValueError(f"Identificatore SQL non valido: {text}")
    return text


def _policy_expression() -> str:
    return """(
        current_setting('marketplace_hub.rls_bypass', true) = '1'
        OR tenant_id = NULLIF(current_setting('marketplace_hub.tenant_id', true), '')::BIGINT
    )"""


def ensure_tenant_database_isolation() -> dict:
    """Add tenant_id + PostgreSQL RLS to the principal operational tables.

    SQLite remains fully compatible for local tests. PostgreSQL gets a second
    security boundary below FastAPI: even a buggy query cannot cross tenants when
    executed inside a tenant scope.
    """
    global _SCHEMA_READY
    if _SCHEMA_READY:
        return {"engine": database_engine(), "ready": True, "tables": []}
    if database_engine() != "postgresql":
        _SCHEMA_READY = True
        return {"engine": "sqlite", "ready": True, "tables": []}

    from services.db import connect
    from services.tenancy import ensure_tenancy_schema

    # Tenancy metadata must exist before operational rows can be backfilled.
    ensure_tenancy_schema()
    migrated: list[str] = []
    with platform_database_scope():
        with connect() as con:
            for raw_table in RLS_TABLES:
                table = _safe_ident(raw_table)
                if not _table_exists(con, table):
                    continue
                columns = _table_columns(con, table)
                seller_col = _seller_column(columns)
                if table != "sellers" and not seller_col:
                    # Some legacy child tables have no direct seller ownership.
                    # They remain protected through their parent API for now and
                    # will be normalized in the next schema pass.
                    continue
                con.execute(f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS tenant_id BIGINT")
                if table == "sellers":
                    con.execute(
                        """UPDATE sellers s SET tenant_id=ts.tenant_id
                           FROM tenant_sellers ts
                           WHERE s.id=ts.seller_id AND s.tenant_id IS NULL"""
                    )
                else:
                    con.execute(
                        f"""UPDATE {table} x SET tenant_id=ts.tenant_id
                            FROM tenant_sellers ts
                            WHERE x.{seller_col}=ts.seller_id AND x.tenant_id IS NULL"""
                    )

                # Fill tenant_id on all future legacy INSERT statements so the
                # existing business services do not need hundreds of rewrites.
                fn = _safe_ident(f"mh_fill_tenant_{table}")
                trigger = _safe_ident(f"mh_fill_tenant_{table}_trg")
                if table == "sellers":
                    body = """
                    IF NEW.tenant_id IS NULL THEN
                        NEW.tenant_id := NULLIF(current_setting('marketplace_hub.tenant_id', true), '')::BIGINT;
                    END IF;
                    """
                else:
                    body = f"""
                    IF NEW.tenant_id IS NULL THEN
                        SELECT tenant_id INTO NEW.tenant_id FROM tenant_sellers
                        WHERE seller_id=NEW.{seller_col} AND active=1 LIMIT 1;
                    END IF;
                    IF NEW.tenant_id IS NULL THEN
                        NEW.tenant_id := NULLIF(current_setting('marketplace_hub.tenant_id', true), '')::BIGINT;
                    END IF;
                    """
                con.execute(
                    f"""CREATE OR REPLACE FUNCTION {fn}() RETURNS trigger AS $$
                    BEGIN
                        {body}
                        IF NEW.tenant_id IS NULL THEN
                            RAISE EXCEPTION 'tenant_id mancante per %', TG_TABLE_NAME;
                        END IF;
                        RETURN NEW;
                    END;
                    $$ LANGUAGE plpgsql"""
                )
                con.execute(f"DROP TRIGGER IF EXISTS {trigger} ON {table}")
                con.execute(
                    f"CREATE TRIGGER {trigger} BEFORE INSERT OR UPDATE ON {table} "
                    f"FOR EACH ROW EXECUTE FUNCTION {fn}()"
                )
                con.execute(
                    f"CREATE INDEX IF NOT EXISTS idx_{table}_tenant_id ON {table}(tenant_id)"
                )
                expr = _policy_expression()
                con.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
                con.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
                con.execute(f"DROP POLICY IF EXISTS mh_tenant_isolation ON {table}")
                con.execute(
                    f"CREATE POLICY mh_tenant_isolation ON {table} USING {expr} WITH CHECK {expr}"
                )
                migrated.append(table)
    _SCHEMA_READY = True
    return {"engine": "postgresql", "ready": True, "tables": migrated}


def tenant_id_for_seller(seller_id: int) -> int:
    from services.db import row
    item = row(
        "SELECT tenant_id FROM tenant_sellers WHERE seller_id=? AND active=1",
        (int(seller_id),),
    )
    return int(item.get("tenant_id") or 0) if item else 0


def assert_seller_in_tenant(seller_id: int, tenant_id: int) -> None:
    expected = tenant_id_for_seller(int(seller_id))
    if expected <= 0 or expected != int(tenant_id):
        raise RuntimeError("Seller fuori dal tenant del job/sessione.")


def reassign_seller_tenant_rows(seller_id: int, tenant_id: int) -> list[str]:
    """Move the tenant marker for one Seller across protected operational tables.

    Used only by explicit Platform Admin Seller transfers. The ownership mapping
    and all tenant_id copies move together, preventing orphaned RLS rows.
    """
    if database_engine() != "postgresql":
        return []
    from services.db import connect
    changed: list[str] = []
    with platform_database_scope():
        with connect() as con:
            for raw_table in RLS_TABLES:
                table = _safe_ident(raw_table)
                if not _table_exists(con, table):
                    continue
                columns = _table_columns(con, table)
                if "tenant_id" not in columns:
                    continue
                if table == "sellers":
                    con.execute("UPDATE sellers SET tenant_id=? WHERE id=?", (int(tenant_id), int(seller_id)))
                    changed.append(table)
                    continue
                seller_col = _seller_column(columns)
                if not seller_col:
                    continue
                con.execute(
                    f"UPDATE {table} SET tenant_id=? WHERE {seller_col}=?",
                    (int(tenant_id), int(seller_id)),
                )
                changed.append(table)
    return changed
