from __future__ import annotations

from datetime import datetime, timezone
from typing import Iterable

from services.database_config import database_engine

SCOPES = {"tenant", "agency", "platform"}
PERMISSIONS = {"use", "manage"}
_SCHEMA_READY = False


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _columns(con, table: str) -> set[str]:
    if database_engine() == "postgresql":
        found = con.execute(
            """SELECT column_name FROM information_schema.columns
               WHERE table_schema=current_schema() AND table_name=?""",
            (table,),
        ).fetchall()
        return {str(item["column_name"]) for item in found}
    return {str(item["name"]) for item in con.execute(f"PRAGMA table_info({table})").fetchall()}


def ensure_catalog_sharing_schema() -> None:
    """Install the tenant-aware catalogue sharing model.

    The legacy ``visibility`` and ``price_list_access`` fields remain as a
    compatibility mirror for older business services.  ``sharing_scope`` and
    ``catalog_tenant_access`` are authoritative for the SaaS model.
    """
    global _SCHEMA_READY
    if _SCHEMA_READY:
        return

    from services.db import connect
    from services.tenancy import ensure_tenancy_schema
    from services.tenant_db import platform_database_scope

    ensure_tenancy_schema()
    with platform_database_scope():
        with connect() as con:
            supplier_cols = _columns(con, "suppliers")
            if "owner_tenant_id" not in supplier_cols:
                con.execute("ALTER TABLE suppliers ADD COLUMN owner_tenant_id INTEGER")
            if "sharing_scope" not in supplier_cols:
                con.execute("ALTER TABLE suppliers ADD COLUMN sharing_scope TEXT NOT NULL DEFAULT 'tenant'")

            list_cols = _columns(con, "price_lists")
            if "owner_tenant_id" not in list_cols:
                con.execute("ALTER TABLE price_lists ADD COLUMN owner_tenant_id INTEGER")
            if "sharing_scope" not in list_cols:
                con.execute("ALTER TABLE price_lists ADD COLUMN sharing_scope TEXT NOT NULL DEFAULT 'tenant'")

            con.execute(
                """
                CREATE TABLE IF NOT EXISTS catalog_tenant_access (
                    resource_type TEXT NOT NULL,
                    resource_id INTEGER NOT NULL,
                    tenant_id INTEGER NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
                    permission TEXT NOT NULL DEFAULT 'use',
                    created_at TEXT NOT NULL,
                    PRIMARY KEY(resource_type,resource_id,tenant_id)
                )
                """
            )
            con.execute(
                "CREATE INDEX IF NOT EXISTS idx_catalog_tenant_access_tenant "
                "ON catalog_tenant_access(tenant_id,resource_type,resource_id)"
            )
            con.execute(
                "CREATE INDEX IF NOT EXISTS idx_price_lists_owner_tenant "
                "ON price_lists(owner_tenant_id,sharing_scope,active)"
            )
            con.execute(
                "CREATE INDEX IF NOT EXISTS idx_suppliers_owner_tenant "
                "ON suppliers(owner_tenant_id,sharing_scope)"
            )

            # Ownership comes from the Seller->Tenant relation created in v315.
            con.execute(
                """UPDATE suppliers SET owner_tenant_id=(
                       SELECT ts.tenant_id FROM tenant_sellers ts
                       WHERE ts.seller_id=suppliers.owner_seller_id AND ts.active=1 LIMIT 1
                   ) WHERE owner_tenant_id IS NULL"""
            )
            con.execute(
                """UPDATE price_lists SET owner_tenant_id=(
                       SELECT ts.tenant_id FROM tenant_sellers ts
                       WHERE ts.seller_id=price_lists.owner_seller_id AND ts.active=1 LIMIT 1
                   ) WHERE owner_tenant_id IS NULL"""
            )

            # Legacy global maps cleanly to platform. Legacy shared is preserved
            # exactly by converting its seller grants into tenant grants below.
            con.execute(
                "UPDATE price_lists SET sharing_scope='platform' "
                "WHERE visibility='global' AND sharing_scope<>'platform'"
            )
            con.execute(
                "UPDATE suppliers SET sharing_scope='platform' WHERE id IN ("
                "SELECT DISTINCT supplier_id FROM price_lists WHERE sharing_scope='platform')"
            )

            legacy_grants = con.execute(
                """SELECT DISTINCT pla.price_list_id,ts.tenant_id
                   FROM price_list_access pla
                   JOIN tenant_sellers ts ON ts.seller_id=pla.seller_id AND ts.active=1"""
            ).fetchall()
            stamp = _now_iso()
            for grant in legacy_grants:
                con.execute(
                    """INSERT INTO catalog_tenant_access(resource_type,resource_id,tenant_id,permission,created_at)
                       VALUES('price_list',?,?, 'use',?) ON CONFLICT DO NOTHING""",
                    (int(grant["price_list_id"]), int(grant["tenant_id"]), stamp),
                )

            # PostgreSQL gets catalogue-aware RLS instead of the single-owner
            # rule used by v316 for operational tables. Reads may cross tenants
            # only through an explicit Agency/platform/grant policy; writes stay
            # with the owning tenant (or the platform bypass).
            if database_engine() == "postgresql":
                tenant_expr = "NULLIF(current_setting('marketplace_hub.tenant_id', true), '')::BIGINT"
                bypass = "current_setting('marketplace_hub.rls_bypass', true) = '1'"
                read_price_list = f"""(
                    {bypass}
                    OR owner_tenant_id={tenant_expr}
                    OR sharing_scope='platform'
                    OR EXISTS (SELECT 1 FROM catalog_tenant_access cta
                               WHERE cta.resource_type='price_list'
                                 AND cta.resource_id=price_lists.id
                                 AND cta.tenant_id={tenant_expr}
                                 AND cta.permission IN ('use','manage'))
                    OR (sharing_scope='agency' AND EXISTS (
                        SELECT 1 FROM agency_clients ac
                        WHERE ac.agency_tenant_id=price_lists.owner_tenant_id
                          AND ac.client_tenant_id={tenant_expr}
                          AND ac.active=1
                    ))
                )"""
                write_owner = f"({bypass} OR owner_tenant_id={tenant_expr})"
                con.execute("ALTER TABLE price_lists ENABLE ROW LEVEL SECURITY")
                con.execute("ALTER TABLE price_lists FORCE ROW LEVEL SECURITY")
                con.execute("DROP POLICY IF EXISTS mh_catalog_read ON price_lists")
                con.execute("DROP POLICY IF EXISTS mh_catalog_write ON price_lists")
                con.execute(f"CREATE POLICY mh_catalog_read ON price_lists FOR SELECT USING {read_price_list}")
                con.execute(f"CREATE POLICY mh_catalog_write ON price_lists FOR ALL USING {write_owner} WITH CHECK {write_owner}")

                # Supplier visibility follows either its own coarse scope or at
                # least one list that the current tenant can already read.
                read_supplier = f"""(
                    {bypass}
                    OR owner_tenant_id={tenant_expr}
                    OR sharing_scope='platform'
                    OR (sharing_scope='agency' AND EXISTS (
                        SELECT 1 FROM agency_clients ac
                        WHERE ac.agency_tenant_id=suppliers.owner_tenant_id
                          AND ac.client_tenant_id={tenant_expr}
                          AND ac.active=1
                    ))
                    OR EXISTS (SELECT 1 FROM price_lists pl WHERE pl.supplier_id=suppliers.id)
                )"""
                con.execute("ALTER TABLE suppliers ENABLE ROW LEVEL SECURITY")
                con.execute("ALTER TABLE suppliers FORCE ROW LEVEL SECURITY")
                con.execute("DROP POLICY IF EXISTS mh_supplier_read ON suppliers")
                con.execute("DROP POLICY IF EXISTS mh_supplier_write ON suppliers")
                con.execute(f"CREATE POLICY mh_supplier_read ON suppliers FOR SELECT USING {read_supplier}")
                con.execute(f"CREATE POLICY mh_supplier_write ON suppliers FOR ALL USING {write_owner} WITH CHECK {write_owner}")

    _SCHEMA_READY = True


