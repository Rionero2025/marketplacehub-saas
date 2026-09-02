from __future__ import annotations

import csv
import io
import json
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import services.db as db
from services.catalog_intelligence.accounts import load_marketplace_account
from services.catalog_intelligence.marketplaces.mirakl import (
    MiraklCatalogClient,
    MiraklCatalogError,
)
from services.durable_files import put_bytes as put_durable_bytes
from services.catalog_intelligence.repository import (
    create_marketplace_import,
    ensure_publication_runtime,
    marketplace_imports_for_job,
    publication_artifacts,
    publication_item,
    publication_items,
    publication_job,
    recalculate_publication_job,
    record_publication_event,
    save_publication_artifact,
    update_marketplace_import,
    update_publication_item,
    update_publication_job_settings,
    upsert_product_channel_state,
)
from services.catalog_intelligence.utils import canonical_json, clean_text, json_hash
from services.kaufland import KauflandClient, KauflandError
from services.worten import WORTEN_OFFER_COLUMNS


FINAL_ITEM_STATES = {
    "COMPLETED",
    "EXISTING_OFFER",
    "FAILED",
    "BLOCKED",
    "PRODUCT_REJECTED",
    "OFFER_REJECTED",
}
PRODUCT_ACTIONS = {"CREATE_PRODUCT", "UPDATE_PRODUCT"}
OFFER_ACTIONS = {"CREATE_OFFER", "UPDATE_OFFER"}
POLL_PRODUCT_ACTIONS = {"POLL_PRODUCT", "POLL_PRODUCT_IMPORT"}
POLL_OFFER_ACTIONS = {"POLL_OFFER", "POLL_OFFER_IMPORT"}


@dataclass(slots=True)
class PublicationResult:
    job_id: int
    action: str
    processed: int = 0
    succeeded: int = 0
    failed: int = 0
    pending: int = 0
    skipped: int = 0
    details: dict[str, Any] | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "job_id": self.job_id,
            "action": self.action,
            "processed": self.processed,
            "succeeded": self.succeeded,
            "failed": self.failed,
            "pending": self.pending,
            "skipped": self.skipped,
            "details": dict(self.details or {}),
        }


def _json(value: Any, default: Any) -> Any:
    if isinstance(value, (dict, list)):
        return value
    try:
        parsed = json.loads(value or "")
    except (TypeError, ValueError, json.JSONDecodeError):
        return default
    return parsed if isinstance(parsed, type(default)) else default


def _item_ids(values: Iterable[int] | None) -> set[int]:
    return {int(value) for value in values or () if int(value) > 0}


def _job_items(job_id: int, item_ids: Iterable[int] | None = None) -> tuple[dict, list[dict]]:
    job = publication_job(job_id)
    if not job:
        raise ValueError("Job di pubblicazione non trovato.")
    ensure_publication_runtime(job_id)
    items = publication_items(job_id, limit=100000)
    selected = _item_ids(item_ids)
    if selected:
        items = [item for item in items if int(item["id"]) in selected]
    return job, items


def _settings(job: Mapping[str, Any], overrides: Mapping[str, Any] | None = None) -> dict[str, Any]:
    current = _json(job.get("settings_json"), {})
    values = dict(overrides or {})
    current.update(values)
    if values and job.get("id") not in (None, ""):
        # Persist only operational choices.  Repository-side filtering prevents
        # API keys or other credentials from entering the job record.
        update_publication_job_settings(int(job["id"]), values)
    return current


def _kaufland_client(account: Mapping[str, Any], environment: str) -> KauflandClient:
    credentials = account.get("credentials") or {}
    client_key = clean_text(credentials.get("client_key"))
    secret_key = clean_text(credentials.get("secret_key"))
    if not client_key or not secret_key:
        raise ValueError("Client Key e Secret Key Kaufland sono obbligatorie.")
    return KauflandClient(
        client_key=client_key,
        secret_key=secret_key,
        playground=clean_text(environment).lower() in {"test", "playground"},
    )


def _mirakl_client(account: Mapping[str, Any]) -> MiraklCatalogClient:
    credentials = account.get("credentials") or {}
    return MiraklCatalogClient(
        api_url=clean_text(credentials.get("api_url")) or "https://marketplace.worten.pt/api",
        api_key=clean_text(credentials.get("api_key")),
        shop_id=clean_text(credentials.get("shop_id")),
    )


def _require_remote_write(config: Mapping[str, Any]) -> None:
    """Refuse marketplace writes unless the caller explicitly unlocks them."""
    if not bool(config.get("remote_write_enabled")):
        raise PermissionError(
            "Scrittura remota non abilitata. Seleziona modalità Reale e abilita "
            "esplicitamente la scrittura per questo ciclo."
        )


def _unwrap_data(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, Mapping):
        return {}
    data = payload.get("data")
    if isinstance(data, Mapping):
        return dict(data)
    if isinstance(data, list) and data and isinstance(data[0], Mapping):
        return dict(data[0])
    return dict(payload)


def _external_product_id(payload: Any) -> str:
    raw = _unwrap_data(payload)
    for key in ("id_product", "product_id", "id", "sku"):
        token = clean_text(raw.get(key))
        if token:
            return token
    return ""


def _external_offer_id(payload: Any) -> str:
    raw = _unwrap_data(payload)
    for key in ("id_unit", "offer_id", "id_offer", "shop_sku", "sku", "id"):
        token = clean_text(raw.get(key))
        if token:
            return token
    return ""


def _error_status(error: BaseException) -> tuple[int | None, bool]:
    status = getattr(error, "status_code", None)
    if status is None:
        match = re.search(r"HTTP\s+(\d{3})", str(error), flags=re.IGNORECASE)
        status = int(match.group(1)) if match else None
    retryable = status in {408, 425, 429, 500, 502, 503, 504} or status is None
    return status, retryable


def _record_error(job: Mapping[str, Any], item: Mapping[str, Any], error: BaseException, *, action: str) -> None:
    status_code, retryable = _error_status(error)
    message = clean_text(error)[:4000]
    update_publication_item(
        int(item["id"]),
        status="RUNNING" if retryable else "REVIEW_REQUIRED",
        last_error=message,
        attempt_delta=1,
        runtime={
            "retryable": retryable,
            "last_http_status": status_code,
            "last_response_json": {"error": message, "action": action},
            "next_action": action if retryable else "REVIEW",
        },
    )
    record_publication_event(
        job_id=int(job["id"]),
        item_id=int(item["id"]),
        product_id=int(item["canonical_product_id"]),
        event_type="API_ERROR",
        status="RETRYABLE" if retryable else "REVIEW_REQUIRED",
        message=message,
        details={"action": action, "http_status": status_code, "retryable": retryable},
    )


