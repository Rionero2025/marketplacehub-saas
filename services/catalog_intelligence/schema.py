from __future__ import annotations

import threading

from services import db as db_service


CATALOG_SCHEMA_REVISION = 313
_SCHEMA_LOCK = threading.RLock()
_SCHEMA_READY: set[tuple] = set()


DDL = r"""
CREATE TABLE IF NOT EXISTS marketplace_capabilities (
    seller_id INTEGER NOT NULL REFERENCES sellers(id) ON DELETE CASCADE,
    marketplace_account_id INTEGER NOT NULL REFERENCES marketplace_accounts(id) ON DELETE CASCADE,
    marketplace TEXT NOT NULL,
    environment TEXT NOT NULL DEFAULT 'live',
    capability_key TEXT NOT NULL,
    supported INTEGER NOT NULL DEFAULT 0,
    status_code INTEGER,
    message TEXT NOT NULL DEFAULT '',
    details_json TEXT NOT NULL DEFAULT '{}',
    checked_at TEXT NOT NULL,
    PRIMARY KEY(marketplace_account_id,environment,capability_key)
);
CREATE INDEX IF NOT EXISTS idx_marketplace_capabilities_scope
ON marketplace_capabilities(seller_id,marketplace_account_id,marketplace,environment,supported);

CREATE TABLE IF NOT EXISTS taxonomy_sync_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    seller_id INTEGER NOT NULL REFERENCES sellers(id) ON DELETE CASCADE,
    marketplace_account_id INTEGER NOT NULL REFERENCES marketplace_accounts(id) ON DELETE CASCADE,
    marketplace TEXT NOT NULL,
    environment TEXT NOT NULL DEFAULT 'live',
    scope_key TEXT NOT NULL DEFAULT '',
    storefront TEXT NOT NULL DEFAULT '',
    locale TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL,
    category_count INTEGER NOT NULL DEFAULT 0,
    attribute_count INTEGER NOT NULL DEFAULT 0,
    value_count INTEGER NOT NULL DEFAULT 0,
    details_json TEXT NOT NULL DEFAULT '{}',
    error TEXT NOT NULL DEFAULT '',
    snapshot_id INTEGER,
    started_at TEXT NOT NULL,
    completed_at TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_taxonomy_sync_runs_scope
ON taxonomy_sync_runs(marketplace_account_id,environment,storefront,locale,started_at);

CREATE TABLE IF NOT EXISTS taxonomy_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    seller_id INTEGER NOT NULL REFERENCES sellers(id) ON DELETE CASCADE,
    marketplace_account_id INTEGER NOT NULL REFERENCES marketplace_accounts(id) ON DELETE CASCADE,
    marketplace TEXT NOT NULL,
    environment TEXT NOT NULL DEFAULT 'live',
    scope_key TEXT NOT NULL DEFAULT '',
    storefront TEXT NOT NULL DEFAULT '',
    locale TEXT NOT NULL DEFAULT '',
    content_hash TEXT NOT NULL,
    raw_json TEXT NOT NULL DEFAULT '{}',
    category_count INTEGER NOT NULL DEFAULT 0,
    attribute_count INTEGER NOT NULL DEFAULT 0,
    value_count INTEGER NOT NULL DEFAULT 0,
    active INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    UNIQUE(marketplace_account_id,environment,scope_key,content_hash)
);
CREATE INDEX IF NOT EXISTS idx_taxonomy_snapshots_active
ON taxonomy_snapshots(marketplace_account_id,environment,scope_key,active,created_at);

CREATE TABLE IF NOT EXISTS taxonomy_categories (
    snapshot_id INTEGER NOT NULL REFERENCES taxonomy_snapshots(id) ON DELETE CASCADE,
    external_id TEXT NOT NULL,
    parent_external_id TEXT NOT NULL DEFAULT '',
    code TEXT NOT NULL DEFAULT '',
    label TEXT NOT NULL DEFAULT '',
    path TEXT NOT NULL DEFAULT '',
    level INTEGER NOT NULL DEFAULT 0,
    is_leaf INTEGER NOT NULL DEFAULT 0,
    product_type TEXT NOT NULL DEFAULT '',
    required_attributes_json TEXT NOT NULL DEFAULT '[]',
    raw_json TEXT NOT NULL DEFAULT '{}',
    PRIMARY KEY(snapshot_id,external_id)
);
CREATE INDEX IF NOT EXISTS idx_taxonomy_categories_lookup
ON taxonomy_categories(snapshot_id,parent_external_id,is_leaf,label);

CREATE TABLE IF NOT EXISTS taxonomy_attributes (
    snapshot_id INTEGER NOT NULL REFERENCES taxonomy_snapshots(id) ON DELETE CASCADE,
    category_external_id TEXT NOT NULL DEFAULT '',
    external_id TEXT NOT NULL,
    code TEXT NOT NULL DEFAULT '',
    label TEXT NOT NULL DEFAULT '',
    data_type TEXT NOT NULL DEFAULT 'TEXT',
    requirement_level TEXT NOT NULL DEFAULT 'OPTIONAL',
    required INTEGER NOT NULL DEFAULT 0,
    multiple INTEGER NOT NULL DEFAULT 0,
    variant INTEGER NOT NULL DEFAULT 0,
    unit TEXT NOT NULL DEFAULT '',
    locale TEXT NOT NULL DEFAULT '',
    value_list_code TEXT NOT NULL DEFAULT '',
    constraints_json TEXT NOT NULL DEFAULT '{}',
    raw_json TEXT NOT NULL DEFAULT '{}',
    PRIMARY KEY(snapshot_id,category_external_id,external_id)
);
CREATE INDEX IF NOT EXISTS idx_taxonomy_attributes_lookup
ON taxonomy_attributes(snapshot_id,category_external_id,required,label);

CREATE TABLE IF NOT EXISTS taxonomy_attribute_values (
    snapshot_id INTEGER NOT NULL REFERENCES taxonomy_snapshots(id) ON DELETE CASCADE,
    category_external_id TEXT NOT NULL DEFAULT '',
    attribute_external_id TEXT NOT NULL,
    value_code TEXT NOT NULL,
    label TEXT NOT NULL DEFAULT '',
    position INTEGER NOT NULL DEFAULT 0,
    raw_json TEXT NOT NULL DEFAULT '{}',
    PRIMARY KEY(snapshot_id,category_external_id,attribute_external_id,value_code)
);
CREATE INDEX IF NOT EXISTS idx_taxonomy_attribute_values_lookup
ON taxonomy_attribute_values(snapshot_id,attribute_external_id,category_external_id,label);

CREATE TABLE IF NOT EXISTS taxonomy_attribute_conditions (
    snapshot_id INTEGER NOT NULL REFERENCES taxonomy_snapshots(id) ON DELETE CASCADE,
    category_external_id TEXT NOT NULL,
    attribute_external_id TEXT NOT NULL,
    condition_hash TEXT NOT NULL,
    condition_json TEXT NOT NULL,
    PRIMARY KEY(snapshot_id,category_external_id,attribute_external_id,condition_hash)
);

CREATE TABLE IF NOT EXISTS taxonomy_locales (
    snapshot_id INTEGER NOT NULL REFERENCES taxonomy_snapshots(id) ON DELETE CASCADE,
    code TEXT NOT NULL,
    storefront TEXT NOT NULL DEFAULT '',
    label TEXT NOT NULL DEFAULT '',
    raw_json TEXT NOT NULL DEFAULT '{}',
    PRIMARY KEY(snapshot_id,code,storefront)
);

CREATE TABLE IF NOT EXISTS source_catalog_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    seller_id INTEGER NOT NULL REFERENCES sellers(id) ON DELETE CASCADE,
    supplier_id INTEGER NOT NULL REFERENCES suppliers(id) ON DELETE CASCADE,
    price_list_id INTEGER NOT NULL REFERENCES price_lists(id) ON DELETE CASCADE,
    saved_view_id INTEGER REFERENCES saved_views(id) ON DELETE SET NULL,
    source_path TEXT NOT NULL DEFAULT '',
    content_hash TEXT NOT NULL,
    row_count INTEGER NOT NULL DEFAULT 0,
    columns_json TEXT NOT NULL DEFAULT '[]',
    metadata_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    UNIQUE(seller_id,price_list_id,saved_view_id,content_hash)
);
CREATE INDEX IF NOT EXISTS idx_source_catalog_snapshots_scope
ON source_catalog_snapshots(seller_id,supplier_id,price_list_id,created_at);

CREATE TABLE IF NOT EXISTS canonical_products (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    seller_id INTEGER NOT NULL REFERENCES sellers(id) ON DELETE CASCADE,
    supplier_id INTEGER NOT NULL REFERENCES suppliers(id) ON DELETE CASCADE,
    price_list_id INTEGER NOT NULL REFERENCES price_lists(id) ON DELETE CASCADE,
    source_snapshot_id INTEGER NOT NULL REFERENCES source_catalog_snapshots(id) ON DELETE CASCADE,
    source_row_key TEXT NOT NULL,
    source_row_number INTEGER NOT NULL DEFAULT 0,
    ean TEXT NOT NULL DEFAULT '',
    supplier_sku TEXT NOT NULL DEFAULT '',
    brand TEXT NOT NULL DEFAULT '',
    model TEXT NOT NULL DEFAULT '',
    title TEXT NOT NULL DEFAULT '',
    description TEXT NOT NULL DEFAULT '',
    normalized_json TEXT NOT NULL DEFAULT '{}',
    raw_json TEXT NOT NULL DEFAULT '{}',
    content_hash TEXT NOT NULL,
    completeness_score REAL NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'NORMALIZED',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(source_snapshot_id,source_row_key)
);
CREATE INDEX IF NOT EXISTS idx_canonical_products_scope
ON canonical_products(seller_id,supplier_id,price_list_id,status,ean,supplier_sku);

CREATE TABLE IF NOT EXISTS product_evidence (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    canonical_product_id INTEGER NOT NULL REFERENCES canonical_products(id) ON DELETE CASCADE,
    canonical_field TEXT NOT NULL,
    source_field TEXT NOT NULL DEFAULT '',
    source_path TEXT NOT NULL DEFAULT '',
    source_value_json TEXT NOT NULL DEFAULT 'null',
    source_file TEXT NOT NULL DEFAULT '',
    source_row INTEGER NOT NULL DEFAULT 0,
    source_hash TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_product_evidence_product
ON product_evidence(canonical_product_id,canonical_field,source_field);

CREATE TABLE IF NOT EXISTS canonical_product_values (
    canonical_product_id INTEGER NOT NULL REFERENCES canonical_products(id) ON DELETE CASCADE,
    field_name TEXT NOT NULL,
    value_json TEXT NOT NULL DEFAULT 'null',
    data_type TEXT NOT NULL DEFAULT 'TEXT',
    unit TEXT NOT NULL DEFAULT '',
    source_kind TEXT NOT NULL DEFAULT 'SOURCE',
    evidence_id INTEGER REFERENCES product_evidence(id) ON DELETE SET NULL,
    confidence REAL NOT NULL DEFAULT 1,
    updated_at TEXT NOT NULL,
    PRIMARY KEY(canonical_product_id,field_name)
);

CREATE TABLE IF NOT EXISTS mapping_rules (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    seller_id INTEGER NOT NULL REFERENCES sellers(id) ON DELETE CASCADE,
    supplier_id INTEGER REFERENCES suppliers(id) ON DELETE CASCADE,
    marketplace TEXT NOT NULL,
    category_external_id TEXT NOT NULL DEFAULT '',
    source_field TEXT NOT NULL,
    canonical_field TEXT NOT NULL,
    target_attribute TEXT NOT NULL DEFAULT '',
    transform_key TEXT NOT NULL DEFAULT 'identity',
    transform_config_json TEXT NOT NULL DEFAULT '{}',
    status TEXT NOT NULL DEFAULT 'APPROVED',
    confidence REAL NOT NULL DEFAULT 1,
    use_count INTEGER NOT NULL DEFAULT 0,
    success_count INTEGER NOT NULL DEFAULT 0,
    failure_count INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(seller_id,supplier_id,marketplace,category_external_id,source_field,canonical_field,target_attribute)
);
CREATE INDEX IF NOT EXISTS idx_mapping_rules_lookup
ON mapping_rules(seller_id,supplier_id,marketplace,category_external_id,status);

CREATE TABLE IF NOT EXISTS mapping_decisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    publication_job_id INTEGER,
    canonical_product_id INTEGER NOT NULL REFERENCES canonical_products(id) ON DELETE CASCADE,
    taxonomy_snapshot_id INTEGER REFERENCES taxonomy_snapshots(id) ON DELETE SET NULL,
    mapping_rule_id INTEGER REFERENCES mapping_rules(id) ON DELETE SET NULL,
    target_category_external_id TEXT NOT NULL DEFAULT '',
    target_attribute_external_id TEXT NOT NULL DEFAULT '',
    value_json TEXT NOT NULL DEFAULT 'null',
    evidence_ids_json TEXT NOT NULL DEFAULT '[]',
    decision_source TEXT NOT NULL DEFAULT 'DETERMINISTIC',
    confidence REAL NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'PROPOSED',
    details_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_mapping_decisions_product
ON mapping_decisions(canonical_product_id,status,target_category_external_id,target_attribute_external_id);

CREATE TABLE IF NOT EXISTS validation_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    seller_id INTEGER NOT NULL REFERENCES sellers(id) ON DELETE CASCADE,
    marketplace_account_id INTEGER NOT NULL REFERENCES marketplace_accounts(id) ON DELETE CASCADE,
    taxonomy_snapshot_id INTEGER REFERENCES taxonomy_snapshots(id) ON DELETE SET NULL,
    publication_job_id INTEGER,
    status TEXT NOT NULL,
    product_count INTEGER NOT NULL DEFAULT 0,
    valid_count INTEGER NOT NULL DEFAULT 0,
    warning_count INTEGER NOT NULL DEFAULT 0,
    invalid_count INTEGER NOT NULL DEFAULT 0,
    started_at TEXT NOT NULL,
    completed_at TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS validation_issues (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    validation_run_id INTEGER NOT NULL REFERENCES validation_runs(id) ON DELETE CASCADE,
    canonical_product_id INTEGER NOT NULL REFERENCES canonical_products(id) ON DELETE CASCADE,
    severity TEXT NOT NULL,
    code TEXT NOT NULL,
    field_name TEXT NOT NULL DEFAULT '',
    message TEXT NOT NULL,
    details_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_validation_issues_scope
ON validation_issues(validation_run_id,canonical_product_id,severity,code);

CREATE TABLE IF NOT EXISTS publication_jobs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    seller_id INTEGER NOT NULL REFERENCES sellers(id) ON DELETE CASCADE,
    marketplace_account_id INTEGER NOT NULL REFERENCES marketplace_accounts(id) ON DELETE CASCADE,
    marketplace TEXT NOT NULL,
    environment TEXT NOT NULL DEFAULT 'live',
    storefront TEXT NOT NULL DEFAULT '',
    locale TEXT NOT NULL DEFAULT '',
    price_list_id INTEGER REFERENCES price_lists(id) ON DELETE SET NULL,
    source_snapshot_id INTEGER REFERENCES source_catalog_snapshots(id) ON DELETE SET NULL,
    taxonomy_snapshot_id INTEGER REFERENCES taxonomy_snapshots(id) ON DELETE SET NULL,
    job_type TEXT NOT NULL DEFAULT 'PRODUCT_CREATION',
    status TEXT NOT NULL DEFAULT 'CREATED',
    total_items INTEGER NOT NULL DEFAULT 0,
    ready_items INTEGER NOT NULL DEFAULT 0,
    success_items INTEGER NOT NULL DEFAULT 0,
    failed_items INTEGER NOT NULL DEFAULT 0,
    review_items INTEGER NOT NULL DEFAULT 0,
    settings_json TEXT NOT NULL DEFAULT '{}',
    idempotency_key TEXT NOT NULL DEFAULT '',
    error TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(marketplace_account_id,idempotency_key)
);
CREATE INDEX IF NOT EXISTS idx_publication_jobs_scope
ON publication_jobs(seller_id,marketplace_account_id,status,created_at);

CREATE TABLE IF NOT EXISTS publication_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    publication_job_id INTEGER NOT NULL REFERENCES publication_jobs(id) ON DELETE CASCADE,
    canonical_product_id INTEGER NOT NULL REFERENCES canonical_products(id) ON DELETE CASCADE,
    status TEXT NOT NULL DEFAULT 'CREATED',
    product_status TEXT NOT NULL DEFAULT '',
    offer_status TEXT NOT NULL DEFAULT '',
    external_product_id TEXT NOT NULL DEFAULT '',
    external_offer_id TEXT NOT NULL DEFAULT '',
    import_id TEXT NOT NULL DEFAULT '',
    payload_hash TEXT NOT NULL DEFAULT '',
    product_payload_json TEXT NOT NULL DEFAULT '{}',
    offer_payload_json TEXT NOT NULL DEFAULT '{}',
    attempt_count INTEGER NOT NULL DEFAULT 0,
    last_error TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(publication_job_id,canonical_product_id)
);
CREATE INDEX IF NOT EXISTS idx_publication_items_status
ON publication_items(publication_job_id,status,product_status,offer_status);

CREATE TABLE IF NOT EXISTS marketplace_imports (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    publication_job_id INTEGER NOT NULL REFERENCES publication_jobs(id) ON DELETE CASCADE,
    marketplace_account_id INTEGER NOT NULL REFERENCES marketplace_accounts(id) ON DELETE CASCADE,
    import_type TEXT NOT NULL,
    external_import_id TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'CREATED',
    request_json TEXT NOT NULL DEFAULT '{}',
    response_json TEXT NOT NULL DEFAULT '{}',
    report_paths_json TEXT NOT NULL DEFAULT '{}',
    has_error_report INTEGER NOT NULL DEFAULT 0,
    has_success_report INTEGER NOT NULL DEFAULT 0,
    error TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(marketplace_account_id,import_type,external_import_id)
);

CREATE TABLE IF NOT EXISTS catalog_audit_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    seller_id INTEGER NOT NULL REFERENCES sellers(id) ON DELETE CASCADE,
    marketplace_account_id INTEGER REFERENCES marketplace_accounts(id) ON DELETE SET NULL,
    publication_job_id INTEGER REFERENCES publication_jobs(id) ON DELETE SET NULL,
    canonical_product_id INTEGER REFERENCES canonical_products(id) ON DELETE SET NULL,
    action TEXT NOT NULL,
    actor TEXT NOT NULL DEFAULT 'system',
    before_json TEXT NOT NULL DEFAULT '{}',
    after_json TEXT NOT NULL DEFAULT '{}',
    details_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_catalog_audit_log_scope
ON catalog_audit_log(seller_id,marketplace_account_id,publication_job_id,canonical_product_id,created_at);


CREATE TABLE IF NOT EXISTS taxonomy_category_enrichments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    seller_id INTEGER NOT NULL REFERENCES sellers(id) ON DELETE CASCADE,
    marketplace_account_id INTEGER NOT NULL REFERENCES marketplace_accounts(id) ON DELETE CASCADE,
    taxonomy_snapshot_id INTEGER NOT NULL REFERENCES taxonomy_snapshots(id) ON DELETE CASCADE,
    marketplace TEXT NOT NULL,
    environment TEXT NOT NULL DEFAULT 'live',
    scope_key TEXT NOT NULL DEFAULT '',
    category_external_id TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'COMPLETED',
    category_json TEXT NOT NULL DEFAULT '{}',
    attributes_json TEXT NOT NULL DEFAULT '[]',
    content_hash TEXT NOT NULL DEFAULT '',
    attribute_count INTEGER NOT NULL DEFAULT 0,
    value_count INTEGER NOT NULL DEFAULT 0,
    error TEXT NOT NULL DEFAULT '',
    fetched_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(marketplace_account_id,environment,scope_key,taxonomy_snapshot_id,category_external_id)
);
CREATE INDEX IF NOT EXISTS idx_taxonomy_category_enrichments_scope
ON taxonomy_category_enrichments(
    marketplace_account_id,environment,scope_key,taxonomy_snapshot_id,category_external_id,status
);

CREATE TABLE IF NOT EXISTS category_classification_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    seller_id INTEGER NOT NULL REFERENCES sellers(id) ON DELETE CASCADE,
    marketplace_account_id INTEGER NOT NULL REFERENCES marketplace_accounts(id) ON DELETE CASCADE,
    taxonomy_snapshot_id INTEGER NOT NULL REFERENCES taxonomy_snapshots(id) ON DELETE CASCADE,
    source_snapshot_id INTEGER NOT NULL REFERENCES source_catalog_snapshots(id) ON DELETE CASCADE,
    marketplace TEXT NOT NULL,
    environment TEXT NOT NULL DEFAULT 'live',
    storefront TEXT NOT NULL DEFAULT '',
    locale TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'RUNNING',
    product_count INTEGER NOT NULL DEFAULT 0,
    classified_count INTEGER NOT NULL DEFAULT 0,
    review_count INTEGER NOT NULL DEFAULT 0,
    blocked_count INTEGER NOT NULL DEFAULT 0,
    settings_json TEXT NOT NULL DEFAULT '{}',
    error TEXT NOT NULL DEFAULT '',
    started_at TEXT NOT NULL,
    completed_at TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_category_classification_runs_scope
ON category_classification_runs(
    seller_id,marketplace_account_id,source_snapshot_id,taxonomy_snapshot_id,status,started_at
);

CREATE TABLE IF NOT EXISTS product_category_candidates (
    classification_run_id INTEGER NOT NULL REFERENCES category_classification_runs(id) ON DELETE CASCADE,
    canonical_product_id INTEGER NOT NULL REFERENCES canonical_products(id) ON DELETE CASCADE,
    rank INTEGER NOT NULL,
    category_external_id TEXT NOT NULL,
    category_label TEXT NOT NULL DEFAULT '',
    category_path TEXT NOT NULL DEFAULT '',
    score REAL NOT NULL DEFAULT 0,
    candidate_source TEXT NOT NULL DEFAULT 'LOCAL_TAXONOMY',
    signals_json TEXT NOT NULL DEFAULT '{}',
    raw_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    PRIMARY KEY(classification_run_id,canonical_product_id,rank)
);
CREATE INDEX IF NOT EXISTS idx_product_category_candidates_lookup
ON product_category_candidates(
    canonical_product_id,classification_run_id,category_external_id,score
);

CREATE TABLE IF NOT EXISTS product_category_assignments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    seller_id INTEGER NOT NULL REFERENCES sellers(id) ON DELETE CASCADE,
    marketplace_account_id INTEGER NOT NULL REFERENCES marketplace_accounts(id) ON DELETE CASCADE,
    taxonomy_snapshot_id INTEGER NOT NULL REFERENCES taxonomy_snapshots(id) ON DELETE CASCADE,
    source_snapshot_id INTEGER NOT NULL REFERENCES source_catalog_snapshots(id) ON DELETE CASCADE,
    classification_run_id INTEGER REFERENCES category_classification_runs(id) ON DELETE SET NULL,
    canonical_product_id INTEGER NOT NULL REFERENCES canonical_products(id) ON DELETE CASCADE,
    marketplace TEXT NOT NULL,
    storefront TEXT NOT NULL DEFAULT '',
    category_external_id TEXT NOT NULL,
    category_label TEXT NOT NULL DEFAULT '',
    category_path TEXT NOT NULL DEFAULT '',
    decision_source TEXT NOT NULL DEFAULT 'LOCAL_TAXONOMY',
    confidence REAL NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'REVIEW',
    classification_signature TEXT NOT NULL DEFAULT '',
    evidence_json TEXT NOT NULL DEFAULT '{}',
    approved_by TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(marketplace_account_id,source_snapshot_id,canonical_product_id)
);
CREATE INDEX IF NOT EXISTS idx_product_category_assignments_scope
ON product_category_assignments(
    seller_id,marketplace_account_id,source_snapshot_id,status,category_external_id,confidence
);

CREATE TABLE IF NOT EXISTS category_mapping_rules (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    seller_id INTEGER NOT NULL REFERENCES sellers(id) ON DELETE CASCADE,
    supplier_id INTEGER REFERENCES suppliers(id) ON DELETE CASCADE,
    marketplace TEXT NOT NULL,
    storefront TEXT NOT NULL DEFAULT '',
    source_signature TEXT NOT NULL,
    source_label TEXT NOT NULL DEFAULT '',
    category_external_id TEXT NOT NULL,
    category_label TEXT NOT NULL DEFAULT '',
    confidence REAL NOT NULL DEFAULT 1,
    status TEXT NOT NULL DEFAULT 'APPROVED',
    use_count INTEGER NOT NULL DEFAULT 0,
    success_count INTEGER NOT NULL DEFAULT 0,
    failure_count INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(seller_id,supplier_id,marketplace,storefront,source_signature)
);
CREATE INDEX IF NOT EXISTS idx_category_mapping_rules_lookup
ON category_mapping_rules(
    seller_id,supplier_id,marketplace,storefront,status,source_signature
);

CREATE TABLE IF NOT EXISTS product_feed_preparations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    seller_id INTEGER NOT NULL REFERENCES sellers(id) ON DELETE CASCADE,
    marketplace_account_id INTEGER NOT NULL REFERENCES marketplace_accounts(id) ON DELETE CASCADE,
    taxonomy_snapshot_id INTEGER NOT NULL REFERENCES taxonomy_snapshots(id) ON DELETE CASCADE,
    source_snapshot_id INTEGER NOT NULL REFERENCES source_catalog_snapshots(id) ON DELETE CASCADE,
    canonical_product_id INTEGER NOT NULL REFERENCES canonical_products(id) ON DELETE CASCADE,
    category_external_id TEXT NOT NULL,
    marketplace TEXT NOT NULL,
    storefront TEXT NOT NULL DEFAULT '',
    locale TEXT NOT NULL DEFAULT '',
    product_payload_json TEXT NOT NULL DEFAULT '{}',
    offer_payload_json TEXT NOT NULL DEFAULT '{}',
    mapped_attributes_json TEXT NOT NULL DEFAULT '{}',
    missing_fields_json TEXT NOT NULL DEFAULT '[]',
    issues_json TEXT NOT NULL DEFAULT '[]',
    validation_status TEXT NOT NULL DEFAULT 'BLOCKED',
    readiness_score REAL NOT NULL DEFAULT 0,
    payload_hash TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(marketplace_account_id,source_snapshot_id,canonical_product_id,category_external_id)
);
CREATE INDEX IF NOT EXISTS idx_product_feed_preparations_scope
ON product_feed_preparations(
    seller_id,marketplace_account_id,source_snapshot_id,validation_status,category_external_id
);

CREATE TABLE IF NOT EXISTS catalog_ai_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    seller_id INTEGER NOT NULL REFERENCES sellers(id) ON DELETE CASCADE,
    marketplace_account_id INTEGER NOT NULL REFERENCES marketplace_accounts(id) ON DELETE CASCADE,
    taxonomy_snapshot_id INTEGER NOT NULL REFERENCES taxonomy_snapshots(id) ON DELETE CASCADE,
    source_snapshot_id INTEGER NOT NULL REFERENCES source_catalog_snapshots(id) ON DELETE CASCADE,
    ai_profile_id INTEGER NOT NULL,
    purpose TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'RUNNING',
    product_count INTEGER NOT NULL DEFAULT 0,
    success_count INTEGER NOT NULL DEFAULT 0,
    review_count INTEGER NOT NULL DEFAULT 0,
    failed_count INTEGER NOT NULL DEFAULT 0,
    settings_json TEXT NOT NULL DEFAULT '{}',
    error TEXT NOT NULL DEFAULT '',
    started_at TEXT NOT NULL,
    completed_at TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_catalog_ai_runs_scope
ON catalog_ai_runs(seller_id,marketplace_account_id,source_snapshot_id,purpose,status,started_at);

CREATE TABLE IF NOT EXISTS catalog_ai_product_results (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ai_run_id INTEGER NOT NULL REFERENCES catalog_ai_runs(id) ON DELETE CASCADE,
    canonical_product_id INTEGER NOT NULL REFERENCES canonical_products(id) ON DELETE CASCADE,
    task_type TEXT NOT NULL,
    input_hash TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL,
    provider TEXT NOT NULL DEFAULT '',
    model TEXT NOT NULL DEFAULT '',
    response_json TEXT NOT NULL DEFAULT '{}',
    accepted_json TEXT NOT NULL DEFAULT '{}',
    rejection_reason TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_catalog_ai_product_results_scope
ON catalog_ai_product_results(ai_run_id,canonical_product_id,task_type,status);

CREATE TABLE IF NOT EXISTS product_ai_attribute_mappings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    seller_id INTEGER NOT NULL REFERENCES sellers(id) ON DELETE CASCADE,
    marketplace_account_id INTEGER NOT NULL REFERENCES marketplace_accounts(id) ON DELETE CASCADE,
    taxonomy_snapshot_id INTEGER NOT NULL REFERENCES taxonomy_snapshots(id) ON DELETE CASCADE,
    source_snapshot_id INTEGER NOT NULL REFERENCES source_catalog_snapshots(id) ON DELETE CASCADE,
    canonical_product_id INTEGER NOT NULL REFERENCES canonical_products(id) ON DELETE CASCADE,
    category_external_id TEXT NOT NULL,
    attribute_external_id TEXT NOT NULL,
    value_json TEXT NOT NULL DEFAULT 'null',
    evidence_path TEXT NOT NULL DEFAULT '',
    evidence_value_json TEXT NOT NULL DEFAULT 'null',
    confidence REAL NOT NULL DEFAULT 0,
    reason TEXT NOT NULL DEFAULT '',
    ai_profile_id INTEGER NOT NULL,
    ai_run_id INTEGER REFERENCES catalog_ai_runs(id) ON DELETE SET NULL,
    status TEXT NOT NULL DEFAULT 'REVIEW',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(marketplace_account_id,source_snapshot_id,canonical_product_id,category_external_id,attribute_external_id)
);
CREATE INDEX IF NOT EXISTS idx_product_ai_attribute_mappings_scope
ON product_ai_attribute_mappings(
    seller_id,marketplace_account_id,source_snapshot_id,canonical_product_id,category_external_id,status
);


CREATE TABLE IF NOT EXISTS publication_item_runtime (
    publication_item_id INTEGER PRIMARY KEY REFERENCES publication_items(id) ON DELETE CASCADE,
    seller_id INTEGER NOT NULL REFERENCES sellers(id) ON DELETE CASCADE,
    marketplace_account_id INTEGER NOT NULL REFERENCES marketplace_accounts(id) ON DELETE CASCADE,
    marketplace TEXT NOT NULL,
    storefront TEXT NOT NULL DEFAULT '',
    ean TEXT NOT NULL DEFAULT '',
    seller_sku TEXT NOT NULL DEFAULT '',
    idempotency_key TEXT NOT NULL,
    duplicate_check_status TEXT NOT NULL DEFAULT 'NOT_CHECKED',
    remote_product_exists INTEGER NOT NULL DEFAULT 0,
    remote_offer_exists INTEGER NOT NULL DEFAULT 0,
    planned_action TEXT NOT NULL DEFAULT 'CHECK_DUPLICATE',
    next_action TEXT NOT NULL DEFAULT 'CHECK_DUPLICATE',
    retryable INTEGER NOT NULL DEFAULT 0,
    last_http_status INTEGER,
    last_response_json TEXT NOT NULL DEFAULT '{}',
    next_poll_at TEXT NOT NULL DEFAULT '',
    submitted_at TEXT NOT NULL DEFAULT '',
    completed_at TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(marketplace_account_id,idempotency_key)
);
CREATE INDEX IF NOT EXISTS idx_publication_item_runtime_scope
ON publication_item_runtime(
    seller_id,marketplace_account_id,marketplace,next_action,duplicate_check_status,updated_at
);

CREATE TABLE IF NOT EXISTS publication_item_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    publication_job_id INTEGER NOT NULL REFERENCES publication_jobs(id) ON DELETE CASCADE,
    publication_item_id INTEGER NOT NULL REFERENCES publication_items(id) ON DELETE CASCADE,
    canonical_product_id INTEGER NOT NULL REFERENCES canonical_products(id) ON DELETE CASCADE,
    event_type TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT '',
    message TEXT NOT NULL DEFAULT '',
    details_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_publication_item_events_scope
ON publication_item_events(publication_job_id,publication_item_id,event_type,created_at);

CREATE TABLE IF NOT EXISTS product_channel_states (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    seller_id INTEGER NOT NULL REFERENCES sellers(id) ON DELETE CASCADE,
    marketplace_account_id INTEGER NOT NULL REFERENCES marketplace_accounts(id) ON DELETE CASCADE,
    marketplace TEXT NOT NULL,
    environment TEXT NOT NULL DEFAULT 'live',
    storefront TEXT NOT NULL DEFAULT '',
    locale TEXT NOT NULL DEFAULT '',
    canonical_product_id INTEGER REFERENCES canonical_products(id) ON DELETE SET NULL,
    ean TEXT NOT NULL DEFAULT '',
    seller_sku TEXT NOT NULL DEFAULT '',
    external_product_id TEXT NOT NULL DEFAULT '',
    external_offer_id TEXT NOT NULL DEFAULT '',
    product_status TEXT NOT NULL DEFAULT '',
    offer_status TEXT NOT NULL DEFAULT '',
    product_payload_hash TEXT NOT NULL DEFAULT '',
    offer_payload_hash TEXT NOT NULL DEFAULT '',
    last_import_id TEXT NOT NULL DEFAULT '',
    last_error TEXT NOT NULL DEFAULT '',
    last_checked_at TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(marketplace_account_id,environment,storefront,ean,seller_sku)
);
CREATE INDEX IF NOT EXISTS idx_product_channel_states_lookup
ON product_channel_states(
    seller_id,marketplace_account_id,environment,storefront,ean,seller_sku,product_status,offer_status
);

CREATE TABLE IF NOT EXISTS publication_artifacts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    publication_job_id INTEGER NOT NULL REFERENCES publication_jobs(id) ON DELETE CASCADE,
    marketplace_import_id INTEGER REFERENCES marketplace_imports(id) ON DELETE CASCADE,
    artifact_type TEXT NOT NULL,
    filename TEXT NOT NULL,
    local_path TEXT NOT NULL DEFAULT '',
    storage_key TEXT NOT NULL DEFAULT '',
    storage_backend TEXT NOT NULL DEFAULT '',
    storage_sha256 TEXT NOT NULL DEFAULT '',
    storage_size_bytes INTEGER NOT NULL DEFAULT 0,
    content_hash TEXT NOT NULL DEFAULT '',
    row_count INTEGER NOT NULL DEFAULT 0,
    metadata_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    UNIQUE(publication_job_id,artifact_type,content_hash)
);
CREATE INDEX IF NOT EXISTS idx_publication_artifacts_job
ON publication_artifacts(publication_job_id,artifact_type,created_at);
"""