def tenant_for_seller(seller_id: int) -> int:
    ensure_catalog_sharing_schema()
    from services.db import row
    item = row(
        "SELECT tenant_id FROM tenant_sellers WHERE seller_id=? AND active=1",
        (int(seller_id),),
    )
    return int(item["tenant_id"]) if item else 0


def agency_client_ids(agency_tenant_id: int) -> list[int]:
    ensure_catalog_sharing_schema()
    from services.db import rows
    return [
        int(item["client_tenant_id"])
        for item in rows(
            """SELECT client_tenant_id FROM agency_clients
               WHERE agency_tenant_id=? AND active=1 ORDER BY client_tenant_id""",
            (int(agency_tenant_id),),
        )
    ]


def allowed_share_targets(owner_tenant_id: int) -> list[dict]:
    ensure_catalog_sharing_schema()
    from services.db import rows, row
    owner = row("SELECT id,tenant_type FROM tenants WHERE id=?", (int(owner_tenant_id),))
    if not owner:
        return []
    ids = [int(owner_tenant_id)]
    if str(owner.get("tenant_type") or "") == "agency":
        ids.extend(agency_client_ids(int(owner_tenant_id)))
    placeholders = ",".join("?" for _ in ids)
    return rows(
        f"SELECT id,name,slug,tenant_type,status FROM tenants WHERE id IN ({placeholders}) ORDER BY lower(name),id",
        tuple(ids),
    )


