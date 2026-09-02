from __future__ import annotations

import json
from collections.abc import Callable, Iterable, Mapping
from typing import Any

from services.catalog_intelligence.models import (
    CanonicalProduct,
    Capability,
    TaxonomyAttribute,
    TaxonomyBundle,
    ValidationIssue,
)
from services.catalog_intelligence.schema import ensure_schema
from services.catalog_intelligence.utils import canonical_json, clean_text, json_hash, load_json
from services import postgresql_backend
from services.database_config import database_engine
from services.db import connect, execute, execute_many, json_text, now_iso, row, rows


def save_capabilities(
    *,
    seller_id: int,
    account_id: int,
    marketplace: str,
    environment: str,
    capabilities: Iterable[Capability],
) -> None:
    ensure_schema()
    checked_at = now_iso()
    values = [
        (
            int(seller_id),
            int(account_id),
            clean_text(marketplace).lower(),
            clean_text(environment) or "live",
            item.key,
            int(bool(item.supported)),
            item.status_code,
            item.message,
            json_text(item.details),
            checked_at,
        )
        for item in capabilities
    ]
    if not values:
        return
    execute_many(
        """
        INSERT INTO marketplace_capabilities(
            seller_id,marketplace_account_id,marketplace,environment,
            capability_key,supported,status_code,message,details_json,checked_at
        ) VALUES(?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(marketplace_account_id,environment,capability_key) DO UPDATE SET
            seller_id=excluded.seller_id,
            marketplace=excluded.marketplace,
            supported=excluded.supported,
            status_code=excluded.status_code,
            message=excluded.message,
            details_json=excluded.details_json,
            checked_at=excluded.checked_at
        """,
        values,
    )


def capabilities_for_account(account_id: int, environment: str = "live") -> list[dict]:
    ensure_schema()
    return rows(
        """
        SELECT * FROM marketplace_capabilities
        WHERE marketplace_account_id=? AND environment=?
        ORDER BY capability_key
        """,
        (int(account_id), clean_text(environment) or "live"),
    )


def start_taxonomy_sync(
    *,
    seller_id: int,
    account_id: int,
    marketplace: str,
    environment: str,
    scope_key: str,
    storefront: str = "",
    locale: str = "",
    details: Mapping[str, Any] | None = None,
) -> int:
    ensure_schema()
    return execute(
        """
        INSERT INTO taxonomy_sync_runs(
            seller_id,marketplace_account_id,marketplace,environment,scope_key,
            storefront,locale,status,details_json,started_at
        ) VALUES(?,?,?,?,?,?,?,?,?,?)
        """,
        (
            int(seller_id),
            int(account_id),
            clean_text(marketplace).lower(),
            clean_text(environment) or "live",
            clean_text(scope_key),
            clean_text(storefront).lower(),
            clean_text(locale),
            "RUNNING",
            json_text(dict(details or {})),
            now_iso(),
        ),
    )


def complete_taxonomy_sync(
    run_id: int,
    *,
    status: str,
    snapshot_id: int | None = None,
    category_count: int = 0,
    attribute_count: int = 0,
    value_count: int = 0,
    error: str = "",
    details: Mapping[str, Any] | None = None,
) -> None:
    ensure_schema()
    execute(
        """
        UPDATE taxonomy_sync_runs SET
            status=?,snapshot_id=?,category_count=?,attribute_count=?,value_count=?,
            error=?,details_json=?,completed_at=?
        WHERE id=?
        """,
        (
            clean_text(status).upper(),
            snapshot_id,
            max(0, int(category_count)),
            max(0, int(attribute_count)),
            max(0, int(value_count)),
            clean_text(error)[:4000],
            json_text(dict(details or {})),
            now_iso(),
            int(run_id),
        ),
    )


def _bundle_serializable(bundle: TaxonomyBundle) -> dict[str, Any]:
    return {
        "marketplace": bundle.marketplace,
        "scope_key": bundle.scope_key,
        "storefront": bundle.storefront,
        "locale": bundle.locale,
        "categories": [
            {
                "external_id": item.external_id,
                "parent_external_id": item.parent_external_id,
                "code": item.code,
                "label": item.label,
                "path": item.path,
                "level": item.level,
                "is_leaf": item.is_leaf,
                "product_type": item.product_type,
                "required_attributes": item.required_attributes,
                "raw": item.raw,
            }
            for item in bundle.categories
        ],
        "attributes": [
            {
                "external_id": item.external_id,
                "category_external_id": item.category_external_id,
                "code": item.code,
                "label": item.label,
                "data_type": item.data_type,
                "requirement_level": item.requirement_level,
                "required": item.required,
                "multiple": item.multiple,
                "variant": item.variant,
                "unit": item.unit,
                "locale": item.locale,
                "value_list_code": item.value_list_code,
                "constraints": item.constraints,
                "values": item.values,
                "conditions": item.conditions,
                "raw": item.raw,
            }
            for item in bundle.attributes
        ],
        "locales": bundle.locales,
        "metadata": bundle.metadata,
    }


def save_taxonomy_bundle(
    *,
    seller_id: int,
    account_id: int,
    environment: str,
    bundle: TaxonomyBundle,
) -> tuple[int, bool]:
    """Persist an immutable taxonomy snapshot.

    Returns ``(snapshot_id, created)``.  Repeated synchronization with identical
    content reuses the existing snapshot and only marks it active.
    """
    ensure_schema()
    serialized = _bundle_serializable(bundle)
    content_hash = json_hash(serialized)
    environment = clean_text(environment) or "live"
    existing = row(
        """
        SELECT id FROM taxonomy_snapshots
        WHERE marketplace_account_id=? AND environment=? AND scope_key=? AND content_hash=?
        """,
        (int(account_id), environment, bundle.scope_key, content_hash),
    )
    if existing:
        snapshot_id = int(existing["id"])
        execute(
            """
            UPDATE taxonomy_snapshots SET active=CASE WHEN id=? THEN 1 ELSE 0 END
            WHERE marketplace_account_id=? AND environment=? AND scope_key=?
            """,
            (snapshot_id, int(account_id), environment, bundle.scope_key),
        )
        return snapshot_id, False

    execute(
        """
        UPDATE taxonomy_snapshots SET active=0
        WHERE marketplace_account_id=? AND environment=? AND scope_key=?
        """,
        (int(account_id), environment, bundle.scope_key),
    )
    value_count = sum(len(item.values) for item in bundle.attributes)
    snapshot_id = execute(
        """
        INSERT INTO taxonomy_snapshots(
            seller_id,marketplace_account_id,marketplace,environment,scope_key,
            storefront,locale,content_hash,raw_json,category_count,attribute_count,
            value_count,active,created_at
        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            int(seller_id),
            int(account_id),
            clean_text(bundle.marketplace).lower(),
            environment,
            bundle.scope_key,
            clean_text(bundle.storefront).lower(),
            clean_text(bundle.locale),
            content_hash,
            canonical_json({"metadata": bundle.metadata}),
            len(bundle.categories),
            len(bundle.attributes),
            value_count,
            1,
            now_iso(),
        ),
    )
    if not snapshot_id:
        found = row(
            """
            SELECT id FROM taxonomy_snapshots
            WHERE marketplace_account_id=? AND environment=? AND scope_key=? AND content_hash=?
            """,
            (int(account_id), environment, bundle.scope_key, content_hash),
        )
        if not found:
            raise RuntimeError("Impossibile salvare lo snapshot della tassonomia.")
        snapshot_id = int(found["id"])

    category_rows = [
        (
            snapshot_id,
            item.external_id,
            item.parent_external_id,
            item.code,
            item.label,
            item.path,
            int(item.level),
            int(item.is_leaf),
            item.product_type,
            json_text(item.required_attributes),
            json_text(item.raw),
        )
        for item in bundle.categories
    ]
    execute_many(
        """
        INSERT INTO taxonomy_categories(
            snapshot_id,external_id,parent_external_id,code,label,path,level,is_leaf,
            product_type,required_attributes_json,raw_json
        ) VALUES(?,?,?,?,?,?,?,?,?,?,?)
        """,
        category_rows,
    )

    attribute_rows = []
    value_rows = []
    condition_rows = []
    for item in bundle.attributes:
        attribute_rows.append(
            (
                snapshot_id,
                item.category_external_id,
                item.external_id,
                item.code,
                item.label,
                item.data_type,
                item.requirement_level,
                int(item.required),
                int(item.multiple),
                int(item.variant),
                item.unit,
                item.locale,
                item.value_list_code,
                json_text(item.constraints),
                json_text(item.raw),
            )
        )
        for position, value in enumerate(item.values):
            code = clean_text(
                value.get("code")
                or value.get("id")
                or value.get("value")
                or value.get("key")
            )
            if not code:
                continue
            label = clean_text(
                value.get("label")
                or value.get("name")
                or value.get("description")
                or code
            )
            value_rows.append(
                (
                    snapshot_id,
                    item.category_external_id,
                    item.external_id,
                    code,
                    label,
                    position,
                    json_text(value),
                )
            )
        for condition in item.conditions:
            condition_rows.append(
                (
                    snapshot_id,
                    item.category_external_id,
                    item.external_id,
                    json_hash(condition),
                    json_text(condition),
                )
            )
    execute_many(
        """
        INSERT INTO taxonomy_attributes(
            snapshot_id,category_external_id,external_id,code,label,data_type,
            requirement_level,required,multiple,variant,unit,locale,value_list_code,
            constraints_json,raw_json
        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        attribute_rows,
    )
    execute_many(
        """
        INSERT INTO taxonomy_attribute_values(
            snapshot_id,category_external_id,attribute_external_id,value_code,label,
            position,raw_json
        ) VALUES(?,?,?,?,?,?,?)
        """,
        value_rows,
    )
    execute_many(
        """
        INSERT INTO taxonomy_attribute_conditions(
            snapshot_id,category_external_id,attribute_external_id,condition_hash,condition_json
        ) VALUES(?,?,?,?,?)
        """,
        condition_rows,
    )
    locale_rows = []
    for item in bundle.locales:
        code = clean_text(item.get("code") or item.get("locale") or item.get("id"))
        if not code:
            continue
        locale_rows.append(
            (
                snapshot_id,
                code,
                clean_text(item.get("storefront") or bundle.storefront).lower(),
                clean_text(item.get("label") or item.get("name") or code),
                json_text(item),
            )
        )
    execute_many(
        """
        INSERT INTO taxonomy_locales(snapshot_id,code,storefront,label,raw_json)
        VALUES(?,?,?,?,?)
        """,
        locale_rows,
    )
    return snapshot_id, True


def latest_taxonomy_snapshot(
    account_id: int,
    *,
    environment: str = "live",
    scope_key: str = "",
) -> dict | None:
    ensure_schema()
    query = """
        SELECT * FROM taxonomy_snapshots
        WHERE marketplace_account_id=? AND environment=? AND active=1
    """
    params: list[Any] = [int(account_id), clean_text(environment) or "live"]
    if scope_key:
        query += " AND scope_key=?"
        params.append(scope_key)
    query += " ORDER BY created_at DESC,id DESC LIMIT 1"
    return row(query, params)


def taxonomy_categories(
    snapshot_id: int,
    *,
    leaf_only: bool = False,
    search: str = "",
    limit: int = 5000,
) -> list[dict]:
    ensure_schema()
    query = "SELECT * FROM taxonomy_categories WHERE snapshot_id=?"
    params: list[Any] = [int(snapshot_id)]
    if leaf_only:
        query += " AND is_leaf=1"
    if clean_text(search):
        query += " AND (LOWER(label) LIKE ? OR LOWER(path) LIKE ? OR LOWER(code) LIKE ?)"
        term = f"%{clean_text(search).lower()}%"
        params.extend((term, term, term))
    query += " ORDER BY path,label LIMIT ?"
    params.append(max(1, int(limit)))
    return rows(query, params)


def taxonomy_category(snapshot_id: int, category_external_id: str) -> dict | None:
    ensure_schema()
    return row(
        """
        SELECT * FROM taxonomy_categories
        WHERE snapshot_id=? AND external_id=?
        LIMIT 1
        """,
        (int(snapshot_id), clean_text(category_external_id)),
    )


def taxonomy_attributes(snapshot_id: int, category_external_id: str = "") -> list[dict]:
    ensure_schema()
    if category_external_id:
        return rows(
            """
            SELECT * FROM taxonomy_attributes
            WHERE snapshot_id=? AND (category_external_id=? OR category_external_id='')
            ORDER BY required DESC,label,external_id
            """,
            (int(snapshot_id), clean_text(category_external_id)),
        )
    return rows(
        """
        SELECT * FROM taxonomy_attributes
        WHERE snapshot_id=? ORDER BY category_external_id,required DESC,label,external_id
        """,
        (int(snapshot_id),),
    )


def create_source_snapshot(
    *,
    seller_id: int,
    supplier_id: int,
    price_list_id: int,
    saved_view_id: int | None,
    source_path: str,
    content_hash: str,
    row_count: int,
    columns: list[str],
    metadata: Mapping[str, Any] | None = None,
) -> tuple[int, bool]:
    ensure_schema()
    existing = row(
        """
        SELECT id FROM source_catalog_snapshots
        WHERE seller_id=? AND price_list_id=? AND COALESCE(saved_view_id,0)=? AND content_hash=?
        """,
        (int(seller_id), int(price_list_id), int(saved_view_id or 0), content_hash),
    )
    if existing:
        return int(existing["id"]), False
    snapshot_id = execute(
        """
        INSERT INTO source_catalog_snapshots(
            seller_id,supplier_id,price_list_id,saved_view_id,source_path,content_hash,
            row_count,columns_json,metadata_json,created_at
        ) VALUES(?,?,?,?,?,?,?,?,?,?)
        """,
        (
            int(seller_id),
            int(supplier_id),
            int(price_list_id),
            saved_view_id,
            clean_text(source_path),
            content_hash,
            max(0, int(row_count)),
            json_text(columns),
            json_text(dict(metadata or {})),
            now_iso(),
        ),
    )
    return snapshot_id, True


def _json_object(value: Any) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return dict(value)
    try:
        parsed = json.loads(str(value or "{}"))
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}
    return dict(parsed) if isinstance(parsed, Mapping) else {}