def _artifact_dir(job_id: int) -> Path:
    path = Path(db.DATA_DIR) / "catalog_intelligence" / "publication" / f"job_{int(job_id)}"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _save_artifact(
    *,
    job_id: int,
    artifact_type: str,
    filename: str,
    content: bytes,
    row_count: int,
    metadata: Mapping[str, Any] | None = None,
    marketplace_import_id: int | None = None,
) -> dict[str, Any]:
    path = _artifact_dir(job_id) / filename
    path.write_bytes(bytes(content))
    stored=put_durable_bytes(
        namespace="publication_artifacts",identity=f"job_{int(job_id)}",
        filename=filename,content=bytes(content),
        content_type="text/csv" if str(filename).lower().endswith(".csv") else "application/octet-stream",
    )
    content_hash = json_hash({"sha256": __import__("hashlib").sha256(content).hexdigest(), "size": len(content)})
    artifact_id = save_publication_artifact(
        job_id=job_id,
        artifact_type=artifact_type,
        filename=filename,
        local_path=str(path),
        content_hash=content_hash,
        row_count=row_count,
        metadata=metadata,
        marketplace_import_id=marketplace_import_id,
        storage_key=stored["storage_key"],storage_backend=stored["storage_backend"],
        storage_sha256=stored["sha256"],storage_size_bytes=stored["size_bytes"],
    )
    return {
        "id": artifact_id,
        "path": str(path),
        "filename": filename,
        "content_hash": content_hash,
        "row_count": row_count,
        "storage_key": stored["storage_key"],
    }