def _explicit_tenant_ids(resource_type: str, resource_id: int) -> list[int]:
    from services.db import rows
    return [
        int(item["tenant_id"])
        for item in rows(
            """SELECT tenant_id FROM catalog_tenant_access
               WHERE resource_type=? AND resource_id=? AND permission IN ('use','manage')
               ORDER BY tenant_id""",
            (str(resource_type), int(resource_id)),
        )
    ]


def catalog_policy(price_list_id: int) -> dict | None:
    ensure_catalog_sharing_schema()
    from services.db import row
    item = row(
        """SELECT pl.id,pl.name,pl.owner_seller_id,pl.owner_tenant_id,pl.sharing_scope,
                  pl.visibility,s.name supplier_name,s.owner_tenant_id supplier_owner_tenant_id,
                  s.sharing_scope supplier_sharing_scope
           FROM price_lists pl JOIN suppliers s ON s.id=pl.supplier_id
           WHERE pl.id=?""",
        (int(price_list_id),),
    )
    if not item:
        return None
    item = dict(item)
    item["tenant_ids"] = _explicit_tenant_ids("price_list", int(price_list_id))
    return item


def supplier_policy(supplier_id: int) -> dict | None:
    ensure_catalog_sharing_schema()
    from services.db import row
    item = row(
        """SELECT id,name,owner_seller_id,owner_tenant_id,sharing_scope
           FROM suppliers WHERE id=?""",
        (int(supplier_id),),
    )
    if not item:
        return None
    item = dict(item)
    item["tenant_ids"] = _explicit_tenant_ids("supplier", int(supplier_id))
    return item


def _validate_scope(owner_tenant_id: int, scope: str, *, platform_admin: bool) -> None:
    from services.db import row
    scope = str(scope or "tenant").strip().lower()
    if scope not in SCOPES:
        raise ValueError("Ambito condivisione non valido.")
    owner = row("SELECT id,tenant_type FROM tenants WHERE id=?", (int(owner_tenant_id),))
    if not owner:
        raise ValueError("Tenant proprietario non trovato.")
    if scope == "agency" and str(owner.get("tenant_type") or "") != "agency":
        raise ValueError("Solo un tenant Agency può pubblicare un catalogo a livello Agency.")
    if scope == "platform" and not platform_admin:
        raise PermissionError("Solo il Platform Admin può creare cataloghi globali di piattaforma.")