def source_snapshot_normalization_cache(
    source_snapshot_id: int,
    *,
    engine_version: int,
) -> dict[str, Any] | None:
    """Return a verified normalization cache for an immutable source snapshot.

    The cache is accepted only when the previous run completed with the same
    normalization engine and every canonical product still owns at least one
    persisted value.  An interrupted or mixed-version run is therefore rebuilt
    automatically instead of being mistaken for a complete catalogue.
    """
    ensure_schema()
    snapshot = row(
        "SELECT id,row_count,metadata_json FROM source_catalog_snapshots WHERE id=?",
        (int(source_snapshot_id),),
    )
    if not snapshot:
        return None
    metadata = _json_object(snapshot.get("metadata_json"))
    state = metadata.get("normalization")
    if not isinstance(state, Mapping):
        return None
    if clean_text(state.get("status")).upper() != "COMPLETED":
        return None
    try:
        cached_engine = int(state.get("engine_version") or 0)
    except (TypeError, ValueError):
        return None
    if cached_engine != int(engine_version):
        return None

    stats = row(
        """
        SELECT
            COUNT(DISTINCT cp.id) AS product_count,
            COUNT(DISTINCT cpv.canonical_product_id) AS value_product_count,
            COALESCE(AVG(cp.completeness_score),0) AS average_completeness
        FROM canonical_products cp
        LEFT JOIN canonical_product_values cpv ON cpv.canonical_product_id=cp.id
        WHERE cp.source_snapshot_id=?
        """,
        (int(source_snapshot_id),),
    ) or {}
    product_count = int(stats.get("product_count") or 0)
    value_product_count = int(stats.get("value_product_count") or 0)
    try:
        expected_unique = int(state.get("unique_product_count") or product_count)
    except (TypeError, ValueError):
        expected_unique = product_count
    if product_count != expected_unique or value_product_count != product_count:
        return None

    product_rows = rows(
        """
        SELECT id FROM canonical_products
        WHERE source_snapshot_id=? ORDER BY source_row_number,id
        """,
        (int(source_snapshot_id),),
    )
    return {
        "source_snapshot_id": int(source_snapshot_id),
        "row_count": int(state.get("input_row_count") or snapshot.get("row_count") or product_count),
        "normalized_count": int(state.get("normalized_count") or snapshot.get("row_count") or product_count),
        "unique_product_count": product_count,
        "product_ids": [int(item["id"]) for item in product_rows],
        "average_completeness": float(
            state.get("average_completeness")
            if state.get("average_completeness") is not None
            else stats.get("average_completeness") or 0
        ),
        "completed_at": clean_text(state.get("completed_at")),
        "duration_seconds": float(state.get("duration_seconds") or 0),
    }


def mark_source_snapshot_normalized(
    source_snapshot_id: int,
    *,
    engine_version: int,
    input_row_count: int,
    normalized_count: int,
    unique_product_count: int,
    average_completeness: float,
    duration_seconds: float,
) -> None:
    """Mark an immutable source snapshot as completely normalized."""
    ensure_schema()
    snapshot = row(
        "SELECT metadata_json FROM source_catalog_snapshots WHERE id=?",
        (int(source_snapshot_id),),
    )
    if not snapshot:
        raise RuntimeError("Snapshot catalogo sorgente non trovato.")
    metadata = _json_object(snapshot.get("metadata_json"))
    metadata["normalization"] = {
        "status": "COMPLETED",
        "engine_version": int(engine_version),
        "input_row_count": max(0, int(input_row_count)),
        "normalized_count": max(0, int(normalized_count)),
        "unique_product_count": max(0, int(unique_product_count)),
        "average_completeness": round(float(average_completeness or 0), 4),
        "duration_seconds": round(max(0.0, float(duration_seconds or 0)), 4),
        "completed_at": now_iso(),
    }
    execute(
        "UPDATE source_catalog_snapshots SET metadata_json=? WHERE id=?",
        (json_text(metadata), int(source_snapshot_id)),
    )


def _notify_progress(callback: Callable[[dict[str, Any]], None] | None, payload: dict[str, Any]) -> None:
    if callback is None:
        return
    try:
        callback(payload)
    except Exception:
        # A UI refresh must never invalidate the database transaction.
        return


def _id_chunks(values: list[int], size: int = 500) -> Iterable[list[int]]:
    for start in range(0, len(values), max(1, int(size))):
        yield values[start : start + max(1, int(size))]


def _chunks(values: list[Any], size: int) -> Iterable[list[Any]]:
    step = max(1, int(size))
    for index in range(0, len(values), step):
        yield values[index:index + step]


def _db_row_value(item: Any, key: str, position: int = 0) -> Any:
    if isinstance(item, Mapping):
        return item.get(key)
    try:
        return item[key]
    except (KeyError, TypeError, IndexError):
        return item[position]