def _schema_runtime_key() -> tuple:
    engine = db_service.database_engine()
    if engine == "postgresql":
        public = db_service.database_config_public()
        return (
            "postgresql",
            str(public.get("postgresql_host") or ""),
            str(public.get("postgresql_port") or ""),
            str(public.get("postgresql_database") or ""),
            str(public.get("postgresql_user") or ""),
            CATALOG_SCHEMA_REVISION,
        )
    return ("sqlite", str(db_service.Path(db_service.DB_PATH).resolve()), CATALOG_SCHEMA_REVISION)


def _reset_schema_cache_for_tests() -> None:
    with _SCHEMA_LOCK:
        _SCHEMA_READY.clear()


def ensure_schema(*, force: bool = False) -> None:
    """Ensure Catalog Intelligence DDL once per process/database revision.

    Repository functions call this guard defensively.  On Streamlit reruns this
    used to execute the full DDL plus a migration UPDATE dozens of times.  With
    SQLite, any such write can collide with a catalogue batch transaction.  v258
    makes repeated calls true no-ops after a successful migration while still
    preserving automatic migration on a fresh process/database target.
    """
    key = _schema_runtime_key()
    if not force and key in _SCHEMA_READY:
        return
    with _SCHEMA_LOCK:
        if not force and key in _SCHEMA_READY:
            return

        def migrate() -> None:
            with db_service.connect() as con:
                con.executescript(DDL)
                # v248 migration: low-confidence category is REVIEW, not a
                # technical BLOCKED product. Execute this only with the schema
                # migration, never on every repository read/write call.
                con.execute(
                    """
                    UPDATE product_category_assignments
                    SET status='REVIEW'
                    WHERE status='BLOCKED'
                    """
                )
                artifact_columns={str(item["name"]) for item in con.execute("PRAGMA table_info(publication_artifacts)").fetchall()}
                artifact_migrations={
                    "storage_key":"TEXT NOT NULL DEFAULT ''",
                    "storage_backend":"TEXT NOT NULL DEFAULT ''",
                    "storage_sha256":"TEXT NOT NULL DEFAULT ''",
                    "storage_size_bytes":"INTEGER NOT NULL DEFAULT 0",
                }
                for column,declaration in artifact_migrations.items():
                    if column not in artifact_columns:
                        con.execute(f"ALTER TABLE publication_artifacts ADD COLUMN {column} {declaration}")

        db_service._retry_locked(migrate, attempts=10, base_delay=0.15)
        _SCHEMA_READY.add(key)


__all__ = ["CATALOG_SCHEMA_REVISION", "DDL", "ensure_schema"]