def _mirakl_product_records(items: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for item in items:
        payload = _json(item.get("product_payload_json"), {})
        record = payload.get("csv_row") if isinstance(payload.get("csv_row"), Mapping) else payload.get("attributes")
        if not isinstance(record, Mapping):
            continue
        normalized = {clean_text(key): value for key, value in record.items() if clean_text(key)}
        if normalized:
            records.append(normalized)
    return records


def build_mirakl_product_csv(items: Iterable[Mapping[str, Any]]) -> bytes:
    records = _mirakl_product_records(items)
    if not records:
        raise ValueError("Nessuna riga prodotto Mirakl valida da esportare.")
    preferred = [
        "product-sku",
        "product-id",
        "product-id-type",
        "category-code",
        "title",
        "description",
        "short-description",
        "brand",
        "manufacturer-part-number",
    ]
    image_fields = sorted(
        {key for record in records for key in record if re.fullmatch(r"image-\d+", key)},
        key=lambda value: int(value.split("-")[-1]),
    )
    others = sorted({key for record in records for key in record} - set(preferred) - set(image_fields))
    fieldnames = [key for key in preferred if any(key in record for record in records)] + image_fields + others
    output = io.StringIO(newline="")
    writer = csv.DictWriter(output, fieldnames=fieldnames, delimiter=";", lineterminator="\n", extrasaction="ignore")
    writer.writeheader()
    for record in records:
        writer.writerow({key: _csv_value(record.get(key)) for key in fieldnames})
    return output.getvalue().encode("utf-8-sig")


def _csv_value(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (list, tuple, set)):
        return "|".join(_csv_value(item) for item in value if _csv_value(item))
    if isinstance(value, Mapping):
        return canonical_json(dict(value))
    return str(value)


def build_mirakl_offer_csv(
    items: Iterable[Mapping[str, Any]],
    *,
    settings: Mapping[str, Any] | None = None,
) -> bytes:
    config = dict(settings or {})
    records: list[dict[str, Any]] = []
    for item in items:
        payload = _json(item.get("offer_payload_json"), {})
        sku = clean_text(payload.get("shop_sku") or item.get("supplier_sku"))
        ean = clean_text(payload.get("product_id") or item.get("ean"))
        if not sku or not ean:
            continue
        record = {column: "" for column in WORTEN_OFFER_COLUMNS}
        record.update(
            {
                "sku": sku,
                "product-id": ean,
                "product-id-type": clean_text(payload.get("product_id_type")) or "EAN",
                "price": _csv_value(payload.get("price")),
                "quantity": _csv_value(payload.get("quantity")),
                "state": clean_text(payload.get("state_code")) or clean_text(config.get("offer_state")) or "11",
                "logistic-class": clean_text(payload.get("logistic_class")) or clean_text(config.get("logistic_class")),
                "leadtime-to-ship": _csv_value(payload.get("leadtime_to_ship") or config.get("leadtime_to_ship") or 1),
                "update-delete": "update",
                "ship-from-country-offer": clean_text(config.get("ship_from_country")).upper(),
            }
        )
        records.append(record)
    if not records:
        raise ValueError("Nessuna offerta Mirakl valida da esportare.")
    output = io.StringIO(newline="")
    writer = csv.DictWriter(output, fieldnames=WORTEN_OFFER_COLUMNS, delimiter=";", lineterminator="\n")
    writer.writeheader()
    writer.writerows(records)
    return output.getvalue().encode("utf-8-sig")


def _recursive_records(payload: Any) -> Iterable[Mapping[str, Any]]:
    if isinstance(payload, Mapping):
        yield payload
        for value in payload.values():
            yield from _recursive_records(value)
    elif isinstance(payload, list):
        for value in payload:
            yield from _recursive_records(value)


def _mirakl_duplicate_state(payload: Any, *, ean: str, seller_sku: str) -> tuple[bool, bool, str, str]:
    product_exists = False
    offer_exists = False
    external_product_id = ""
    external_offer_id = ""
    expected_ean = clean_text(ean)
    expected_sku = clean_text(seller_sku)
    for raw in _recursive_records(payload):
        values = {clean_text(value) for value in raw.values() if not isinstance(value, (dict, list))}
        references = raw.get("product_references") or raw.get("references") or []
        if isinstance(references, Mapping):
            references = [references]
        reference_values: set[str] = set()
        for reference in references if isinstance(references, list) else []:
            if isinstance(reference, Mapping):
                reference_values.add(clean_text(reference.get("reference") or reference.get("value")))
        raw_product = clean_text(raw.get("product_id") or raw.get("product_sku") or raw.get("sku"))
        if expected_ean and (expected_ean in values or expected_ean in reference_values or raw_product == expected_ean):
            product_exists = True
            external_product_id = clean_text(raw.get("product_sku") or raw.get("product_id") or raw.get("id")) or external_product_id
        raw_offer_sku = clean_text(raw.get("shop_sku") or raw.get("offer_sku") or raw.get("sku"))
        if expected_sku and raw_offer_sku == expected_sku:
            offer_exists = True
            product_exists = True
            external_offer_id = clean_text(raw.get("offer_id") or raw.get("id")) or raw_offer_sku
    return product_exists, offer_exists, external_product_id, external_offer_id


def extract_import_id(payload: Any) -> str:
    if isinstance(payload, Mapping):
        for key in ("import_id", "import-id", "id", "importId"):
            token = clean_text(payload.get(key))
            if token:
                return token
        for value in payload.values():
            token = extract_import_id(value)
            if token:
                return token
    elif isinstance(payload, list):
        for value in payload:
            token = extract_import_id(value)
            if token:
                return token
    return ""


def interpret_mirakl_import_status(payload: Any) -> dict[str, Any]:
    flat: dict[str, Any] = {}
    for raw in _recursive_records(payload):
        for key, value in raw.items():
            normalized = clean_text(key).lower().replace("-", "_")
            if normalized not in flat and not isinstance(value, (dict, list)):
                flat[normalized] = value
    status = clean_text(
        flat.get("status")
        or flat.get("import_status")
        or flat.get("product_import_status")
        or flat.get("integration_status")
        or flat.get("transformation_status")
    ).upper()
    terminal_success = {"COMPLETE", "COMPLETED", "DONE", "SUCCESS", "SUCCEEDED", "PROCESSED"}
    terminal_failure = {"FAILED", "FAIL", "ERROR", "ABORTED", "CANCELLED", "REJECTED"}

    def number(*keys: str) -> int | None:
        for key in keys:
            value = flat.get(key)
            if value not in (None, ""):
                try:
                    return int(float(str(value)))
                except (TypeError, ValueError):
                    continue
        return None

    error_count = number(
        "error_count",
        "errors_count",
        "number_of_errors",
        "products_in_error",
        "invalid_lines",
        "lines_in_error",
    )
    success_count = number(
        "success_count",
        "successful_count",
        "number_of_successes",
        "products_successfully_imported",
        "valid_lines",
        "lines_in_success",
    )
    total_count = number("total_count", "processed_count", "products_count", "lines_count")
    done = status in terminal_success or status in terminal_failure
    failed = status in terminal_failure
    if not status:
        done_flag = flat.get("import_completed") or flat.get("completed") or flat.get("done")
        if str(done_flag).lower() in {"true", "1", "yes"}:
            done = True
            status = "COMPLETED"
    return {
        "status": status or "UNKNOWN",
        "done": done,
        "failed": failed,
        "error_count": error_count,
        "success_count": success_count,
        "total_count": total_count,
        "flat": flat,
    }


def _parse_report_identifiers(content: bytes) -> set[str]:
    if not content:
        return set()
    text = content.decode("utf-8-sig", errors="replace")
    try:
        dialect = csv.Sniffer().sniff(text[:8192], delimiters=";,\t,")
    except csv.Error:
        dialect = csv.excel
        dialect.delimiter = ";"
    result: set[str] = set()
    try:
        reader = csv.DictReader(io.StringIO(text), dialect=dialect)
        for row in reader:
            normalized = {clean_text(key).lower().replace("_", "-"): clean_text(value) for key, value in row.items()}
            for key in (
                "product-sku",
                "shop-sku",
                "sku",
                "product-id",
                "ean",
                "product-reference",
            ):
                token = normalized.get(key)
                if token:
                    result.add(token)
    except csv.Error:
        return set()
    return result


def _plan_item_kaufland(
    job: Mapping[str, Any],
    item: Mapping[str, Any],
    client: KauflandClient,
    config: Mapping[str, Any],
) -> None:
    ean = clean_text(item.get("ean"))
    offer_payload = _json(item.get("offer_payload_json"), {})
    seller_sku = clean_text(offer_payload.get("id_offer") or item.get("supplier_sku"))
    storefront = clean_text(job.get("storefront")).lower()
    product_response = client.product_by_ean_or_none(ean, storefront)
    product_exists = product_response is not None
    product_id = _external_product_id(product_response)
    units = client.units(seller_sku, storefront) if seller_sku else []
    exact_units = [
        unit for unit in units
        if clean_text(unit.get("id_offer")) == seller_sku
        and clean_text(unit.get("storefront") or storefront).lower() == storefront
    ]
    offer_exists = bool(exact_units)
    offer_id = _external_offer_id(exact_units[0]) if exact_units else ""
    update_product = bool(config.get("update_existing_product_data"))
    update_offer = bool(config.get("update_existing_offers"))
    if product_exists and offer_exists and not update_product and not update_offer:
        planned = "NO_ACTION"
        next_action = "NONE"
        status = "EXISTING_OFFER"
        product_status = "EXISTS"
        offer_status = "EXISTS"
    elif not product_exists:
        planned = "CREATE_PRODUCT"
        next_action = "CREATE_PRODUCT"
        status = "PLANNED"
        product_status = "MISSING"
        offer_status = "PENDING"
    elif update_product:
        planned = "UPDATE_PRODUCT"
        next_action = "UPDATE_PRODUCT"
        status = "PLANNED"
        product_status = "EXISTS"
        offer_status = "EXISTS" if offer_exists else "PENDING"
    else:
        planned = "UPDATE_OFFER" if offer_exists and update_offer else "CREATE_OFFER"
        next_action = planned
        status = "OFFER_PENDING"
        product_status = "ACCEPTED"
        offer_status = "EXISTS" if offer_exists else "MISSING"
    update_publication_item(
        int(item["id"]),
        status=status,
        product_status=product_status,
        offer_status=offer_status,
        external_product_id=product_id,
        external_offer_id=offer_id,
        last_error="",
        runtime={
            "duplicate_check_status": "COMPLETED",
            "remote_product_exists": product_exists,
            "remote_offer_exists": offer_exists,
            "planned_action": planned,
            "next_action": next_action,
            "retryable": False,
            "last_response_json": {
                "product": _unwrap_data(product_response) if product_response else {},
                "matching_units": exact_units,
            },
        },
    )
    upsert_product_channel_state(
        seller_id=int(job["seller_id"]),
        account_id=int(job["marketplace_account_id"]),
        marketplace="kaufland",
        environment=clean_text(job.get("environment")) or "live",
        storefront=storefront,
        locale=clean_text(job.get("locale")),
        canonical_product_id=int(item["canonical_product_id"]),
        ean=ean,
        seller_sku=seller_sku,
        external_product_id=product_id,
        external_offer_id=offer_id,
        product_status=product_status,
        offer_status=offer_status,
        product_payload_hash=clean_text(item.get("payload_hash")),
    )
    record_publication_event(
        job_id=int(job["id"]), item_id=int(item["id"]),
        product_id=int(item["canonical_product_id"]), event_type="DUPLICATE_CHECK",
        status=status, message=f"Pianificato: {planned}",
        details={"product_exists": product_exists, "offer_exists": offer_exists, "planned_action": planned},
    )


def _plan_item_mirakl(
    job: Mapping[str, Any],
    item: Mapping[str, Any],
    client: MiraklCatalogClient,
    config: Mapping[str, Any],
) -> None:
    ean = clean_text(item.get("ean"))
    offer_payload = _json(item.get("offer_payload_json"), {})
    seller_sku = clean_text(offer_payload.get("shop_sku") or item.get("supplier_sku"))
    payload = client.product_offers(ean=ean)
    product_exists, offer_exists, product_id, offer_id = _mirakl_duplicate_state(
        payload, ean=ean, seller_sku=seller_sku
    )
    update_product = bool(config.get("update_existing_product_data"))
    update_offer = bool(config.get("update_existing_offers"))
    if product_exists and offer_exists and not update_product and not update_offer:
        planned = "NO_ACTION"
        next_action = "NONE"
        status = "EXISTING_OFFER"
        product_status = "EXISTS"
        offer_status = "EXISTS"
    elif not product_exists:
        planned = "CREATE_PRODUCT"
        next_action = "CREATE_PRODUCT"
        status = "PLANNED"
        product_status = "MISSING"
        offer_status = "PENDING"
    elif update_product:
        planned = "UPDATE_PRODUCT"
        next_action = "UPDATE_PRODUCT"
        status = "PLANNED"
        product_status = "EXISTS"
        offer_status = "EXISTS" if offer_exists else "PENDING"
    else:
        planned = "UPDATE_OFFER" if offer_exists and update_offer else "CREATE_OFFER"
        next_action = planned
        status = "OFFER_PENDING"
        product_status = "ACCEPTED"
        offer_status = "EXISTS" if offer_exists else "MISSING"
    update_publication_item(
        int(item["id"]),
        status=status,
        product_status=product_status,
        offer_status=offer_status,
        external_product_id=product_id,
        external_offer_id=offer_id,
        last_error="",
        runtime={
            "duplicate_check_status": "COMPLETED",
            "remote_product_exists": product_exists,
            "remote_offer_exists": offer_exists,
            "planned_action": planned,
            "next_action": next_action,
            "retryable": False,
            "last_response_json": payload if isinstance(payload, Mapping) else {"payload": payload},
        },
    )
    upsert_product_channel_state(
        seller_id=int(job["seller_id"]), account_id=int(job["marketplace_account_id"]),
        marketplace="worten", environment=clean_text(job.get("environment")) or "live",
        storefront=clean_text(job.get("storefront")).lower(), locale=clean_text(job.get("locale")),
        canonical_product_id=int(item["canonical_product_id"]), ean=ean, seller_sku=seller_sku,
        external_product_id=product_id, external_offer_id=offer_id,
        product_status=product_status, offer_status=offer_status,
        product_payload_hash=clean_text(item.get("payload_hash")),
    )
    record_publication_event(
        job_id=int(job["id"]), item_id=int(item["id"]), product_id=int(item["canonical_product_id"]),
        event_type="DUPLICATE_CHECK", status=status, message=f"Pianificato: {planned}",
        details={"product_exists": product_exists, "offer_exists": offer_exists, "planned_action": planned},
    )


def plan_publication_job(
    job_id: int,
    *,
    item_ids: Iterable[int] | None = None,
    settings: Mapping[str, Any] | None = None,
    allow_review_items: bool = False,
) -> PublicationResult:
    job, items = _job_items(job_id, item_ids)
    config = _settings(job, settings)
    account = load_marketplace_account(int(job["marketplace_account_id"]), seller_id=int(job["seller_id"]))
    marketplace = clean_text(job.get("marketplace")).lower()
    client: Any = (
        _kaufland_client(account, clean_text(job.get("environment")))
        if marketplace == "kaufland"
        else _mirakl_client(account)
    )
    result = PublicationResult(job_id=int(job_id), action="PLAN")
    force_existing_update = bool(
        config.get("update_existing_product_data") or config.get("update_existing_offers")
    )
    for item in items:
        current_status = clean_text(item.get("status")).upper()
        if current_status in FINAL_ITEM_STATES and not force_existing_update:
            result.skipped += 1
            continue
        if current_status == "REVIEW_REQUIRED" and not allow_review_items:
            result.skipped += 1
            continue
        if (
            current_status not in {"READY", "REVIEW_REQUIRED", "PLANNED", "RUNNING"}
            and clean_text(item.get("duplicate_check_status")).upper() == "COMPLETED"
            and not force_existing_update
        ):
            result.skipped += 1
            continue
        result.processed += 1
        update_publication_item(
            int(item["id"]), status="PLANNING",
            runtime={"duplicate_check_status": "RUNNING", "next_action": "CHECK_DUPLICATE"},
        )
        try:
            if marketplace == "kaufland":
                _plan_item_kaufland(job, item, client, config)
            elif marketplace == "worten":
                _plan_item_mirakl(job, item, client, config)
            else:
                raise ValueError(f"Marketplace non supportato: {marketplace}")
        except Exception as exc:
            _record_error(job, item, exc, action="CHECK_DUPLICATE")
            result.failed += 1
        else:
            result.succeeded += 1
    result.details = recalculate_publication_job(job_id)
    return result


def _simulation_artifact(job: Mapping[str, Any], items: list[Mapping[str, Any]]) -> dict[str, Any]:
    marketplace = clean_text(job.get("marketplace")).lower()
    if marketplace == "worten":
        product_items = [item for item in items if clean_text(item.get("next_action")).upper() in PRODUCT_ACTIONS]
        offer_items = [item for item in items if clean_text(item.get("next_action")).upper() in OFFER_ACTIONS]
        artifacts: dict[str, Any] = {}
        if product_items:
            content = build_mirakl_product_csv(product_items)
            artifacts["product"] = _save_artifact(
                job_id=int(job["id"]), artifact_type="MIRAKL_PRODUCT_CSV_SIMULATION",
                filename="mirakl_products_simulation.csv", content=content,
                row_count=len(product_items), metadata={"mode": "SIMULATION"},
            )
        if offer_items:
            content = build_mirakl_offer_csv(offer_items, settings=_settings(job))
            artifacts["offer"] = _save_artifact(
                job_id=int(job["id"]), artifact_type="MIRAKL_OFFER_CSV_SIMULATION",
                filename="mirakl_offers_simulation.csv", content=content,
                row_count=len(offer_items), metadata={"mode": "SIMULATION"},
            )
        return artifacts
    content = json.dumps(
        [
            {
                "item_id": item["id"],
                "ean": item.get("ean"),
                "sku": item.get("seller_sku") or item.get("supplier_sku"),
                "action": item.get("next_action"),
                "product_payload": _json(item.get("product_payload_json"), {}),
                "offer_payload": _json(item.get("offer_payload_json"), {}),
            }
            for item in items
        ],
        ensure_ascii=False,
        indent=2,
    ).encode("utf-8")
    return {
        "kaufland": _save_artifact(
            job_id=int(job["id"]), artifact_type="KAUFLAND_JSON_SIMULATION",
            filename="kaufland_simulation.json", content=content,
            row_count=len(items), metadata={"mode": "SIMULATION"},
        )
    }


def simulate_publication_job(
    job_id: int,
    *,
    item_ids: Iterable[int] | None = None,
) -> PublicationResult:
    job, items = _job_items(job_id, item_ids)
    planned = [
        item for item in items
        if clean_text(item.get("next_action")).upper() in PRODUCT_ACTIONS | OFFER_ACTIONS
    ]
    artifacts = _simulation_artifact(job, planned)
    for item in planned:
        record_publication_event(
            job_id=int(job_id), item_id=int(item["id"]), product_id=int(item["canonical_product_id"]),
            event_type="SIMULATION", status="OK", message=f"Simulazione {item.get('next_action')}",
            details={"action": item.get("next_action")},
        )
    return PublicationResult(
        job_id=int(job_id), action="SIMULATION", processed=len(planned),
        succeeded=len(planned), skipped=max(0, len(items) - len(planned)), details={"artifacts": artifacts},
    )


def _submit_kaufland_products(
    job: Mapping[str, Any],
    items: list[Mapping[str, Any]],
    client: KauflandClient,
) -> PublicationResult:
    result = PublicationResult(job_id=int(job["id"]), action="SUBMIT_PRODUCTS")
    storefront = clean_text(job.get("storefront")).lower()
    locale = clean_text(job.get("locale"))
    for item in items:
        if clean_text(item.get("next_action")).upper() not in PRODUCT_ACTIONS:
            result.skipped += 1
            continue
        result.processed += 1
        payload = _json(item.get("product_payload_json"), {})
        try:
            response = client.put_product_data(payload, storefront, locale=locale)
        except Exception as exc:
            _record_error(job, item, exc, action=clean_text(item.get("next_action")).upper())
            result.failed += 1
            continue
        update_publication_item(
            int(item["id"]), status="PRODUCT_PROCESSING", product_status="SUBMITTED",
            attempt_delta=1, last_error="",
            runtime={
                "next_action": "POLL_PRODUCT", "retryable": False,
                "submitted_at": db.now_iso(),
                "last_response_json": response if isinstance(response, Mapping) else {"response": response},
            },
        )
        record_publication_event(
            job_id=int(job["id"]), item_id=int(item["id"]), product_id=int(item["canonical_product_id"]),
            event_type="PRODUCT_SUBMITTED", status="PROCESSING", message="Product data inviati a Kaufland.",
            details={"response": response if isinstance(response, Mapping) else {"response": response}},
        )
        result.pending += 1
    result.details = recalculate_publication_job(int(job["id"]))
    return result


def _submit_mirakl_products(
    job: Mapping[str, Any],
    items: list[Mapping[str, Any]],
    client: MiraklCatalogClient,
    config: Mapping[str, Any],
) -> PublicationResult:
    selected = [item for item in items if clean_text(item.get("next_action")).upper() in PRODUCT_ACTIONS]
    result = PublicationResult(job_id=int(job["id"]), action="SUBMIT_PRODUCTS", processed=len(selected))
    if not selected:
        result.skipped = len(items)
        return result
    csv_bytes = build_mirakl_product_csv(selected)
    artifact = _save_artifact(
        job_id=int(job["id"]), artifact_type="MIRAKL_PRODUCT_CSV",
        filename=f"mirakl_products_job_{int(job['id'])}.csv", content=csv_bytes,
        row_count=len(selected), metadata={"import_mode": clean_text(config.get("product_import_mode")) or "NORMAL"},
    )
    try:
        response = client.import_products(
            csv_bytes,
            filename=artifact["filename"],
            import_mode=clean_text(config.get("product_import_mode")) or "NORMAL",
            operator_format=bool(config.get("product_operator_format", True)),
        )
        import_id = extract_import_id(response)
        if not import_id:
            raise MiraklCatalogError(f"P41 non ha restituito un import_id: {response}")
    except Exception as exc:
        for item in selected:
            _record_error(job, item, exc, action=clean_text(item.get("next_action")).upper())
        result.failed = len(selected)
        result.details = recalculate_publication_job(int(job["id"]))
        return result
    import_row_id = create_marketplace_import(
        job_id=int(job["id"]), account_id=int(job["marketplace_account_id"]),
        import_type="MIRAKL_PRODUCT_P41", external_import_id=import_id, status="PROCESSING",
        request_data={"artifact": artifact, "item_ids": [int(item["id"]) for item in selected]},
        response_data=response if isinstance(response, Mapping) else {"response": response},
    )
    save_publication_artifact(
        job_id=int(job["id"]), artifact_type="MIRAKL_PRODUCT_CSV",
        filename=artifact["filename"], local_path=artifact["path"], content_hash=artifact["content_hash"],
        row_count=len(selected), metadata={"import_id": import_id}, marketplace_import_id=import_row_id,
    )
    for item in selected:
        update_publication_item(
            int(item["id"]), status="PRODUCT_PROCESSING", product_status="SUBMITTED",
            import_id=import_id, attempt_delta=1, last_error="",
            runtime={
                "next_action": "POLL_PRODUCT_IMPORT", "submitted_at": db.now_iso(),
                "last_response_json": response if isinstance(response, Mapping) else {"response": response},
            },
        )
        record_publication_event(
            job_id=int(job["id"]), item_id=int(item["id"]), product_id=int(item["canonical_product_id"]),
            event_type="PRODUCT_IMPORT_SUBMITTED", status="PROCESSING",
            message=f"Import prodotto Mirakl {import_id} inviato.", details={"import_id": import_id},
        )
    result.pending = len(selected)
    result.details = {"import_id": import_id, "artifact": artifact, **recalculate_publication_job(int(job["id"]))}
    return result


def submit_products(
    job_id: int,
    *,
    item_ids: Iterable[int] | None = None,
    settings: Mapping[str, Any] | None = None,
    max_items: int = 100,
) -> PublicationResult:
    job, items = _job_items(job_id, item_ids)
    config = _settings(job, settings)
    _require_remote_write(config)
    selected = [item for item in items if clean_text(item.get("next_action")).upper() in PRODUCT_ACTIONS]
    selected = selected[: max(1, int(max_items))]
    account = load_marketplace_account(int(job["marketplace_account_id"]), seller_id=int(job["seller_id"]))
    marketplace = clean_text(job.get("marketplace")).lower()
    if marketplace == "kaufland":
        return _submit_kaufland_products(job, selected, _kaufland_client(account, clean_text(job.get("environment"))))
    if marketplace == "worten":
        return _submit_mirakl_products(job, selected, _mirakl_client(account), config)
    raise ValueError(f"Marketplace non supportato: {marketplace}")


def _kaufland_product_status(payload: Any) -> tuple[str, str]:
    raw = _unwrap_data(payload)
    status = clean_text(raw.get("update_status") or raw.get("status") or raw.get("state")).upper()
    reason = clean_text(raw.get("update_fail_reason") or raw.get("fail_reason") or raw.get("message"))
    return status or "UNKNOWN", reason


def _refresh_kaufland_products(
    job: Mapping[str, Any],
    items: list[Mapping[str, Any]],
    client: KauflandClient,
) -> PublicationResult:
    result = PublicationResult(job_id=int(job["id"]), action="REFRESH_PRODUCTS")
    storefront = clean_text(job.get("storefront")).lower()
    locale = clean_text(job.get("locale"))
    for item in items:
        if clean_text(item.get("next_action")).upper() != "POLL_PRODUCT":
            result.skipped += 1
            continue
        result.processed += 1
        try:
            response = client.product_data_status(clean_text(item.get("ean")), storefront, locale=locale)
            status, reason = _kaufland_product_status(response)
        except Exception as exc:
            _record_error(job, item, exc, action="POLL_PRODUCT")
            result.failed += 1
            continue
        if status == "SUCCESS":
            try:
                product = client.product_by_ean_or_none(clean_text(item.get("ean")), storefront)
            except Exception:
                product = None
            product_id = _external_product_id(product)
            update_publication_item(
                int(item["id"]), status="OFFER_PENDING", product_status="ACCEPTED",
                offer_status="PENDING", external_product_id=product_id, last_error="",
                runtime={"next_action": "CREATE_OFFER", "last_response_json": response},
            )
            result.succeeded += 1
        elif status in {"FAIL", "FAILED", "REJECTED"}:
            update_publication_item(
                int(item["id"]), status="PRODUCT_REJECTED", product_status="REJECTED",
                last_error=reason or f"Kaufland product data: {status}",
                runtime={"next_action": "REVIEW", "retryable": False, "last_response_json": response},
            )
            result.failed += 1
        else:
            update_publication_item(
                int(item["id"]), status="PRODUCT_PROCESSING", product_status=status,
                runtime={"next_action": "POLL_PRODUCT", "last_response_json": response},
            )
            result.pending += 1
        record_publication_event(
            job_id=int(job["id"]), item_id=int(item["id"]), product_id=int(item["canonical_product_id"]),
            event_type="PRODUCT_STATUS", status=status, message=reason or status,
            details={"response": response if isinstance(response, Mapping) else {"response": response}},
        )
    result.details = recalculate_publication_job(int(job["id"]))
    return result


def _report_path(job_id: int, import_id: str, kind: str, content: bytes) -> str:
    suffix = ".csv" if b";" in content[:4096] or b"," in content[:4096] else ".bin"
    filename=f"mirakl_{clean_text(kind)}_{clean_text(import_id)}{suffix}"
    path = _artifact_dir(job_id) / filename
    path.write_bytes(content)
    stored=put_durable_bytes(
        namespace="publication_reports",identity=f"job_{int(job_id)}",
        filename=filename,content=content,
        content_type="text/csv" if suffix==".csv" else "application/octet-stream",
    )
    return f"storage://{stored['storage_key']}"


def _refresh_mirakl_imports(
    job: Mapping[str, Any],
    items: list[Mapping[str, Any]],
    client: MiraklCatalogClient,
    *,
    import_type: str,
) -> PublicationResult:
    is_product = import_type == "MIRAKL_PRODUCT_P41"
    result = PublicationResult(
        job_id=int(job["id"]),
        action="REFRESH_PRODUCTS" if is_product else "REFRESH_OFFERS",
    )
    imports = marketplace_imports_for_job(int(job["id"]), import_type=import_type, limit=1000)
    items_by_import: dict[str, list[Mapping[str, Any]]] = {}
    for item in items:
        token = clean_text(item.get("import_id"))
        expected_action = "POLL_PRODUCT_IMPORT" if is_product else "POLL_OFFER_IMPORT"
        if token and clean_text(item.get("next_action")).upper() == expected_action:
            items_by_import.setdefault(token, []).append(item)
    for import_row in imports:
        import_id = clean_text(import_row.get("external_import_id"))
        affected = items_by_import.get(import_id, [])
        if not affected:
            continue
        result.processed += len(affected)
        try:
            response = (
                client.product_import_status(import_id)
                if is_product else client.offer_import_status(import_id)
            )
            interpreted = interpret_mirakl_import_status(response)
        except Exception as exc:
            for item in affected:
                _record_error(
                    job, item, exc,
                    action="POLL_PRODUCT_IMPORT" if is_product else "POLL_OFFER_IMPORT",
                )
            result.failed += len(affected)
            continue
        update_marketplace_import(
            int(import_row["id"]), status=interpreted["status"],
            response_data=response if isinstance(response, Mapping) else {"response": response},
        )
        if not interpreted["done"]:
            for item in affected:
                update_publication_item(
                    int(item["id"]),
                    status="PRODUCT_PROCESSING" if is_product else "OFFER_PROCESSING",
                    product_status=interpreted["status"] if is_product else None,
                    offer_status=interpreted["status"] if not is_product else None,
                    runtime={
                        "next_action": "POLL_PRODUCT_IMPORT" if is_product else "POLL_OFFER_IMPORT",
                        "last_response_json": response,
                    },
                )
            result.pending += len(affected)
            continue

        error_identifiers: set[str] = set()
        success_identifiers: set[str] = set()
        report_paths: dict[str, str] = {}
        if is_product:
            for report_key in ("error", "integration", "transformation_error"):
                try:
                    content = client.product_import_report(import_id, report_key)
                except MiraklCatalogError as exc:
                    if exc.status_code not in {400, 404}:
                        report_paths[f"{report_key}_error"] = str(exc)
                    continue
                if not content:
                    continue
                report_paths[report_key] = _report_path(int(job["id"]), import_id, report_key, content)
                identifiers = _parse_report_identifiers(content)
                if report_key in {"error", "transformation_error"}:
                    error_identifiers |= identifiers
                elif report_key == "integration":
                    success_identifiers |= identifiers
        else:
            try:
                content = client.offer_import_error_report(import_id)
            except MiraklCatalogError as exc:
                if exc.status_code not in {400, 404}:
                    report_paths["error_report_error"] = str(exc)
            else:
                if content:
                    report_paths["error"] = _report_path(int(job["id"]), import_id, "offer_error", content)
                    error_identifiers |= _parse_report_identifiers(content)
        update_marketplace_import(
            int(import_row["id"]),
            report_paths=report_paths,
            has_error_report=bool(error_identifiers or (interpreted.get("error_count") or 0) > 0),
            has_success_report=bool(success_identifiers),
            error="" if not interpreted["failed"] else interpreted["status"],
        )

        error_count = interpreted.get("error_count") or 0
        for item in affected:
            sku = clean_text(item.get("seller_sku") or item.get("supplier_sku"))
            ean = clean_text(item.get("ean"))
            explicitly_failed = bool({sku, ean} & error_identifiers)
            explicitly_success = bool({sku, ean} & success_identifiers)
            ambiguous_failure = bool((interpreted["failed"] or error_count > 0) and not error_identifiers and not success_identifiers)
            if explicitly_failed or ambiguous_failure:
                update_publication_item(
                    int(item["id"]),
                    status="PRODUCT_REJECTED" if is_product else "OFFER_REJECTED",
                    product_status="REJECTED" if is_product else None,
                    offer_status="REJECTED" if not is_product else None,
                    last_error=f"Import Mirakl {import_id}: {interpreted['status']}",
                    runtime={"next_action": "REVIEW", "retryable": False, "last_response_json": response},
                )
                result.failed += 1
            else:
                if is_product:
                    update_publication_item(
                        int(item["id"]), status="OFFER_PENDING", product_status="ACCEPTED",
                        offer_status="PENDING", last_error="",
                        runtime={"next_action": "CREATE_OFFER", "last_response_json": response},
                    )
                else:
                    update_publication_item(
                        int(item["id"]), status="COMPLETED", offer_status="ACTIVE", last_error="",
                        runtime={"next_action": "NONE", "completed_at": db.now_iso(), "last_response_json": response},
                    )
                result.succeeded += 1
            record_publication_event(
                job_id=int(job["id"]), item_id=int(item["id"]), product_id=int(item["canonical_product_id"]),
                event_type="PRODUCT_IMPORT_STATUS" if is_product else "OFFER_IMPORT_STATUS",
                status=interpreted["status"], message=f"Import Mirakl {import_id}",
                details={"interpreted": interpreted, "reports": report_paths, "explicit_success": explicitly_success},
            )
    result.details = recalculate_publication_job(int(job["id"]))
    return result


def refresh_products(
    job_id: int,
    *,
    item_ids: Iterable[int] | None = None,
) -> PublicationResult:
    job, items = _job_items(job_id, item_ids)
    account = load_marketplace_account(int(job["marketplace_account_id"]), seller_id=int(job["seller_id"]))
    marketplace = clean_text(job.get("marketplace")).lower()
    if marketplace == "kaufland":
        return _refresh_kaufland_products(job, items, _kaufland_client(account, clean_text(job.get("environment"))))
    if marketplace == "worten":
        return _refresh_mirakl_imports(job, items, _mirakl_client(account), import_type="MIRAKL_PRODUCT_P41")
    raise ValueError(f"Marketplace non supportato: {marketplace}")


def _merge_kaufland_offer(
    item: Mapping[str, Any],
    config: Mapping[str, Any],
    storefront: str,
) -> dict[str, Any]:
    payload = dict(_json(item.get("offer_payload_json"), {}))
    payload["storefront"] = storefront
    if clean_text(config.get("id_warehouse")):
        payload["id_warehouse"] = clean_text(config.get("id_warehouse"))
    if clean_text(config.get("id_shipping_group")):
        payload["id_shipping_group"] = clean_text(config.get("id_shipping_group"))
    if config.get("handling_time") not in (None, ""):
        payload["handling_time"] = max(0, int(config.get("handling_time")))
    if clean_text(config.get("vat_indicator")):
        payload["vat_indicator"] = clean_text(config.get("vat_indicator"))
    if config.get("manufacturer_guarantee_years") not in (None, "", 0, "0"):
        payload["manufacturer_guarantee_years"] = max(1, min(99, int(config.get("manufacturer_guarantee_years"))))
    minimum_price = config.get("minimum_price")
    if minimum_price not in (None, "", 0, "0") and "minimum_price" not in payload:
        payload["minimum_price"] = int(round(float(minimum_price) * 100))
    required = {
        "ean": payload.get("ean"),
        "id_offer": payload.get("id_offer"),
        "listing_price": payload.get("listing_price"),
        "amount": payload.get("amount"),
        "id_shipping_group": payload.get("id_shipping_group"),
    }
    missing = [key for key, value in required.items() if value in (None, "")]
    if missing:
        raise ValueError("Offerta Kaufland incompleta: " + ", ".join(missing))
    if int(payload["listing_price"]) <= 0:
        raise ValueError("Il prezzo di vendita Kaufland deve essere maggiore di zero.")
    return payload


def _submit_kaufland_offers(
    job: Mapping[str, Any],
    items: list[Mapping[str, Any]],
    client: KauflandClient,
    config: Mapping[str, Any],
) -> PublicationResult:
    result = PublicationResult(job_id=int(job["id"]), action="SUBMIT_OFFERS")
    storefront = clean_text(job.get("storefront")).lower()
    for item in items:
        if clean_text(item.get("next_action")).upper() not in OFFER_ACTIONS:
            result.skipped += 1
            continue
        result.processed += 1
        try:
            payload = _merge_kaufland_offer(item, config, storefront)
            action = clean_text(item.get("next_action")).upper()
            existing_unit_id = clean_text(item.get("external_offer_id"))
            if action == "UPDATE_OFFER" and existing_unit_id:
                response = client.patch_unit(existing_unit_id, payload, storefront)
            else:
                response = client.upsert(payload, storefront)
            units = client.units(clean_text(payload.get("id_offer")), storefront)
            exact = [unit for unit in units if clean_text(unit.get("id_offer")) == clean_text(payload.get("id_offer"))]
            if not exact:
                raise KauflandError("Offerta inviata ma non verificabile tramite id_offer.")
            external_offer_id = _external_offer_id(exact[0])
        except Exception as exc:
            _record_error(job, item, exc, action=clean_text(item.get("next_action")).upper())
            result.failed += 1
            continue
        update_publication_item(
            int(item["id"]), status="COMPLETED", offer_status="ACTIVE",
            external_offer_id=external_offer_id, attempt_delta=1, last_error="",
            runtime={
                "next_action": "NONE", "remote_offer_exists": True,
                "completed_at": db.now_iso(),
                "last_response_json": {
                    "submit": response if isinstance(response, Mapping) else {"response": response},
                    "verified_unit": exact[0],
                },
            },
        )
        upsert_product_channel_state(
            seller_id=int(job["seller_id"]), account_id=int(job["marketplace_account_id"]),
            marketplace="kaufland", environment=clean_text(job.get("environment")) or "live",
            storefront=storefront, locale=clean_text(job.get("locale")),
            canonical_product_id=int(item["canonical_product_id"]), ean=clean_text(item.get("ean")),
            seller_sku=clean_text(payload.get("id_offer")),
            external_product_id=clean_text(item.get("external_product_id")),
            external_offer_id=external_offer_id, product_status="ACCEPTED", offer_status="ACTIVE",
            product_payload_hash=clean_text(item.get("payload_hash")),
            offer_payload_hash=json_hash(payload),
        )
        record_publication_event(
            job_id=int(job["id"]), item_id=int(item["id"]), product_id=int(item["canonical_product_id"]),
            event_type="OFFER_ACTIVE", status="COMPLETED", message="Offerta Kaufland attiva.",
            details={"id_unit": external_offer_id, "id_offer": payload.get("id_offer")},
        )
        result.succeeded += 1
    result.details = recalculate_publication_job(int(job["id"]))
    return result


def _submit_mirakl_offers(
    job: Mapping[str, Any],
    items: list[Mapping[str, Any]],
    client: MiraklCatalogClient,
    config: Mapping[str, Any],
) -> PublicationResult:
    selected = [item for item in items if clean_text(item.get("next_action")).upper() in OFFER_ACTIONS]
    result = PublicationResult(job_id=int(job["id"]), action="SUBMIT_OFFERS", processed=len(selected))
    if not selected:
        result.skipped = len(items)
        return result
    csv_bytes = build_mirakl_offer_csv(selected, settings=config)
    artifact = _save_artifact(
        job_id=int(job["id"]), artifact_type="MIRAKL_OFFER_CSV",
        filename=f"mirakl_offers_job_{int(job['id'])}.csv", content=csv_bytes,
        row_count=len(selected), metadata={"import_mode": clean_text(config.get("offer_import_mode")) or "NORMAL"},
    )
    try:
        response = client.import_offers(
            csv_bytes, filename=artifact["filename"],
            import_mode=clean_text(config.get("offer_import_mode")) or "NORMAL",
            operator_format=bool(config.get("offer_operator_format", True)),
            with_products=False,
        )
        import_id = extract_import_id(response)
        if not import_id:
            raise MiraklCatalogError(f"OF01 non ha restituito un import_id: {response}")
    except Exception as exc:
        for item in selected:
            _record_error(job, item, exc, action=clean_text(item.get("next_action")).upper())
        result.failed = len(selected)
        result.details = recalculate_publication_job(int(job["id"]))
        return result
    import_row_id = create_marketplace_import(
        job_id=int(job["id"]), account_id=int(job["marketplace_account_id"]),
        import_type="MIRAKL_OFFER_OF01", external_import_id=import_id, status="PROCESSING",
        request_data={"artifact": artifact, "item_ids": [int(item["id"]) for item in selected]},
        response_data=response if isinstance(response, Mapping) else {"response": response},
    )
    save_publication_artifact(
        job_id=int(job["id"]), artifact_type="MIRAKL_OFFER_CSV",
        filename=artifact["filename"], local_path=artifact["path"], content_hash=artifact["content_hash"],
        row_count=len(selected), metadata={"import_id": import_id}, marketplace_import_id=import_row_id,
    )
    for item in selected:
        update_publication_item(
            int(item["id"]), status="OFFER_PROCESSING", offer_status="SUBMITTED",
            import_id=import_id, attempt_delta=1, last_error="",
            runtime={
                "next_action": "POLL_OFFER_IMPORT", "submitted_at": db.now_iso(),
                "last_response_json": response if isinstance(response, Mapping) else {"response": response},
            },
        )
        record_publication_event(
            job_id=int(job["id"]), item_id=int(item["id"]), product_id=int(item["canonical_product_id"]),
            event_type="OFFER_IMPORT_SUBMITTED", status="PROCESSING",
            message=f"Import offerte Mirakl {import_id} inviato.", details={"import_id": import_id},
        )
    result.pending = len(selected)
    result.details = {"import_id": import_id, "artifact": artifact, **recalculate_publication_job(int(job["id"]))}
    return result


def submit_offers(
    job_id: int,
    *,
    item_ids: Iterable[int] | None = None,
    settings: Mapping[str, Any] | None = None,
    max_items: int = 100,
) -> PublicationResult:
    job, items = _job_items(job_id, item_ids)
    config = _settings(job, settings)
    _require_remote_write(config)
    selected = [item for item in items if clean_text(item.get("next_action")).upper() in OFFER_ACTIONS]
    selected = selected[: max(1, int(max_items))]
    account = load_marketplace_account(int(job["marketplace_account_id"]), seller_id=int(job["seller_id"]))
    marketplace = clean_text(job.get("marketplace")).lower()
    if marketplace == "kaufland":
        return _submit_kaufland_offers(
            job, selected,
            _kaufland_client(account, clean_text(job.get("environment"))), config,
        )
    if marketplace == "worten":
        return _submit_mirakl_offers(job, selected, _mirakl_client(account), config)
    raise ValueError(f"Marketplace non supportato: {marketplace}")


def refresh_offers(
    job_id: int,
    *,
    item_ids: Iterable[int] | None = None,
) -> PublicationResult:
    job, items = _job_items(job_id, item_ids)
    marketplace = clean_text(job.get("marketplace")).lower()
    if marketplace == "kaufland":
        # Kaufland POST /units is synchronous and verified immediately.
        return PublicationResult(
            job_id=int(job_id), action="REFRESH_OFFERS", skipped=len(items),
            details=recalculate_publication_job(int(job_id)),
        )
    account = load_marketplace_account(int(job["marketplace_account_id"]), seller_id=int(job["seller_id"]))
    return _refresh_mirakl_imports(
        job, items, _mirakl_client(account), import_type="MIRAKL_OFFER_OF01"
    )


def run_publication_cycle(
    job_id: int,
    *,
    mode: str = "SIMULATION",
    item_ids: Iterable[int] | None = None,
    settings: Mapping[str, Any] | None = None,
    max_items: int = 100,
    allow_review_items: bool = False,
) -> dict[str, Any]:
    """Advance a persistent job by one safe state-machine cycle.

    Simulation performs read-only duplicate checks and writes local artifacts.
    REAL may write to the selected marketplace and must be explicitly selected
    by the caller/UI.
    """
    selected_mode = clean_text(mode).upper() or "SIMULATION"
    if selected_mode not in {"SIMULATION", "REAL"}:
        raise ValueError("Modalità non valida: usa SIMULATION o REAL.")
    job, items = _job_items(job_id, item_ids)
    outputs: dict[str, Any] = {"job_id": int(job_id), "mode": selected_mode, "steps": []}
    if any(clean_text(item.get("duplicate_check_status")).upper() != "COMPLETED" for item in items):
        outputs["steps"].append(
            plan_publication_job(
                job_id, item_ids=item_ids, settings=settings,
                allow_review_items=allow_review_items,
            ).as_dict()
        )
        job, items = _job_items(job_id, item_ids)
    if selected_mode == "SIMULATION":
        outputs["steps"].append(simulate_publication_job(job_id, item_ids=item_ids).as_dict())
        outputs["summary"] = recalculate_publication_job(job_id)
        return outputs

    config = _settings(job, settings)
    _require_remote_write(config)
    if any(clean_text(item.get("next_action")).upper() in PRODUCT_ACTIONS for item in items):
        outputs["steps"].append(
            submit_products(job_id, item_ids=item_ids, settings=settings, max_items=max_items).as_dict()
        )
    job, items = _job_items(job_id, item_ids)
    if any(clean_text(item.get("next_action")).upper() in POLL_PRODUCT_ACTIONS for item in items):
        outputs["steps"].append(refresh_products(job_id, item_ids=item_ids).as_dict())
    job, items = _job_items(job_id, item_ids)
    if any(clean_text(item.get("next_action")).upper() in OFFER_ACTIONS for item in items):
        outputs["steps"].append(
            submit_offers(job_id, item_ids=item_ids, settings=settings, max_items=max_items).as_dict()
        )
    job, items = _job_items(job_id, item_ids)
    if any(clean_text(item.get("next_action")).upper() in POLL_OFFER_ACTIONS for item in items):
        outputs["steps"].append(refresh_offers(job_id, item_ids=item_ids).as_dict())
    outputs["summary"] = recalculate_publication_job(job_id)
    return outputs



def publication_configuration_options(job_id: int) -> dict[str, Any]:
    """Load the marketplace-specific offer configuration on explicit request."""
    job = publication_job(job_id)
    if not job:
        raise ValueError("Job di pubblicazione non trovato.")
    account = load_marketplace_account(
        int(job["marketplace_account_id"]), seller_id=int(job["seller_id"])
    )
    marketplace = clean_text(job.get("marketplace")).lower()
    if marketplace == "kaufland":
        client = _kaufland_client(account, clean_text(job.get("environment")))
        storefront = clean_text(job.get("storefront")).lower()
        return {
            "marketplace": marketplace,
            "warehouses": client.warehouses(storefront),
            "shipping_groups": client.shipping_groups(storefront),
            "vat_indicators": client.vat_indicators(storefront),
        }
    if marketplace == "worten":
        client = _mirakl_client(account)
        return {
            "marketplace": marketplace,
            "offer_states": client.offer_states(),
            "logistic_classes": client.logistic_classes(),
        }
    raise ValueError(f"Marketplace non supportato: {marketplace}")


def retry_failed_items(job_id: int, *, item_ids: Iterable[int] | None = None) -> int:
    job, items = _job_items(job_id, item_ids)
    count = 0
    for item in items:
        if not bool(item.get("retryable")):
            continue
        next_action = clean_text(item.get("next_action")).upper()
        if not next_action or next_action in {"NONE", "REVIEW"}:
            continue
        update_publication_item(
            int(item["id"]), status="RUNNING", last_error="",
            runtime={"retryable": False},
        )
        record_publication_event(
            job_id=int(job["id"]), item_id=int(item["id"]), product_id=int(item["canonical_product_id"]),
            event_type="RETRY_SCHEDULED", status="RUNNING", message=f"Nuovo tentativo: {next_action}",
        )
        count += 1
    recalculate_publication_job(job_id)
    return count


__all__ = [
    "PublicationResult",
    "build_mirakl_offer_csv",
    "build_mirakl_product_csv",
    "extract_import_id",
    "interpret_mirakl_import_status",
    "plan_publication_job",
    "publication_configuration_options",
    "refresh_offers",
    "refresh_products",
    "retry_failed_items",
    "run_publication_cycle",
    "simulate_publication_job",
    "submit_offers",
    "submit_products",
]