def save_canonical_products(
    *,
    seller_id: int,
    supplier_id: int,
    price_list_id: int,
    source_snapshot_id: int,
    products: Iterable[CanonicalProduct],
    progress_callback: Callable[[dict[str, Any]], None] | None = None,
    batch_size: int = 1200,
) -> list[int]:
    """Persist a catalogue with a low-write bulk path.

    v252 keeps the complete supplier row exactly once in ``raw_json`` and the
    compact canonical representation in ``normalized_json``.  Provenance is
    embedded compactly in ``_provenance`` and can be reconstructed on demand.
    The old implementation wrote tens of thousands of evidence/value rows while
    importing the catalogue; those writes were not used by classification/feed
    preparation and were the main SQLite bottleneck on Windows.

    A single lightweight ``_persist_marker`` row per product preserves the cache
    completeness invariant.  Existing relational evidence is removed only when
    a product actually changes and is materialized lazily by ``product_evidence``.
    """
    ensure_schema()
    product_list = list(products)
    total = len(product_list)
    if not product_list:
        _notify_progress(
            progress_callback,
            {"phase": "PERSIST", "completed": 0, "total": 0, "phase_percent": 100.0},
        )
        return []

    unique_by_key: dict[str, CanonicalProduct] = {}
    for product in product_list:
        unique_by_key[product.source_row_key] = product
    unique_products = list(unique_by_key.values())

    upsert_sql = """
        INSERT INTO canonical_products(
            seller_id,supplier_id,price_list_id,source_snapshot_id,source_row_key,
            source_row_number,ean,supplier_sku,brand,model,title,description,
            normalized_json,raw_json,content_hash,completeness_score,status,created_at,updated_at
        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(source_snapshot_id,source_row_key) DO UPDATE SET
            seller_id=excluded.seller_id,
            supplier_id=excluded.supplier_id,
            price_list_id=excluded.price_list_id,
            source_row_number=excluded.source_row_number,
            ean=excluded.ean,
            supplier_sku=excluded.supplier_sku,
            brand=excluded.brand,
            model=excluded.model,
            title=excluded.title,
            description=excluded.description,
            normalized_json=excluded.normalized_json,
            raw_json=excluded.raw_json,
            content_hash=excluded.content_hash,
            completeness_score=excluded.completeness_score,
            status=excluded.status,
            updated_at=excluded.updated_at
    """
    marker_sql = """
        INSERT INTO canonical_product_values(
            canonical_product_id,field_name,value_json,data_type,source_kind,
            evidence_id,confidence,updated_at
        ) VALUES(?,?,?,?,?,?,?,?)
        ON CONFLICT(canonical_product_id,field_name) DO UPDATE SET
            value_json=excluded.value_json,updated_at=excluded.updated_at
    """

    saved_by_key: dict[str, int] = {}
    processed = 0
    with connect() as con:
        existing_rows = con.execute(
            """SELECT id,source_row_key,content_hash FROM canonical_products
               WHERE source_snapshot_id=?""",
            (int(source_snapshot_id),),
        ).fetchall()
        existing: dict[str, tuple[int, str]] = {
            str(_db_row_value(item, "source_row_key", 1)): (
                int(_db_row_value(item, "id", 0) or 0),
                str(_db_row_value(item, "content_hash", 2) or ""),
            )
            for item in existing_rows
        }

        for batch in _chunks(unique_products, max(100, int(batch_size))):
            timestamp = now_iso()
            changed: list[CanonicalProduct] = []
            upsert_values: list[tuple[Any, ...]] = []
            for product in batch:
                found = existing.get(product.source_row_key)
                if found and found[1] == product.content_hash:
                    saved_by_key[product.source_row_key] = found[0]
                    continue
                changed.append(product)
                compact_normalized = dict(product.normalized)
                # The complete supplier row is already stored in raw_json.
                # Duplicating source_attributes inside normalized_json roughly
                # doubled IO for full Innpro feeds without adding information.
                compact_normalized.pop("source_attributes", None)
                upsert_values.append(
                    (
                        int(seller_id), int(supplier_id), int(price_list_id),
                        int(source_snapshot_id), product.source_row_key,
                        int(product.source_row_number), product.ean, product.supplier_sku,
                        product.brand, product.model, product.title, product.description,
                        json_text(compact_normalized), json_text(product.raw), product.content_hash,
                        float(product.completeness_score), "NORMALIZED", timestamp, timestamp,
                    )
                )

            if upsert_values:
                con.executemany(upsert_sql, upsert_values)
                keys = [item.source_row_key for item in changed]
                placeholders = ",".join("?" for _ in keys)
                found_rows = con.execute(
                    f"""SELECT id,source_row_key FROM canonical_products
                    WHERE source_snapshot_id=? AND source_row_key IN ({placeholders})""",
                    (int(source_snapshot_id), *keys),
                ).fetchall()
                changed_ids: list[int] = []
                for item in found_rows:
                    product_id = int(_db_row_value(item, "id", 0) or 0)
                    key = str(_db_row_value(item, "source_row_key", 1))
                    saved_by_key[key] = product_id
                    changed_ids.append(product_id)
                missing = [key for key in keys if not saved_by_key.get(key)]
                if missing:
                    raise RuntimeError(
                        "Impossibile recuperare gli ID dei prodotti normalizzati: "
                        + ", ".join(missing[:3])
                    )
                if changed_ids:
                    id_placeholders = ",".join("?" for _ in changed_ids)
                    con.execute(
                        f"DELETE FROM canonical_product_values WHERE canonical_product_id IN ({id_placeholders})",
                        tuple(changed_ids),
                    )
                    con.execute(
                        f"DELETE FROM product_evidence WHERE canonical_product_id IN ({id_placeholders})",
                        tuple(changed_ids),
                    )
                    # Keep one small relational provenance row for backwards
                    # compatibility/debugging. Full provenance stays compact in
                    # normalized_json and is reconstructed lazily on demand.
                    title_evidence_rows = []
                    for product in changed:
                        product_id = saved_by_key[product.source_row_key]
                        evidence = next((item for item in product.evidence if item.canonical_field == "title"), None)
                        if evidence is not None:
                            title_evidence_rows.append((
                                product_id, evidence.canonical_field, evidence.source_field, evidence.source_path,
                                json_text(evidence.source_value), evidence.source_file, int(evidence.source_row),
                                evidence.source_hash, timestamp,
                            ))
                    if title_evidence_rows:
                        con.executemany(
                            """INSERT INTO product_evidence(
                                canonical_product_id,canonical_field,source_field,source_path,
                                source_value_json,source_file,source_row,source_hash,created_at
                            ) VALUES(?,?,?,?,?,?,?,?,?)""",
                            title_evidence_rows,
                        )
                    con.executemany(
                        marker_sql,
                        [
                            (product_id, "_persist_marker", "true", "BOOLEAN", "SYSTEM", None, 1.0, timestamp)
                            for product_id in changed_ids
                        ],
                    )

            con.commit()
            processed += len(batch)
            _notify_progress(
                progress_callback,
                {
                    "phase": "PERSIST",
                    "completed": min(processed, len(unique_products)),
                    "total": len(unique_products),
                    "phase_percent": round(
                        (min(processed, len(unique_products)) / len(unique_products)) * 100.0, 2
                    ) if unique_products else 100.0,
                },
            )

    return [saved_by_key[item.source_row_key] for item in product_list]

def canonical_products_for_source(source_snapshot_id: int, *, limit: int = 10000) -> list[dict]:
    ensure_schema()
    return rows(
        """
        SELECT * FROM canonical_products
        WHERE source_snapshot_id=? ORDER BY source_row_number,id LIMIT ?
        """,
        (int(source_snapshot_id), max(1, int(limit))),
    )



def source_snapshot(snapshot_id: int) -> dict | None:
    ensure_schema()
    return row("SELECT * FROM source_catalog_snapshots WHERE id=?", (int(snapshot_id),))


def source_snapshots(
    *,
    seller_id: int,
    price_list_id: int | None = None,
    limit: int = 100,
) -> list[dict]:
    ensure_schema()
    query = "SELECT * FROM source_catalog_snapshots WHERE seller_id=?"
    params: list[Any] = [int(seller_id)]
    if price_list_id is not None:
        query += " AND price_list_id=?"
        params.append(int(price_list_id))
    query += " ORDER BY created_at DESC,id DESC LIMIT ?"
    params.append(max(1, int(limit)))
    return rows(query, params)


def canonical_product(product_id: int) -> dict | None:
    ensure_schema()
    return row("SELECT * FROM canonical_products WHERE id=?", (int(product_id),))


def canonical_products_for_seller(
    seller_id: int,
    *,
    price_list_id: int | None = None,
    source_snapshot_id: int | None = None,
    status: str = "",
    limit: int = 10000,
) -> list[dict]:
    ensure_schema()
    query = "SELECT * FROM canonical_products WHERE seller_id=?"
    params: list[Any] = [int(seller_id)]
    if price_list_id is not None:
        query += " AND price_list_id=?"
        params.append(int(price_list_id))
    if source_snapshot_id is not None:
        query += " AND source_snapshot_id=?"
        params.append(int(source_snapshot_id))
    if clean_text(status):
        query += " AND status=?"
        params.append(clean_text(status).upper())
    query += " ORDER BY updated_at DESC,source_row_number,id LIMIT ?"
    params.append(max(1, int(limit)))
    return rows(query, params)


def taxonomy_sync_runs(account_id: int, *, environment: str = "", limit: int = 50) -> list[dict]:
    ensure_schema()
    query = "SELECT * FROM taxonomy_sync_runs WHERE marketplace_account_id=?"
    params: list[Any] = [int(account_id)]
    if clean_text(environment):
        query += " AND environment=?"
        params.append(clean_text(environment).lower())
    query += " ORDER BY started_at DESC,id DESC LIMIT ?"
    params.append(max(1, int(limit)))
    return rows(query, params)


def taxonomy_attribute_values(
    snapshot_id: int,
    attribute_external_id: str,
    *,
    category_external_id: str = "",
    limit: int = 5000,
) -> list[dict]:
    ensure_schema()
    query = """
        SELECT * FROM taxonomy_attribute_values
        WHERE snapshot_id=? AND attribute_external_id=?
    """
    params: list[Any] = [int(snapshot_id), clean_text(attribute_external_id)]
    if category_external_id:
        query += " AND (category_external_id=? OR category_external_id='')"
        params.append(clean_text(category_external_id))
    query += " ORDER BY position,label,value_code LIMIT ?"
    params.append(max(1, int(limit)))
    return rows(query, params)