def _validate_explicit_targets(owner_tenant_id: int, tenant_ids: Iterable[int], *, platform_admin: bool) -> list[int]:
    requested = sorted({int(value) for value in tenant_ids if int(value) > 0})
    if not requested:
        return []
    if platform_admin:
        from services.db import rows
        placeholders = ",".join("?" for _ in requested)
        existing = {int(item["id"]) for item in rows(f"SELECT id FROM tenants WHERE id IN ({placeholders})", tuple(requested))}
        if existing != set(requested):
            raise ValueError("Uno o più tenant di destinazione non esistono.")
        return requested
    allowed = {int(item["id"]) for item in allowed_share_targets(int(owner_tenant_id))}
    if not set(requested).issubset(allowed):
        raise PermissionError("Puoi condividere il catalogo solo con tenant collegati alla tua Agency.")
    return requested


def _sync_legacy_price_list_access(price_list_id: int, owner_tenant_id: int, scope: str, explicit_tenants: list[int]) -> None:
    """Mirror the new tenant policy into the old seller-based access model."""
    from services.db import execute, rows

    targets: set[int] = {int(owner_tenant_id)}
    targets.update(int(value) for value in explicit_tenants)
    if scope == "agency":
        targets.update(agency_client_ids(int(owner_tenant_id)))

    seller_ids: set[int] = set()
    if targets:
        placeholders = ",".join("?" for _ in targets)
        seller_ids = {
            int(item["seller_id"])
            for item in rows(
                f"SELECT seller_id FROM tenant_sellers WHERE active=1 AND tenant_id IN ({placeholders})",
                tuple(sorted(targets)),
            )
        }

    owner = rows("SELECT owner_seller_id FROM price_lists WHERE id=?", (int(price_list_id),))
    owner_seller_id = int(owner[0]["owner_seller_id"]) if owner else 0
    execute("DELETE FROM price_list_access WHERE price_list_id=?", (int(price_list_id),))
    for seller_id in sorted(seller_ids):
        if seller_id == owner_seller_id:
            continue
        execute(
            """INSERT INTO price_list_access(price_list_id,seller_id,permission)
               VALUES(?,?,'use') ON CONFLICT DO NOTHING""",
            (int(price_list_id), seller_id),
        )

    if scope == "platform":
        legacy_visibility = "global"
    elif seller_ids - {owner_seller_id}:
        legacy_visibility = "shared"
    else:
        legacy_visibility = "private"
    execute("UPDATE price_lists SET visibility=? WHERE id=?", (legacy_visibility, int(price_list_id)))