def create_publication_job(
    *,
    seller_id: int,
    account_id: int,
    marketplace: str,
    environment: str,
    storefront: str,
    locale: str,
    price_list_id: int,
    source_snapshot_id: int,
    taxonomy_snapshot_id: int | None,
    settings: Mapping[str, Any],
    product_ids: Iterable[int],
) -> int:
    ensure_schema()
    products = list(dict.fromkeys(int(value) for value in product_ids if int(value) > 0))
    idempotency_key = json_hash(
        {
            "account_id": int(account_id),
            "environment": environment,
            "storefront": storefront,
            "locale": locale,
            "source_snapshot_id": int(source_snapshot_id),
            "taxonomy_snapshot_id": taxonomy_snapshot_id,
            "product_ids": products,
            "settings": dict(settings),
        }
    )
    existing = row(
        """
        SELECT id FROM publication_jobs
        WHERE marketplace_account_id=? AND idempotency_key=?
        """,
        (int(account_id), idempotency_key),
    )
    if existing:
        return int(existing["id"])
    job_id = execute(
        """
        INSERT INTO publication_jobs(
            seller_id,marketplace_account_id,marketplace,environment,storefront,locale,
            price_list_id,source_snapshot_id,taxonomy_snapshot_id,job_type,status,total_items,
            settings_json,idempotency_key,created_at,updated_at
        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            int(seller_id),
            int(account_id),
            clean_text(marketplace).lower(),
            clean_text(environment) or "live",
            clean_text(storefront),
            clean_text(locale),
            int(price_list_id),
            int(source_snapshot_id),
            taxonomy_snapshot_id,
            "PRODUCT_CREATION",
            "CREATED",
            len(products),
            json_text(dict(settings)),
            idempotency_key,
            now_iso(),
            now_iso(),
        ),
    )
    execute_many(
        """
        INSERT INTO publication_items(
            publication_job_id,canonical_product_id,status,created_at,updated_at
        ) VALUES(?,?,?,?,?)
        """,
        [(job_id, product_id, "CREATED", now_iso(), now_iso()) for product_id in products],
    )
    return job_id


def publication_jobs(seller_id: int, account_id: int | None = None, limit: int = 100) -> list[dict]:
    ensure_schema()
    query = "SELECT * FROM publication_jobs WHERE seller_id=?"
    params: list[Any] = [int(seller_id)]
    if account_id is not None:
        query += " AND marketplace_account_id=?"
        params.append(int(account_id))
    query += " ORDER BY created_at DESC,id DESC LIMIT ?"
    params.append(max(1, int(limit)))
    return rows(query, params)


def record_validation_run(
    *,
    seller_id: int,
    account_id: int,
    taxonomy_snapshot_id: int | None,
    job_id: int | None,
    issues_by_product: Mapping[int, Iterable[ValidationIssue]],
) -> int:
    ensure_schema()
    product_ids = list(issues_by_product)
    valid = 0
    warning = 0
    invalid = 0
    normalized: dict[int, list[ValidationIssue]] = {}
    for product_id, issues in issues_by_product.items():
        items = list(issues)
        normalized[int(product_id)] = items
        severities = {item.severity.upper() for item in items}
        if "ERROR" in severities or "BLOCKER" in severities:
            invalid += 1
        elif "WARNING" in severities:
            warning += 1
        else:
            valid += 1
    run_id = execute(
        """
        INSERT INTO validation_runs(
            seller_id,marketplace_account_id,taxonomy_snapshot_id,publication_job_id,
            status,product_count,valid_count,warning_count,invalid_count,started_at,completed_at
        ) VALUES(?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            int(seller_id),
            int(account_id),
            taxonomy_snapshot_id,
            job_id,
            "COMPLETED",
            len(product_ids),
            valid,
            warning,
            invalid,
            now_iso(),
            now_iso(),
        ),
    )
    issue_rows = []
    for product_id, issues in normalized.items():
        for issue in issues:
            issue_rows.append(
                (
                    run_id,
                    product_id,
                    issue.severity.upper(),
                    issue.code,
                    issue.field_name,
                    issue.message,
                    json_text(issue.details),
                    now_iso(),
                )
            )
    execute_many(
        """
        INSERT INTO validation_issues(
            validation_run_id,canonical_product_id,severity,code,field_name,message,
            details_json,created_at
        ) VALUES(?,?,?,?,?,?,?,?)
        """,
        issue_rows,
    )
    return run_id



def _raw_path_value(raw: Mapping[str, Any], source_field: str) -> Any:
    path = clean_text(source_field)
    if not path:
        return None
    current: Any = raw
    for part in path.split("."):
        if not isinstance(current, Mapping):
            return None
        if part in current:
            current = current.get(part)
            continue
        wanted = part.lower()
        found = next((key for key in current if str(key).lower() == wanted), None)
        if found is None:
            return None
        current = current.get(found)
    return current


def product_evidence(product_id: int) -> list[dict]:
    """Return provenance, reconstructing v252 compact evidence on demand."""
    ensure_schema()
    persisted = rows(
        """SELECT * FROM product_evidence WHERE canonical_product_id=?
        ORDER BY canonical_field,source_field,id""",
        (int(product_id),),
    )
    product = row(
        "SELECT normalized_json,raw_json FROM canonical_products WHERE id=?",
        (int(product_id),),
    )
    if not product:
        return []
    normalized = load_json(product.get("normalized_json"), {})
    raw = load_json(product.get("raw_json"), {})
    if not isinstance(normalized, Mapping):
        normalized = {}
    if not isinstance(raw, Mapping):
        raw = {}
    result: list[dict] = list(persisted)
    persisted_keys = {
        (clean_text(item.get("canonical_field")), clean_text(item.get("source_field")))
        for item in persisted
    }
    for index, item in enumerate(normalized.get("_provenance") or [], start=1):
        if not isinstance(item, Mapping):
            continue
        source_field = clean_text(item.get("source_field"))
        key = (clean_text(item.get("canonical_field")), source_field)
        if key in persisted_keys:
            continue
        result.append(
            {
                "id": -index,
                "canonical_product_id": int(product_id),
                "canonical_field": clean_text(item.get("canonical_field")),
                "source_field": source_field,
                "source_path": clean_text(item.get("source_path")),
                "source_value_json": json_text(_raw_path_value(raw, source_field)),
                "source_file": clean_text(item.get("source_file")),
                "source_row": int(item.get("source_row") or 0),
                "source_hash": clean_text(item.get("source_hash")),
                "created_at": "",
            }
        )
    return result

def _attribute_serializable(item: TaxonomyAttribute | Mapping[str, Any]) -> dict[str, Any]:
    if isinstance(item, TaxonomyAttribute):
        return {
            "external_id": item.external_id,
            "category_external_id": item.category_external_id,
            "code": item.code,
            "label": item.label,
            "data_type": item.data_type,
            "requirement_level": item.requirement_level,
            "required": bool(item.required),
            "multiple": bool(item.multiple),
            "variant": bool(item.variant),
            "unit": item.unit,
            "locale": item.locale,
            "value_list_code": item.value_list_code,
            "constraints": dict(item.constraints),
            "values": list(item.values),
            "conditions": list(item.conditions),
            "raw": dict(item.raw),
        }
    raw = dict(item)
    if "constraints_json" in raw and "constraints" not in raw:
        raw["constraints"] = json.loads(raw.get("constraints_json") or "{}")
    if "raw_json" in raw and "raw" not in raw:
        raw["raw"] = json.loads(raw.get("raw_json") or "{}")
    return raw


def save_taxonomy_category_enrichment(
    *,
    seller_id: int,
    account_id: int,
    snapshot_id: int,
    marketplace: str,
    environment: str,
    scope_key: str,
    category_external_id: str,
    category: Mapping[str, Any] | None,
    attributes: Iterable[TaxonomyAttribute | Mapping[str, Any]],
    status: str = "COMPLETED",
    error: str = "",
) -> int:
    ensure_schema()
    serialized_attributes = [_attribute_serializable(item) for item in attributes]
    payload = {
        "category": dict(category or {}),
        "attributes": serialized_attributes,
    }
    content_hash = json_hash(payload)
    now = now_iso()
    execute(
        """
        INSERT INTO taxonomy_category_enrichments(
            seller_id,marketplace_account_id,taxonomy_snapshot_id,marketplace,
            environment,scope_key,category_external_id,status,category_json,
            attributes_json,content_hash,attribute_count,value_count,error,
            fetched_at,updated_at
        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(
            marketplace_account_id,environment,scope_key,taxonomy_snapshot_id,category_external_id
        ) DO UPDATE SET
            seller_id=excluded.seller_id,
            marketplace=excluded.marketplace,
            status=excluded.status,
            category_json=excluded.category_json,
            attributes_json=excluded.attributes_json,
            content_hash=excluded.content_hash,
            attribute_count=excluded.attribute_count,
            value_count=excluded.value_count,
            error=excluded.error,
            fetched_at=excluded.fetched_at,
            updated_at=excluded.updated_at
        """,
        (
            int(seller_id),
            int(account_id),
            int(snapshot_id),
            clean_text(marketplace).lower(),
            clean_text(environment).lower() or "live",
            clean_text(scope_key),
            clean_text(category_external_id),
            clean_text(status).upper() or "COMPLETED",
            json_text(dict(category or {})),
            json_text(serialized_attributes),
            content_hash,
            len(serialized_attributes),
            sum(len(item.get("values") or []) for item in serialized_attributes),
            clean_text(error)[:4000],
            now,
            now,
        ),
    )
    found = row(
        """
        SELECT id FROM taxonomy_category_enrichments
        WHERE marketplace_account_id=? AND environment=? AND scope_key=?
          AND taxonomy_snapshot_id=? AND category_external_id=?
        """,
        (
            int(account_id),
            clean_text(environment).lower() or "live",
            clean_text(scope_key),
            int(snapshot_id),
            clean_text(category_external_id),
        ),
    )
    if not found:
        raise RuntimeError("Impossibile memorizzare gli attributi della categoria.")
    return int(found["id"])


def taxonomy_category_enrichment(
    account_id: int,
    *,
    snapshot_id: int,
    environment: str,
    scope_key: str,
    category_external_id: str,
) -> dict | None:
    ensure_schema()
    return row(
        """
        SELECT * FROM taxonomy_category_enrichments
        WHERE marketplace_account_id=? AND environment=? AND scope_key=?
          AND taxonomy_snapshot_id=? AND category_external_id=?
        LIMIT 1
        """,
        (
            int(account_id),
            clean_text(environment).lower() or "live",
            clean_text(scope_key),
            int(snapshot_id),
            clean_text(category_external_id),
        ),
    )


def start_category_classification(
    *,
    seller_id: int,
    account_id: int,
    taxonomy_snapshot_id: int,
    source_snapshot_id: int,
    marketplace: str,
    environment: str,
    storefront: str,
    locale: str,
    product_count: int,
    settings: Mapping[str, Any] | None = None,
) -> int:
    ensure_schema()
    return execute(
        """
        INSERT INTO category_classification_runs(
            seller_id,marketplace_account_id,taxonomy_snapshot_id,source_snapshot_id,
            marketplace,environment,storefront,locale,status,product_count,
            settings_json,started_at
        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            int(seller_id),
            int(account_id),
            int(taxonomy_snapshot_id),
            int(source_snapshot_id),
            clean_text(marketplace).lower(),
            clean_text(environment).lower() or "live",
            clean_text(storefront).lower(),
            clean_text(locale),
            "RUNNING",
            max(0, int(product_count)),
            json_text(dict(settings or {})),
            now_iso(),
        ),
    )


def complete_category_classification(
    run_id: int,
    *,
    status: str,
    classified_count: int = 0,
    review_count: int = 0,
    blocked_count: int = 0,
    error: str = "",
) -> None:
    ensure_schema()
    execute(
        """
        UPDATE category_classification_runs SET
            status=?,classified_count=?,review_count=?,blocked_count=?,error=?,completed_at=?
        WHERE id=?
        """,
        (
            clean_text(status).upper(),
            max(0, int(classified_count)),
            max(0, int(review_count)),
            max(0, int(blocked_count)),
            clean_text(error)[:4000],
            now_iso(),
            int(run_id),
        ),
    )


def save_category_candidates(
    *,
    run_id: int,
    product_id: int,
    candidates: Iterable[Mapping[str, Any]],
) -> None:
    ensure_schema()
    execute(
        "DELETE FROM product_category_candidates WHERE classification_run_id=? AND canonical_product_id=?",
        (int(run_id), int(product_id)),
    )
    values = []
    for rank_value, item in enumerate(candidates, start=1):
        rank = int(item.get("rank") or rank_value)
        values.append(
            (
                int(run_id),
                int(product_id),
                rank,
                clean_text(item.get("category_external_id") or item.get("external_id")),
                clean_text(item.get("category_label") or item.get("label")),
                clean_text(item.get("category_path") or item.get("path")),
                float(item.get("score") or 0.0),
                clean_text(item.get("source") or "LOCAL_TAXONOMY"),
                json_text(dict(item.get("signals") or {})),
                json_text(dict(item.get("raw") or {})),
                now_iso(),
            )
        )
    execute_many(
        """
        INSERT INTO product_category_candidates(
            classification_run_id,canonical_product_id,rank,category_external_id,
            category_label,category_path,score,candidate_source,signals_json,raw_json,created_at
        ) VALUES(?,?,?,?,?,?,?,?,?,?,?)
        """,
        values,
    )


def upsert_category_assignment(
    *,
    seller_id: int,
    account_id: int,
    taxonomy_snapshot_id: int,
    source_snapshot_id: int,
    classification_run_id: int | None,
    product_id: int,
    marketplace: str,
    storefront: str,
    category_external_id: str,
    category_label: str,
    category_path: str,
    decision_source: str,
    confidence: float,
    status: str,
    classification_signature: str,
    evidence: Mapping[str, Any] | None = None,
    approved_by: str = "",
) -> int:
    ensure_schema()
    now = now_iso()
    execute(
        """
        INSERT INTO product_category_assignments(
            seller_id,marketplace_account_id,taxonomy_snapshot_id,source_snapshot_id,
            classification_run_id,canonical_product_id,marketplace,storefront,
            category_external_id,category_label,category_path,decision_source,
            confidence,status,classification_signature,evidence_json,approved_by,
            created_at,updated_at
        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(marketplace_account_id,source_snapshot_id,canonical_product_id) DO UPDATE SET
            taxonomy_snapshot_id=excluded.taxonomy_snapshot_id,
            classification_run_id=excluded.classification_run_id,
            marketplace=excluded.marketplace,
            storefront=excluded.storefront,
            category_external_id=excluded.category_external_id,
            category_label=excluded.category_label,
            category_path=excluded.category_path,
            decision_source=excluded.decision_source,
            confidence=excluded.confidence,
            status=excluded.status,
            classification_signature=excluded.classification_signature,
            evidence_json=excluded.evidence_json,
            approved_by=excluded.approved_by,
            updated_at=excluded.updated_at
        """,
        (
            int(seller_id),
            int(account_id),
            int(taxonomy_snapshot_id),
            int(source_snapshot_id),
            classification_run_id,
            int(product_id),
            clean_text(marketplace).lower(),
            clean_text(storefront).lower(),
            clean_text(category_external_id),
            clean_text(category_label),
            clean_text(category_path),
            clean_text(decision_source).upper() or "LOCAL_TAXONOMY",
            max(0.0, min(100.0, float(confidence))),
            clean_text(status).upper() or "REVIEW",
            clean_text(classification_signature),
            json_text(dict(evidence or {})),
            clean_text(approved_by),
            now,
            now,
        ),
    )
    found = row(
        """
        SELECT id FROM product_category_assignments
        WHERE marketplace_account_id=? AND source_snapshot_id=? AND canonical_product_id=?
        """,
        (int(account_id), int(source_snapshot_id), int(product_id)),
    )
    if not found:
        raise RuntimeError("Impossibile memorizzare la categoria proposta.")
    return int(found["id"])


def category_assignments_for_source(
    *,
    seller_id: int,
    account_id: int,
    source_snapshot_id: int,
    statuses: Iterable[str] = (),
    limit: int = 10000,
) -> list[dict]:
    ensure_schema()
    query = """
        SELECT a.*,p.ean,p.supplier_sku,p.brand,p.title,p.description,
               p.normalized_json,p.raw_json,p.completeness_score,p.supplier_id,p.price_list_id
        FROM product_category_assignments a
        JOIN canonical_products p ON p.id=a.canonical_product_id
        WHERE a.seller_id=? AND a.marketplace_account_id=? AND a.source_snapshot_id=?
    """
    params: list[Any] = [int(seller_id), int(account_id), int(source_snapshot_id)]
    status_values = [clean_text(value).upper() for value in statuses if clean_text(value)]
    if status_values:
        placeholders = ",".join("?" for _ in status_values)
        query += f" AND a.status IN ({placeholders})"
        params.extend(status_values)
    query += " ORDER BY p.source_row_number,p.id LIMIT ?"
    params.append(max(1, int(limit)))
    return rows(query, params)


def category_candidates_for_run(run_id: int, *, product_id: int | None = None) -> list[dict]:
    ensure_schema()
    query = "SELECT * FROM product_category_candidates WHERE classification_run_id=?"
    params: list[Any] = [int(run_id)]
    if product_id is not None:
        query += " AND canonical_product_id=?"
        params.append(int(product_id))
    query += " ORDER BY canonical_product_id,rank"
    return rows(query, params)


def category_classification_runs(
    *, seller_id: int, account_id: int | None = None, limit: int = 100
) -> list[dict]:
    ensure_schema()
    query = "SELECT * FROM category_classification_runs WHERE seller_id=?"
    params: list[Any] = [int(seller_id)]
    if account_id is not None:
        query += " AND marketplace_account_id=?"
        params.append(int(account_id))
    query += " ORDER BY started_at DESC,id DESC LIMIT ?"
    params.append(max(1, int(limit)))
    return rows(query, params)


def upsert_category_mapping_rule(
    *,
    seller_id: int,
    supplier_id: int | None,
    marketplace: str,
    storefront: str,
    source_signature: str,
    source_label: str,
    category_external_id: str,
    category_label: str,
    confidence: float = 1.0,
    status: str = "APPROVED",
) -> int:
    ensure_schema()
    now = now_iso()
    marketplace_value = clean_text(marketplace).lower()
    storefront_value = clean_text(storefront).lower()
    signature_value = clean_text(source_signature)
    existing = row(
        """
        SELECT id FROM category_mapping_rules
        WHERE seller_id=? AND COALESCE(supplier_id,0)=? AND marketplace=?
          AND storefront=? AND source_signature=?
        ORDER BY id DESC LIMIT 1
        """,
        (int(seller_id), int(supplier_id or 0), marketplace_value, storefront_value, signature_value),
    )
    values = (
        clean_text(source_label),
        clean_text(category_external_id),
        clean_text(category_label),
        max(0.0, min(1.0, float(confidence))),
        clean_text(status).upper() or "APPROVED",
        now,
    )
    if existing:
        rule_id = int(existing["id"])
        execute(
            """
            UPDATE category_mapping_rules SET
                source_label=?,category_external_id=?,category_label=?,confidence=?,status=?,updated_at=?
            WHERE id=?
            """,
            values + (rule_id,),
        )
        return rule_id
    rule_id = execute(
        """
        INSERT INTO category_mapping_rules(
            seller_id,supplier_id,marketplace,storefront,source_signature,source_label,
            category_external_id,category_label,confidence,status,created_at,updated_at
        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            int(seller_id),supplier_id,marketplace_value,storefront_value,signature_value,
            clean_text(source_label),clean_text(category_external_id),clean_text(category_label),
            max(0.0,min(1.0,float(confidence))),clean_text(status).upper() or "APPROVED",now,now,
        ),
    )
    if rule_id:
        return int(rule_id)
    found = row(
        """
        SELECT id FROM category_mapping_rules
        WHERE seller_id=? AND COALESCE(supplier_id,0)=? AND marketplace=?
          AND storefront=? AND source_signature=?
        ORDER BY id DESC LIMIT 1
        """,
        (int(seller_id), int(supplier_id or 0), marketplace_value, storefront_value, signature_value),
    )
    if not found:
        raise RuntimeError("Impossibile memorizzare la regola categoria.")
    return int(found["id"])


def find_category_mapping_rule(
    *,
    seller_id: int,
    supplier_id: int | None,
    marketplace: str,
    storefront: str,
    source_signature: str,
) -> dict | None:
    ensure_schema()
    return row(
        """
        SELECT * FROM category_mapping_rules
        WHERE seller_id=? AND (supplier_id=? OR supplier_id IS NULL)
          AND marketplace=? AND storefront=? AND source_signature=? AND status='APPROVED'
        ORDER BY CASE WHEN supplier_id=? THEN 0 ELSE 1 END,id DESC LIMIT 1
        """,
        (
            int(seller_id),
            supplier_id,
            clean_text(marketplace).lower(),
            clean_text(storefront).lower(),
            clean_text(source_signature),
            supplier_id,
        ),
    )


def save_feed_preparation(
    *,
    seller_id: int,
    account_id: int,
    taxonomy_snapshot_id: int,
    source_snapshot_id: int,
    product_id: int,
    category_external_id: str,
    marketplace: str,
    storefront: str,
    locale: str,
    product_payload: Mapping[str, Any],
    offer_payload: Mapping[str, Any],
    mapped_attributes: Mapping[str, Any],
    missing_fields: Iterable[str],
    issues: Iterable[ValidationIssue | Mapping[str, Any]],
    validation_status: str,
    readiness_score: float,
    payload_hash: str,
) -> int:
    ensure_schema()
    issue_payload = []
    for item in issues:
        if isinstance(item, ValidationIssue):
            issue_payload.append(
                {
                    "severity": item.severity,
                    "code": item.code,
                    "message": item.message,
                    "field_name": item.field_name,
                    "details": item.details,
                }
            )
        else:
            issue_payload.append(dict(item))
    now = now_iso()
    execute(
        """
        INSERT INTO product_feed_preparations(
            seller_id,marketplace_account_id,taxonomy_snapshot_id,source_snapshot_id,
            canonical_product_id,category_external_id,marketplace,storefront,locale,
            product_payload_json,offer_payload_json,mapped_attributes_json,
            missing_fields_json,issues_json,validation_status,readiness_score,
            payload_hash,created_at,updated_at
        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(
            marketplace_account_id,source_snapshot_id,canonical_product_id,category_external_id
        ) DO UPDATE SET
            taxonomy_snapshot_id=excluded.taxonomy_snapshot_id,
            marketplace=excluded.marketplace,
            storefront=excluded.storefront,
            locale=excluded.locale,
            product_payload_json=excluded.product_payload_json,
            offer_payload_json=excluded.offer_payload_json,
            mapped_attributes_json=excluded.mapped_attributes_json,
            missing_fields_json=excluded.missing_fields_json,
            issues_json=excluded.issues_json,
            validation_status=excluded.validation_status,
            readiness_score=excluded.readiness_score,
            payload_hash=excluded.payload_hash,
            updated_at=excluded.updated_at
        """,
        (
            int(seller_id),
            int(account_id),
            int(taxonomy_snapshot_id),
            int(source_snapshot_id),
            int(product_id),
            clean_text(category_external_id),
            clean_text(marketplace).lower(),
            clean_text(storefront).lower(),
            clean_text(locale),
            json_text(dict(product_payload)),
            json_text(dict(offer_payload)),
            json_text(dict(mapped_attributes)),
            json_text(list(missing_fields)),
            json_text(issue_payload),
            clean_text(validation_status).upper(),
            max(0.0, min(100.0, float(readiness_score))),
            clean_text(payload_hash),
            now,
            now,
        ),
    )
    found = row(
        """
        SELECT id FROM product_feed_preparations
        WHERE marketplace_account_id=? AND source_snapshot_id=?
          AND canonical_product_id=? AND category_external_id=?
        """,
        (int(account_id), int(source_snapshot_id), int(product_id), clean_text(category_external_id)),
    )
    if not found:
        raise RuntimeError("Impossibile memorizzare la scheda prodotto preparata.")
    return int(found["id"])


def feed_preparations_for_source(
    *,
    seller_id: int,
    account_id: int,
    source_snapshot_id: int,
    statuses: Iterable[str] = (),
    limit: int = 10000,
) -> list[dict]:
    ensure_schema()
    query = """
        SELECT f.*,p.ean,p.supplier_sku,p.brand,p.title,p.description,p.source_row_number
        FROM product_feed_preparations f
        JOIN canonical_products p ON p.id=f.canonical_product_id
        WHERE f.seller_id=? AND f.marketplace_account_id=? AND f.source_snapshot_id=?
    """
    params: list[Any] = [int(seller_id), int(account_id), int(source_snapshot_id)]
    status_values = [clean_text(value).upper() for value in statuses if clean_text(value)]
    if status_values:
        placeholders = ",".join("?" for _ in status_values)
        query += f" AND f.validation_status IN ({placeholders})"
        params.extend(status_values)
    query += " ORDER BY p.source_row_number,p.id LIMIT ?"
    params.append(max(1, int(limit)))
    return rows(query, params)


def feed_preparation(preparation_id: int) -> dict | None:
    ensure_schema()
    return row("SELECT * FROM product_feed_preparations WHERE id=?", (int(preparation_id),))


def apply_feed_preparations_to_job(job_id: int, preparations: Iterable[Mapping[str, Any]]) -> None:
    ensure_schema()
    ready = failed = review = 0
    for item in preparations:
        status = clean_text(item.get("validation_status")).upper()
        if status == "READY":
            publication_status = "READY"
            ready += 1
        elif status in {"VALID_WITH_WARNINGS", "REVIEW"}:
            publication_status = "REVIEW_REQUIRED"
            review += 1
        else:
            publication_status = "BLOCKED"
            failed += 1
        execute(
            """
            UPDATE publication_items SET
                status=?,product_status=?,payload_hash=?,product_payload_json=?,
                offer_payload_json=?,last_error=?,updated_at=?
            WHERE publication_job_id=? AND canonical_product_id=?
            """,
            (
                publication_status,
                "PREPARED" if publication_status != "BLOCKED" else "BLOCKED",
                clean_text(item.get("payload_hash")),
                item.get("product_payload_json") or "{}",
                item.get("offer_payload_json") or "{}",
                "" if publication_status != "BLOCKED" else "Validazione deterministica fallita",
                now_iso(),
                int(job_id),
                int(item.get("canonical_product_id")),
            ),
        )
    execute(
        """
        UPDATE publication_jobs SET
            status=?,ready_items=?,failed_items=?,review_items=?,updated_at=?
        WHERE id=?
        """,
        (
            "READY" if ready and not failed and not review else "PARTIAL" if ready else "REVIEW_REQUIRED",
            ready,
            failed,
            review,
            now_iso(),
            int(job_id),
        ),
    )



def publication_job(job_id: int) -> dict | None:
    ensure_schema()
    return row("SELECT * FROM publication_jobs WHERE id=?", (int(job_id),))


def update_publication_job_settings(
    job_id: int,
    updates: Mapping[str, Any],
) -> dict:
    """Merge non-sensitive publication settings into a persistent job.

    Credentials are deliberately not accepted here.  The UI and state machine
    store only operational choices such as warehouse, shipping group, import
    mode and retry limits so a job can be resumed after a restart.
    """
    ensure_schema()
    current = publication_job(job_id)
    if not current:
        raise ValueError("Job di pubblicazione non trovato.")
    try:
        settings = json.loads(current.get("settings_json") or "{}")
    except (TypeError, ValueError, json.JSONDecodeError):
        settings = {}
    if not isinstance(settings, dict):
        settings = {}
    forbidden = {
        "api_key", "client_key", "secret_key", "password", "token",
        "credentials", "credentials_encrypted", "remote_write_enabled",
    }
    sensitive_fragments = ("api_key", "secret_key", "access_token", "password", "credential")
    for existing_key in list(settings):
        existing_lower = clean_text(existing_key).lower()
        if (
            existing_lower in forbidden
            or any(fragment in existing_lower for fragment in sensitive_fragments)
        ):
            settings.pop(existing_key, None)
    for key, value in dict(updates or {}).items():
        normalized = clean_text(key)
        lowered = normalized.lower()
        if (
            not normalized
            or lowered in forbidden
            or any(fragment in lowered for fragment in sensitive_fragments)
        ):
            continue
        settings[normalized] = value
    execute(
        "UPDATE publication_jobs SET settings_json=?,updated_at=? WHERE id=?",
        (json_text(settings), now_iso(), int(job_id)),
    )
    return settings


def publication_item(item_id: int) -> dict | None:
    ensure_schema()
    return row(
        """
        SELECT i.*,p.seller_id,p.supplier_id,p.source_snapshot_id,p.ean,p.supplier_sku,
               p.brand,p.model,p.title,p.description,p.normalized_json,p.raw_json,
               r.idempotency_key,r.duplicate_check_status,r.remote_product_exists,
               r.remote_offer_exists,r.planned_action,r.next_action,r.retryable,
               r.last_http_status,r.last_response_json,r.next_poll_at,r.submitted_at,
               r.completed_at AS runtime_completed_at
        FROM publication_items i
        JOIN canonical_products p ON p.id=i.canonical_product_id
        LEFT JOIN publication_item_runtime r ON r.publication_item_id=i.id
        WHERE i.id=?
        """,
        (int(item_id),),
    )


def publication_items(
    job_id: int,
    *,
    statuses: Iterable[str] = (),
    next_actions: Iterable[str] = (),
    limit: int = 10000,
) -> list[dict]:
    ensure_schema()
    query = """
        SELECT i.*,p.seller_id,p.supplier_id,p.source_snapshot_id,p.ean,p.supplier_sku,
               p.brand,p.model,p.title,p.description,p.normalized_json,p.raw_json,
               r.idempotency_key,r.duplicate_check_status,r.remote_product_exists,
               r.remote_offer_exists,r.planned_action,r.next_action,r.retryable,
               r.last_http_status,r.last_response_json,r.next_poll_at,r.submitted_at,
               r.completed_at AS runtime_completed_at
        FROM publication_items i
        JOIN canonical_products p ON p.id=i.canonical_product_id
        LEFT JOIN publication_item_runtime r ON r.publication_item_id=i.id
        WHERE i.publication_job_id=?
    """
    params: list[Any] = [int(job_id)]
    status_values = [clean_text(value).upper() for value in statuses if clean_text(value)]
    if status_values:
        query += f" AND i.status IN ({','.join('?' for _ in status_values)})"
        params.extend(status_values)
    action_values = [clean_text(value).upper() for value in next_actions if clean_text(value)]
    if action_values:
        query += f" AND r.next_action IN ({','.join('?' for _ in action_values)})"
        params.extend(action_values)
    query += " ORDER BY p.source_row_number,p.id LIMIT ?"
    params.append(max(1, int(limit)))
    return rows(query, params)


def ensure_publication_runtime(job_id: int) -> int:
    """Create or realign per-item state-machine rows idempotently.

    A feed can be regenerated inside an existing job.  When its payload hash
    changes, the old terminal state must not hide the new version.  We therefore
    rotate the idempotency key and restart only that item from the read-only
    duplicate check, while preserving the remote identifiers in publication_items.
    """
    ensure_schema()
    job = publication_job(job_id)
    if not job:
        raise ValueError("Job di pubblicazione non trovato.")
    changed = 0
    for item in publication_items(job_id, limit=100000):
        try:
            offer = json.loads(item.get("offer_payload_json") or "{}")
        except (TypeError, ValueError, json.JSONDecodeError):
            offer = {}
        seller_sku = clean_text(
            offer.get("id_offer") or offer.get("shop_sku") or item.get("supplier_sku")
        )
        ean = clean_text(item.get("ean"))
        token = json_hash(
            {
                "account_id": int(job["marketplace_account_id"]),
                "environment": clean_text(job.get("environment")) or "live",
                "storefront": clean_text(job.get("storefront")).lower(),
                "ean": ean,
                "seller_sku": seller_sku,
                "payload_hash": clean_text(item.get("payload_hash")),
                "operation": "PRODUCT_AND_OFFER",
            }
        )
        initial_status = clean_text(item.get("status")).upper()
        next_action = "CHECK_DUPLICATE" if initial_status in {
            "READY", "REVIEW_REQUIRED", "PLANNED", "RUNNING",
            "COMPLETED", "EXISTING_OFFER", "PRODUCT_REJECTED", "OFFER_REJECTED",
        } else "NONE"
        existing_key = str(item.get("idempotency_key") or "").strip()
        if existing_key == token:
            continue
        if existing_key:
            execute(
                """
                UPDATE publication_item_runtime SET
                    ean=?,seller_sku=?,idempotency_key=?,duplicate_check_status='NOT_CHECKED',
                    planned_action='CHECK_DUPLICATE',next_action=?,retryable=0,
                    last_http_status=NULL,last_response_json='{}',next_poll_at='',
                    submitted_at='',completed_at='',updated_at=?
                WHERE publication_item_id=?
                """,
                (ean, seller_sku, token, next_action, now_iso(), int(item["id"])),
            )
            if next_action != "NONE":
                execute(
                    """
                    UPDATE publication_items SET status='READY',product_status='PREPARED',
                        offer_status='',import_id='',last_error='',updated_at=? WHERE id=?
                    """,
                    (now_iso(), int(item["id"])),
                )
            changed += 1
            continue
        execute(
            """
            INSERT INTO publication_item_runtime(
                publication_item_id,seller_id,marketplace_account_id,marketplace,storefront,
                ean,seller_sku,idempotency_key,duplicate_check_status,planned_action,next_action,
                created_at,updated_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(publication_item_id) DO NOTHING
            """,
            (
                int(item["id"]),
                int(job["seller_id"]),
                int(job["marketplace_account_id"]),
                clean_text(job.get("marketplace")).lower(),
                clean_text(job.get("storefront")).lower(),
                ean,
                seller_sku,
                token,
                "NOT_CHECKED",
                "CHECK_DUPLICATE" if next_action != "NONE" else "BLOCKED",
                next_action,
                now_iso(),
                now_iso(),
            ),
        )
        changed += 1
    return changed


def update_publication_item(
    item_id: int,
    *,
    status: str | None = None,
    product_status: str | None = None,
    offer_status: str | None = None,
    external_product_id: str | None = None,
    external_offer_id: str | None = None,
    import_id: str | None = None,
    attempt_delta: int = 0,
    last_error: str | None = None,
    runtime: Mapping[str, Any] | None = None,
) -> None:
    ensure_schema()
    assignments: list[str] = []
    params: list[Any] = []
    for column, value in (
        ("status", status),
        ("product_status", product_status),
        ("offer_status", offer_status),
        ("external_product_id", external_product_id),
        ("external_offer_id", external_offer_id),
        ("import_id", import_id),
        ("last_error", last_error),
    ):
        if value is not None:
            assignments.append(f"{column}=?")
            params.append(clean_text(value) if not isinstance(value, (dict, list)) else json_text(value))
    if attempt_delta:
        assignments.append("attempt_count=attempt_count+?")
        params.append(int(attempt_delta))
    assignments.append("updated_at=?")
    params.append(now_iso())
    params.append(int(item_id))
    execute(f"UPDATE publication_items SET {','.join(assignments)} WHERE id=?", params)

    runtime_values = dict(runtime or {})
    if runtime_values:
        allowed = {
            "duplicate_check_status",
            "remote_product_exists",
            "remote_offer_exists",
            "planned_action",
            "next_action",
            "retryable",
            "last_http_status",
            "last_response_json",
            "next_poll_at",
            "submitted_at",
            "completed_at",
        }
        runtime_assignments: list[str] = []
        runtime_params: list[Any] = []
        for key, value in runtime_values.items():
            if key not in allowed:
                continue
            runtime_assignments.append(f"{key}=?")
            if key in {"remote_product_exists", "remote_offer_exists", "retryable"}:
                runtime_params.append(int(bool(value)))
            elif key == "last_http_status":
                runtime_params.append(None if value in (None, "") else int(value))
            elif key == "last_response_json":
                runtime_params.append(json_text(value) if isinstance(value, (dict, list)) else clean_text(value) or "{}")
            elif key == "next_action" and str(value or "").strip().upper() == "NONE":
                # ``clean_text`` intentionally treats the literal "none" as an
                # empty source value.  Here NONE is a real state-machine token.
                runtime_params.append("NONE")
            else:
                runtime_params.append(clean_text(value))
        if runtime_assignments:
            runtime_assignments.append("updated_at=?")
            runtime_params.append(now_iso())
            runtime_params.append(int(item_id))
            execute(
                f"UPDATE publication_item_runtime SET {','.join(runtime_assignments)} WHERE publication_item_id=?",
                runtime_params,
            )


def record_publication_event(
    *,
    job_id: int,
    item_id: int,
    product_id: int,
    event_type: str,
    status: str = "",
    message: str = "",
    details: Mapping[str, Any] | None = None,
) -> int:
    ensure_schema()
    return execute(
        """
        INSERT INTO publication_item_events(
            publication_job_id,publication_item_id,canonical_product_id,event_type,
            status,message,details_json,created_at
        ) VALUES(?,?,?,?,?,?,?,?)
        """,
        (
            int(job_id),
            int(item_id),
            int(product_id),
            clean_text(event_type).upper(),
            clean_text(status).upper(),
            clean_text(message)[:4000],
            json_text(dict(details or {})),
            now_iso(),
        ),
    )


def publication_events(job_id: int, *, item_id: int | None = None, limit: int = 1000) -> list[dict]:
    ensure_schema()
    query = "SELECT * FROM publication_item_events WHERE publication_job_id=?"
    params: list[Any] = [int(job_id)]
    if item_id is not None:
        query += " AND publication_item_id=?"
        params.append(int(item_id))
    query += " ORDER BY id DESC LIMIT ?"
    params.append(max(1, int(limit)))
    return rows(query, params)


def recalculate_publication_job(job_id: int) -> dict:
    ensure_schema()
    items = publication_items(job_id, limit=100000)
    total = len(items)
    completed_states = {"COMPLETED", "OFFER_ACTIVE", "EXISTING_OFFER"}
    failed_states = {"FAILED", "BLOCKED", "PRODUCT_REJECTED", "OFFER_REJECTED"}
    review_states = {"REVIEW_REQUIRED", "NEEDS_REVIEW", "PARTIAL"}
    processing_states = {
        "PLANNING", "PLANNED", "PRODUCT_SUBMITTED", "PRODUCT_PROCESSING",
        "PRODUCT_ACCEPTED", "OFFER_PENDING", "OFFER_SUBMITTED", "OFFER_PROCESSING",
        "RUNNING",
    }
    success = sum(1 for item in items if clean_text(item.get("status")).upper() in completed_states)
    failed = sum(1 for item in items if clean_text(item.get("status")).upper() in failed_states)
    review = sum(1 for item in items if clean_text(item.get("status")).upper() in review_states)
    ready = sum(
        1
        for item in items
        if clean_text(item.get("status")).upper()
        in {"READY", "PLANNED", "PRODUCT_ACCEPTED", "OFFER_PENDING"}
    )
    processing = sum(1 for item in items if clean_text(item.get("status")).upper() in processing_states)
    if total and success == total:
        job_status = "COMPLETED"
    elif processing:
        job_status = "RUNNING"
    elif failed and success:
        job_status = "PARTIAL"
    elif failed and failed == total:
        job_status = "FAILED"
    elif review:
        job_status = "REVIEW_REQUIRED"
    elif ready:
        job_status = "READY"
    else:
        job_status = "CREATED"
    execute(
        """
        UPDATE publication_jobs SET status=?,total_items=?,ready_items=?,success_items=?,
            failed_items=?,review_items=?,updated_at=? WHERE id=?
        """,
        (job_status, total, ready, success, failed, review, now_iso(), int(job_id)),
    )
    return {
        "job_id": int(job_id),
        "status": job_status,
        "total": total,
        "ready": ready,
        "success": success,
        "failed": failed,
        "review": review,
        "processing": processing,
    }


def product_channel_state(
    *,
    account_id: int,
    environment: str,
    storefront: str,
    ean: str,
    seller_sku: str,
) -> dict | None:
    ensure_schema()
    return row(
        """
        SELECT * FROM product_channel_states
        WHERE marketplace_account_id=? AND environment=? AND storefront=? AND ean=? AND seller_sku=?
        """,
        (
            int(account_id),
            clean_text(environment) or "live",
            clean_text(storefront).lower(),
            clean_text(ean),
            clean_text(seller_sku),
        ),
    )


def upsert_product_channel_state(
    *,
    seller_id: int,
    account_id: int,
    marketplace: str,
    environment: str,
    storefront: str,
    locale: str,
    canonical_product_id: int | None,
    ean: str,
    seller_sku: str,
    external_product_id: str = "",
    external_offer_id: str = "",
    product_status: str = "",
    offer_status: str = "",
    product_payload_hash: str = "",
    offer_payload_hash: str = "",
    last_import_id: str = "",
    last_error: str = "",
) -> int:
    ensure_schema()
    existing = product_channel_state(
        account_id=account_id,
        environment=environment,
        storefront=storefront,
        ean=ean,
        seller_sku=seller_sku,
    )
    values = (
        int(seller_id), int(account_id), clean_text(marketplace).lower(),
        clean_text(environment) or "live", clean_text(storefront).lower(), clean_text(locale),
        canonical_product_id, clean_text(ean), clean_text(seller_sku), clean_text(external_product_id),
        clean_text(external_offer_id), clean_text(product_status).upper(), clean_text(offer_status).upper(),
        clean_text(product_payload_hash), clean_text(offer_payload_hash), clean_text(last_import_id),
        clean_text(last_error)[:4000], now_iso(), now_iso(), now_iso(),
    )
    execute(
        """
        INSERT INTO product_channel_states(
            seller_id,marketplace_account_id,marketplace,environment,storefront,locale,
            canonical_product_id,ean,seller_sku,external_product_id,external_offer_id,
            product_status,offer_status,product_payload_hash,offer_payload_hash,last_import_id,
            last_error,last_checked_at,created_at,updated_at
        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(marketplace_account_id,environment,storefront,ean,seller_sku) DO UPDATE SET
            seller_id=excluded.seller_id,marketplace=excluded.marketplace,locale=excluded.locale,
            canonical_product_id=excluded.canonical_product_id,
            external_product_id=CASE WHEN excluded.external_product_id<>'' THEN excluded.external_product_id ELSE product_channel_states.external_product_id END,
            external_offer_id=CASE WHEN excluded.external_offer_id<>'' THEN excluded.external_offer_id ELSE product_channel_states.external_offer_id END,
            product_status=CASE WHEN excluded.product_status<>'' THEN excluded.product_status ELSE product_channel_states.product_status END,
            offer_status=CASE WHEN excluded.offer_status<>'' THEN excluded.offer_status ELSE product_channel_states.offer_status END,
            product_payload_hash=CASE WHEN excluded.product_payload_hash<>'' THEN excluded.product_payload_hash ELSE product_channel_states.product_payload_hash END,
            offer_payload_hash=CASE WHEN excluded.offer_payload_hash<>'' THEN excluded.offer_payload_hash ELSE product_channel_states.offer_payload_hash END,
            last_import_id=CASE WHEN excluded.last_import_id<>'' THEN excluded.last_import_id ELSE product_channel_states.last_import_id END,
            last_error=excluded.last_error,last_checked_at=excluded.last_checked_at,updated_at=excluded.updated_at
        """,
        values,
    )
    found = product_channel_state(
        account_id=account_id,
        environment=environment,
        storefront=storefront,
        ean=ean,
        seller_sku=seller_sku,
    )
    return int(found["id"]) if found else int(existing["id"]) if existing else 0


def create_marketplace_import(
    *,
    job_id: int,
    account_id: int,
    import_type: str,
    external_import_id: str,
    status: str,
    request_data: Mapping[str, Any] | None = None,
    response_data: Mapping[str, Any] | None = None,
    error: str = "",
) -> int:
    ensure_schema()
    external = clean_text(external_import_id) or f"LOCAL-{json_hash({'job': job_id, 'type': import_type, 'request': dict(request_data or {})})[:24]}"
    execute(
        """
        INSERT INTO marketplace_imports(
            publication_job_id,marketplace_account_id,import_type,external_import_id,status,
            request_json,response_json,error,created_at,updated_at
        ) VALUES(?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(marketplace_account_id,import_type,external_import_id) DO UPDATE SET
            status=excluded.status,response_json=excluded.response_json,error=excluded.error,
            updated_at=excluded.updated_at
        """,
        (
            int(job_id), int(account_id), clean_text(import_type).upper(), external,
            clean_text(status).upper(), json_text(dict(request_data or {})),
            json_text(dict(response_data or {})), clean_text(error)[:4000], now_iso(), now_iso(),
        ),
    )
    found = row(
        """
        SELECT id FROM marketplace_imports
        WHERE marketplace_account_id=? AND import_type=? AND external_import_id=?
        """,
        (int(account_id), clean_text(import_type).upper(), external),
    )
    return int(found["id"]) if found else 0


def update_marketplace_import(
    import_row_id: int,
    *,
    status: str | None = None,
    response_data: Mapping[str, Any] | None = None,
    report_paths: Mapping[str, Any] | None = None,
    has_error_report: bool | None = None,
    has_success_report: bool | None = None,
    error: str | None = None,
) -> None:
    ensure_schema()
    assignments: list[str] = []
    params: list[Any] = []
    for column, value in (("status", status), ("error", error)):
        if value is not None:
            assignments.append(f"{column}=?")
            params.append(clean_text(value).upper() if column == "status" else clean_text(value)[:4000])
    if response_data is not None:
        assignments.append("response_json=?")
        params.append(json_text(dict(response_data)))
    if report_paths is not None:
        assignments.append("report_paths_json=?")
        params.append(json_text(dict(report_paths)))
    if has_error_report is not None:
        assignments.append("has_error_report=?")
        params.append(int(bool(has_error_report)))
    if has_success_report is not None:
        assignments.append("has_success_report=?")
        params.append(int(bool(has_success_report)))
    assignments.append("updated_at=?")
    params.append(now_iso())
    params.append(int(import_row_id))
    execute(f"UPDATE marketplace_imports SET {','.join(assignments)} WHERE id=?", params)


def marketplace_import(import_row_id: int) -> dict | None:
    ensure_schema()
    return row("SELECT * FROM marketplace_imports WHERE id=?", (int(import_row_id),))


def marketplace_imports_for_job(job_id: int, *, import_type: str = "", limit: int = 1000) -> list[dict]:
    ensure_schema()
    query = "SELECT * FROM marketplace_imports WHERE publication_job_id=?"
    params: list[Any] = [int(job_id)]
    if clean_text(import_type):
        query += " AND import_type=?"
        params.append(clean_text(import_type).upper())
    query += " ORDER BY id DESC LIMIT ?"
    params.append(max(1, int(limit)))
    return rows(query, params)


def save_publication_artifact(
    *,
    job_id: int,
    artifact_type: str,
    filename: str,
    local_path: str,
    content_hash: str,
    row_count: int,
    metadata: Mapping[str, Any] | None = None,
    marketplace_import_id: int | None = None,
) -> int:
    ensure_schema()
    execute(
        """
        INSERT INTO publication_artifacts(
            publication_job_id,marketplace_import_id,artifact_type,filename,local_path,
            content_hash,row_count,metadata_json,created_at
        ) VALUES(?,?,?,?,?,?,?,?,?)
        ON CONFLICT(publication_job_id,artifact_type,content_hash) DO UPDATE SET
            marketplace_import_id=excluded.marketplace_import_id,filename=excluded.filename,
            local_path=excluded.local_path,row_count=excluded.row_count,
            metadata_json=excluded.metadata_json
        """,
        (
            int(job_id), marketplace_import_id, clean_text(artifact_type).upper(),
            clean_text(filename), clean_text(local_path), clean_text(content_hash),
            max(0, int(row_count)), json_text(dict(metadata or {})), now_iso(),
        ),
    )
    found = row(
        """
        SELECT id FROM publication_artifacts
        WHERE publication_job_id=? AND artifact_type=? AND content_hash=?
        """,
        (int(job_id), clean_text(artifact_type).upper(), clean_text(content_hash)),
    )
    return int(found["id"]) if found else 0


def publication_artifacts(job_id: int, *, limit: int = 1000) -> list[dict]:
    ensure_schema()
    return rows(
        """
        SELECT * FROM publication_artifacts
        WHERE publication_job_id=? ORDER BY id DESC LIMIT ?
        """,
        (int(job_id), max(1, int(limit))),
    )


__all__ = [
    "upsert_product_channel_state",
    "update_publication_item",
    "update_publication_job_settings",
    "update_marketplace_import",
    "save_publication_artifact",
    "record_publication_event",
    "recalculate_publication_job",
    "publication_job",
    "publication_items",
    "publication_item",
    "publication_events",
    "publication_artifacts",
    "product_channel_state",
    "marketplace_imports_for_job",
    "marketplace_import",
    "ensure_publication_runtime",
    "create_marketplace_import",
    "apply_feed_preparations_to_job",
    "canonical_product",
    "category_assignments_for_source",
    "category_candidates_for_run",
    "category_classification_runs",
    "complete_category_classification",
    "canonical_products_for_seller",
    "canonical_products_for_source",
    "capabilities_for_account",
    "complete_taxonomy_sync",
    "create_publication_job",
    "feed_preparation",
    "feed_preparations_for_source",
    "find_category_mapping_rule",
    "create_source_snapshot",
    "latest_taxonomy_snapshot",
    "publication_jobs",
    "source_snapshot",
    "source_snapshots",
    "product_evidence",
    "record_validation_run",
    "save_category_candidates",
    "save_feed_preparation",
    "save_taxonomy_category_enrichment",
    "save_canonical_products",
    "save_capabilities",
    "save_taxonomy_bundle",
    "start_category_classification",
    "start_taxonomy_sync",
    "taxonomy_category_enrichment",
    "taxonomy_attribute_values",
    "taxonomy_attributes",
    "taxonomy_category",
    "taxonomy_categories",
    "taxonomy_sync_runs",
    "upsert_category_assignment",
    "upsert_category_mapping_rule",
]