def set_price_list_sharing(
    price_list_id: int,
    *,
    actor_tenant_id: int,
    scope: str,
    tenant_ids: Iterable[int] = (),
    permission: str = "use",
    platform_admin: bool = False,
) -> dict:
    ensure_catalog_sharing_schema()
    from services.db import execute, row

    price_list_id = int(price_list_id)
    actor_tenant_id = int(actor_tenant_id)
    scope = str(scope or "tenant").strip().lower()
    permission = str(permission or "use").strip().lower()
    if permission not in PERMISSIONS:
        raise ValueError("Permesso catalogo non valido.")

    current = row("SELECT id,owner_tenant_id,supplier_id FROM price_lists WHERE id=?", (price_list_id,))
    if not current:
        raise ValueError("Listino non trovato.")
    owner_tenant_id = int(current.get("owner_tenant_id") or 0)
    if not owner_tenant_id:
        raise ValueError("Il listino non ha ancora un tenant proprietario.")
    if not platform_admin and actor_tenant_id != owner_tenant_id:
        raise PermissionError("Solo il tenant proprietario può modificare la condivisione del listino.")

    _validate_scope(owner_tenant_id, scope, platform_admin=platform_admin)
    explicit = _validate_explicit_targets(owner_tenant_id, tenant_ids, platform_admin=platform_admin)
    stamp = _now_iso()

    execute("UPDATE price_lists SET sharing_scope=? WHERE id=?", (scope, price_list_id))
    execute(
        "DELETE FROM catalog_tenant_access WHERE resource_type='price_list' AND resource_id=?",
        (price_list_id,),
    )
    for tenant_id in explicit:
        if tenant_id == owner_tenant_id:
            continue
        execute(
            """INSERT INTO catalog_tenant_access(resource_type,resource_id,tenant_id,permission,created_at)
               VALUES('price_list',?,?,?,?) ON CONFLICT DO NOTHING""",
            (price_list_id, int(tenant_id), permission, stamp),
        )

    _sync_legacy_price_list_access(price_list_id, owner_tenant_id, scope, explicit)

    # Supplier sharing is a coarse visibility marker only. Lists remain the
    # authoritative grant, allowing a supplier to contain private + shared feeds.
    if scope == "platform":
        execute("UPDATE suppliers SET sharing_scope='platform' WHERE id=?", (int(current["supplier_id"]),))
    elif scope == "agency":
        execute(
            """UPDATE suppliers SET sharing_scope='agency'
               WHERE id=? AND sharing_scope<>'platform'""",
            (int(current["supplier_id"]),),
        )

    return catalog_policy(price_list_id) or {}


def set_supplier_sharing(
    supplier_id: int,
    *,
    actor_tenant_id: int,
    scope: str,
    platform_admin: bool = False,
) -> dict:
    ensure_catalog_sharing_schema()
    from services.db import execute, row

    supplier_id = int(supplier_id)
    current = row("SELECT id,owner_tenant_id FROM suppliers WHERE id=?", (supplier_id,))
    if not current:
        raise ValueError("Fornitore non trovato.")
    owner_tenant_id = int(current.get("owner_tenant_id") or 0)
    if not platform_admin and int(actor_tenant_id) != owner_tenant_id:
        raise PermissionError("Solo il tenant proprietario può modificare il fornitore.")
    _validate_scope(owner_tenant_id, scope, platform_admin=platform_admin)
    execute("UPDATE suppliers SET sharing_scope=? WHERE id=?", (str(scope).lower(), supplier_id))
    return supplier_policy(supplier_id) or {}


def accessible_price_lists(seller_id: int, tenant_id: int | None = None) -> list[dict]:
    ensure_catalog_sharing_schema()
    from services.db import rows

    seller_id = int(seller_id)
    tenant_id = int(tenant_id or 0) or tenant_for_seller(seller_id)
    if tenant_id <= 0:
        return []

    return rows(
        """
        SELECT DISTINCT pl.*,s.name supplier_name,own.name owner_name,
               pl.sharing_scope AS catalog_scope,pl.owner_tenant_id AS catalog_owner_tenant_id
        FROM price_lists pl
        JOIN suppliers s ON s.id=pl.supplier_id
        JOIN sellers own ON own.id=pl.owner_seller_id
        LEFT JOIN catalog_tenant_access cta
          ON cta.resource_type='price_list' AND cta.resource_id=pl.id
         AND cta.tenant_id=? AND cta.permission IN ('use','manage')
        LEFT JOIN agency_clients ac
          ON ac.agency_tenant_id=pl.owner_tenant_id
         AND ac.client_tenant_id=? AND ac.active=1
        WHERE pl.active=1 AND (
            pl.owner_tenant_id=?
            OR pl.sharing_scope='platform'
            OR cta.tenant_id IS NOT NULL
            OR (pl.sharing_scope='agency' AND (
                pl.owner_tenant_id=? OR ac.client_tenant_id IS NOT NULL
            ))
        )
        ORDER BY lower(s.name),lower(pl.name),pl.id
        """,
        (tenant_id, tenant_id, tenant_id, tenant_id),
    )
