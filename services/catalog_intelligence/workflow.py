from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from typing import Any

from services.catalog_intelligence.accounts import load_marketplace_account
from services.catalog_intelligence.category_classifier import (
    candidates_as_dicts,
    category_decision,
    kaufland_decide_payload,
    merge_candidates,
    parse_kaufland_suggestions,
    rank_categories,
    source_category_signature,
)
from services.catalog_intelligence.category_schema import ensure_category_attributes
from services.catalog_intelligence.ai_enrichment import (
    ai_attribute_mappings_for_product,
    finish_ai_run,
    map_attributes_with_ai,
    resolve_category_with_ai,
    start_ai_run,
)
from services.catalog_intelligence.feed import prepare_product_feed
from services.catalog_intelligence.models import ProductFeedPreparation, ValidationIssue
from services.catalog_intelligence.repository import (
    category_assignments_for_source,
    category_candidates_for_run,
    complete_category_classification,
    find_category_mapping_rule,
    record_validation_run,
    save_category_candidates,
    save_feed_preparation,
    source_snapshot,
    start_category_classification,
    taxonomy_categories,
    taxonomy_category,
    upsert_category_assignment,
)
from services.catalog_intelligence.utils import clean_text, json_hash, load_json
from services.db import row, rows
from services.kaufland import KauflandClient

Progress = Callable[[int, int, str], None]


def _validate_work_scope(
    *,
    seller_id: int,
    account_id: int,
    taxonomy_snapshot: Mapping[str, Any],
    source_snapshot_id: int,
) -> dict[str, Any]:
    """Fail closed when a job mixes Seller/account snapshots.

    Catalog Intelligence is multi-tenant.  A numeric snapshot id supplied by a
    stale Streamlit state must never make products from one Seller visible to
    another Seller or publish them with another account's taxonomy.
    """
    taxonomy_seller = int(taxonomy_snapshot.get("seller_id") or 0)
    taxonomy_account = int(taxonomy_snapshot.get("marketplace_account_id") or 0)
    if taxonomy_seller != int(seller_id) or taxonomy_account != int(account_id):
        raise ValueError("La tassonomia selezionata non appartiene al Seller/account attivo.")
    source = source_snapshot(int(source_snapshot_id))
    if not source or int(source.get("seller_id") or 0) != int(seller_id):
        raise ValueError("Il catalogo sorgente non appartiene al Seller attivo.")
    return source


def _kaufland_client(
    *, seller_id: int, account_id: int, environment: str
) -> KauflandClient:
    account = load_marketplace_account(account_id, seller_id=seller_id)
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


def classify_source_products(
    *,
    seller_id: int,
    account_id: int,
    taxonomy_snapshot: Mapping[str, Any],
    source_snapshot_id: int,
    environment: str = "live",
    use_official_kaufland_suggestions: bool = True,
    limit: int = 1000,
    progress: Progress | None = None,
) -> dict[str, Any]:
    """Classify new products against a cached official taxonomy.

    Categories are read exclusively from ``taxonomy_categories``.  For
    Kaufland, the documented ``/categories/decide`` service is enabled by
    default and is the primary ranking signal.  Every returned category is
    still validated against the locally versioned leaf-category snapshot.
    """
    _validate_work_scope(
        seller_id=seller_id,
        account_id=account_id,
        taxonomy_snapshot=taxonomy_snapshot,
        source_snapshot_id=source_snapshot_id,
    )
    marketplace = clean_text(taxonomy_snapshot.get("marketplace")).lower()
    storefront = clean_text(taxonomy_snapshot.get("storefront")).lower()
    locale = clean_text(taxonomy_snapshot.get("locale"))
    taxonomy_snapshot_id = int(taxonomy_snapshot["id"])
    categories = taxonomy_categories(
        taxonomy_snapshot_id,
        leaf_only=True,
        limit=50000,
    )
    if not categories:
        raise ValueError("Lo snapshot non contiene categorie foglia pubblicabili.")
    categories_by_id = {clean_text(item.get("external_id")): item for item in categories}
    products = rows(
        """
        SELECT * FROM canonical_products
        WHERE source_snapshot_id=?
        ORDER BY source_row_number,id LIMIT ?
        """,
        (int(source_snapshot_id), max(1, int(limit))),
    )
    if not products:
        raise ValueError("Lo snapshot sorgente non contiene prodotti normalizzati.")

    run_id = start_category_classification(
        seller_id=seller_id,
        account_id=account_id,
        taxonomy_snapshot_id=taxonomy_snapshot_id,
        source_snapshot_id=source_snapshot_id,
        marketplace=marketplace,
        environment=environment,
        storefront=storefront,
        locale=locale,
        product_count=len(products),
        settings={
            "taxonomy_source": "DATABASE_SNAPSHOT",
            "official_kaufland_suggestions": bool(use_official_kaufland_suggestions),
            "remote_write_enabled": False,
        },
    )
    client: KauflandClient | None = None
    official_client_error = ""
    if marketplace == "kaufland" and use_official_kaufland_suggestions:
        try:
            client = _kaufland_client(
                seller_id=seller_id,
                account_id=account_id,
                environment=environment,
            )
        except Exception as exc:
            # A credentials/network issue must not convert all products into
            # BLOCKED records.  Keep the cached taxonomy and send the products
            # to REVIEW so the Seller can continue working.
            official_client_error = str(exc)[:1000]

    classified_count = review_count = blocked_count = 0
    summaries: list[dict[str, Any]] = []
    try:
        for position, product in enumerate(products, start=1):
            signature, source_label = source_category_signature(product)
            mapping_rule = None
            if signature:
                mapping_rule = find_category_mapping_rule(
                    seller_id=seller_id,
                    supplier_id=int(product.get("supplier_id") or 0) or None,
                    marketplace=marketplace,
                    storefront=storefront,
                    source_signature=signature,
                )
                if mapping_rule:
                    mapped_category = categories_by_id.get(
                        clean_text(mapping_rule.get("category_external_id"))
                    )
                    if mapped_category:
                        mapping_rule = {
                            **mapping_rule,
                            "category_label": clean_text(mapped_category.get("label")),
                            "category_path": clean_text(mapped_category.get("path")),
                        }
                    else:
                        mapping_rule = None

            local = rank_categories(product, categories, top_k=8)
            official = []
            official_error = official_client_error
            if client is not None and not mapping_rule:
                try:
                    official_payload = client.decide_category(
                        kaufland_decide_payload(product),
                        storefront,
                        locale=locale,
                    )
                    official = parse_kaufland_suggestions(official_payload, categories_by_id)
                except Exception as exc:
                    # A temporary suggestion failure must not discard the local
                    # classification or the previously cached taxonomy.
                    official_error = str(exc)[:1000]

            merged = merge_candidates(
                local,
                official,
                mapping_rule=mapping_rule,
                top_k=5,
            )
            decision = category_decision(merged)
            candidate = decision.get("candidate")
            candidate_dicts = candidates_as_dicts(merged)
            save_category_candidates(
                run_id=run_id,
                product_id=int(product["id"]),
                candidates=candidate_dicts,
            )
            if candidate is None:
                status = clean_text(decision.get("status") or "REVIEW").upper()
                review_count += 1
                assignment_id = upsert_category_assignment(
                    seller_id=seller_id,
                    account_id=account_id,
                    taxonomy_snapshot_id=taxonomy_snapshot_id,
                    source_snapshot_id=source_snapshot_id,
                    classification_run_id=run_id,
                    product_id=int(product["id"]),
                    marketplace=marketplace,
                    storefront=storefront,
                    category_external_id="",
                    category_label="",
                    category_path="",
                    decision_source=clean_text(decision.get("decision_source") or "NO_MATCH_REVIEW"),
                    confidence=0.0,
                    status=status,
                    classification_signature=signature,
                    evidence={
                        "source_category_label": source_label,
                        "official_error": official_error,
                        "review_reason": decision.get("review_reason"),
                    },
                )
            else:
                status = clean_text(decision.get("status")).upper()
                if status == "AUTO_APPROVED":
                    classified_count += 1
                else:
                    # Confidence alone must never generate a technical block.
                    # A weak or local-only category remains in REVIEW.
                    status = "REVIEW"
                    review_count += 1
                assignment_id = upsert_category_assignment(
                    seller_id=seller_id,
                    account_id=account_id,
                    taxonomy_snapshot_id=taxonomy_snapshot_id,
                    source_snapshot_id=source_snapshot_id,
                    classification_run_id=run_id,
                    product_id=int(product["id"]),
                    marketplace=marketplace,
                    storefront=storefront,
                    category_external_id=candidate.category_external_id,
                    category_label=candidate.category_label,
                    category_path=candidate.category_path,
                    decision_source=clean_text(decision.get("decision_source")),
                    confidence=float(decision.get("confidence") or 0.0),
                    status=status,
                    classification_signature=signature,
                    evidence={
                        "source_category_label": source_label,
                        "margin": decision.get("margin"),
                        "candidates": candidate_dicts,
                        "official_error": official_error,
                        "review_reason": decision.get("review_reason"),
                        "official_rank": decision.get("official_rank"),
                    },
                )
            summaries.append(
                {
                    "assignment_id": assignment_id,
                    "product_id": int(product["id"]),
                    "ean": clean_text(product.get("ean")),
                    "sku": clean_text(product.get("supplier_sku")),
                    "title": clean_text(product.get("title")),
                    "category_external_id": candidate.category_external_id if candidate else "",
                    "category_label": candidate.category_label if candidate else "",
                    "category_path": candidate.category_path if candidate else "",
                    "confidence": float(decision.get("confidence") or 0.0),
                    "margin": float(decision.get("margin") or 0.0),
                    "status": status,
                    "source": clean_text(decision.get("decision_source")),
                    "official_error": official_error,
                    "review_reason": clean_text(decision.get("review_reason")),
                    "official_rank": decision.get("official_rank"),
                }
            )
            if progress:
                progress(position, len(products), clean_text(product.get("title")))
        complete_category_classification(
            run_id,
            status="COMPLETED",
            classified_count=classified_count,
            review_count=review_count,
            blocked_count=blocked_count,
        )
    except Exception as exc:
        complete_category_classification(
            run_id,
            status="FAILED",
            classified_count=classified_count,
            review_count=review_count,
            blocked_count=blocked_count,
            error=str(exc),
        )
        raise
    return {
        "run_id": run_id,
        "product_count": len(products),
        "auto_approved": classified_count,
        "review": review_count,
        "blocked": blocked_count,
        "summaries": summaries,
    }



def resolve_review_categories_with_ai(
    *,
    seller_id: int,
    account_id: int,
    taxonomy_snapshot: Mapping[str, Any],
    source_snapshot_id: int,
    ai_profile_id: int,
    minimum_confidence: float = 0.86,
    limit: int = 100,
    progress: Progress | None = None,
) -> dict[str, Any]:
    """Resolve deterministic REVIEW assignments with a constrained AI choice.

    The provider receives only the candidates already produced from the official
    taxonomy snapshot. A category outside that allow-list is rejected and the
    product remains in REVIEW.
    """
    _validate_work_scope(
        seller_id=seller_id,
        account_id=account_id,
        taxonomy_snapshot=taxonomy_snapshot,
        source_snapshot_id=source_snapshot_id,
    )
    assignments = category_assignments_for_source(
        seller_id=seller_id,
        account_id=account_id,
        source_snapshot_id=source_snapshot_id,
        statuses=("REVIEW",),
        limit=max(1, int(limit)),
    )
    if not assignments:
        return {"run_id": None, "product_count": 0, "auto_approved": 0, "review": 0, "failed": 0}
    run_id = start_ai_run(
        seller_id=seller_id,
        account_id=account_id,
        taxonomy_snapshot_id=int(taxonomy_snapshot["id"]),
        source_snapshot_id=source_snapshot_id,
        profile_id=ai_profile_id,
        purpose="CATEGORY_RESOLUTION",
        product_count=len(assignments),
        settings={
            "minimum_confidence": float(minimum_confidence),
            "candidate_policy": "OFFICIAL_CANDIDATES_ONLY",
        },
    )
    auto_approved = review_count = failed_count = 0
    summaries: list[dict[str, Any]] = []
    try:
        for position, assignment in enumerate(assignments, start=1):
            product_id = int(assignment["canonical_product_id"])
            classification_run_id = int(assignment.get("classification_run_id") or 0)
            candidates = (
                category_candidates_for_run(classification_run_id, product_id=product_id)
                if classification_run_id else []
            )
            try:
                decision = resolve_category_with_ai(
                    run_id=run_id,
                    seller_id=seller_id,
                    account_id=account_id,
                    profile_id=ai_profile_id,
                    product={**assignment, "id": product_id},
                    candidates=candidates,
                    minimum_confidence=float(minimum_confidence),
                )
            except Exception as exc:
                failed_count += 1
                summaries.append({"product_id": product_id, "status": "ERROR", "error": str(exc)})
                if progress:
                    progress(position, len(assignments), clean_text(assignment.get("title")))
                continue
            chosen_id = clean_text(decision.get("category_external_id"))
            status = clean_text(decision.get("status") or "REVIEW").upper()
            if chosen_id:
                old_evidence = load_json(assignment.get("evidence_json"), {})
                if not isinstance(old_evidence, Mapping):
                    old_evidence = {}
                upsert_category_assignment(
                    seller_id=seller_id,
                    account_id=account_id,
                    taxonomy_snapshot_id=int(taxonomy_snapshot["id"]),
                    source_snapshot_id=source_snapshot_id,
                    classification_run_id=classification_run_id or None,
                    product_id=product_id,
                    marketplace=clean_text(taxonomy_snapshot.get("marketplace")),
                    storefront=clean_text(taxonomy_snapshot.get("storefront")),
                    category_external_id=chosen_id,
                    category_label=clean_text(decision.get("category_label")),
                    category_path=clean_text(decision.get("category_path")),
                    decision_source="AI_CONSTRAINED",
                    confidence=float(decision.get("confidence") or 0.0),
                    status=status,
                    classification_signature=clean_text(assignment.get("classification_signature")),
                    evidence={
                        **dict(old_evidence),
                        "ai": {
                            "evidence_paths": list(decision.get("evidence_paths") or []),
                            "reason": clean_text(decision.get("reason")),
                            "provider": clean_text(decision.get("provider")),
                            "model": clean_text(decision.get("model")),
                        },
                    },
                )
            if status == "AUTO_APPROVED":
                auto_approved += 1
            else:
                review_count += 1
            summaries.append({"product_id": product_id, **decision})
            if progress:
                progress(position, len(assignments), clean_text(assignment.get("title")))
        finish_ai_run(
            run_id,
            status="COMPLETED",
            success_count=auto_approved,
            review_count=review_count,
            failed_count=failed_count,
        )
    except Exception as exc:
        finish_ai_run(
            run_id,
            status="FAILED",
            success_count=auto_approved,
            review_count=review_count,
            failed_count=failed_count,
            error=str(exc),
        )
        raise
    return {
        "run_id": run_id,
        "product_count": len(assignments),
        "auto_approved": auto_approved,
        "review": review_count,
        "failed": failed_count,
        "summaries": summaries,
    }

def approve_category_assignment(
    *,
    seller_id: int,
    account_id: int,
    taxonomy_snapshot: Mapping[str, Any],
    source_snapshot_id: int,
    product_id: int,
    category_external_id: str,
    approved_by: str = "seller",
    create_mapping_rule: bool = True,
) -> int:
    _validate_work_scope(
        seller_id=seller_id,
        account_id=account_id,
        taxonomy_snapshot=taxonomy_snapshot,
        source_snapshot_id=source_snapshot_id,
    )
    category = taxonomy_category(int(taxonomy_snapshot["id"]), category_external_id)
    if not category or not bool(category.get("is_leaf")):
        raise ValueError("La categoria scelta non è una categoria foglia valida nello snapshot attivo.")
    product = row(
        """
        SELECT * FROM canonical_products
        WHERE id=? AND source_snapshot_id=? AND seller_id=?
        """,
        (int(product_id), int(source_snapshot_id), int(seller_id)),
    )
    if not product:
        raise ValueError("Prodotto non trovato nello snapshot sorgente.")
    signature, source_label = source_category_signature(product)
    assignment_id = upsert_category_assignment(
        seller_id=seller_id,
        account_id=account_id,
        taxonomy_snapshot_id=int(taxonomy_snapshot["id"]),
        source_snapshot_id=source_snapshot_id,
        classification_run_id=None,
        product_id=product_id,
        marketplace=clean_text(taxonomy_snapshot.get("marketplace")),
        storefront=clean_text(taxonomy_snapshot.get("storefront")),
        category_external_id=clean_text(category.get("external_id")),
        category_label=clean_text(category.get("label")),
        category_path=clean_text(category.get("path")),
        decision_source="MANUAL_APPROVAL",
        confidence=100.0,
        status="APPROVED",
        classification_signature=signature,
        evidence={"manual": True, "source_category_label": source_label},
        approved_by=approved_by,
    )
    if create_mapping_rule and signature:
        from services.catalog_intelligence.repository import upsert_category_mapping_rule

        upsert_category_mapping_rule(
            seller_id=seller_id,
            supplier_id=int(product.get("supplier_id") or 0) or None,
            marketplace=clean_text(taxonomy_snapshot.get("marketplace")),
            storefront=clean_text(taxonomy_snapshot.get("storefront")),
            source_signature=signature,
            source_label=source_label,
            category_external_id=clean_text(category.get("external_id")),
            category_label=clean_text(category.get("label")),
            confidence=1.0,
            status="APPROVED",
        )
    return assignment_id


def prepare_assigned_product_feeds(
    *,
    seller_id: int,
    account_id: int,
    taxonomy_snapshot: Mapping[str, Any],
    source_snapshot_id: int,
    environment: str = "live",
    include_review: bool = False,
    limit: int = 1000,
    progress: Progress | None = None,
    ai_profile_id: int | None = None,
    ai_minimum_confidence: float = 0.72,
) -> dict[str, Any]:
    _validate_work_scope(
        seller_id=seller_id,
        account_id=account_id,
        taxonomy_snapshot=taxonomy_snapshot,
        source_snapshot_id=source_snapshot_id,
    )
    statuses = ("AUTO_APPROVED", "APPROVED") + (("REVIEW",) if include_review else ())
    assignments = category_assignments_for_source(
        seller_id=seller_id,
        account_id=account_id,
        source_snapshot_id=source_snapshot_id,
        statuses=statuses,
        limit=limit,
    )
    if not assignments:
        raise ValueError("Nessun prodotto con categoria approvata disponibile per preparare il feed.")
    marketplace = clean_text(taxonomy_snapshot.get("marketplace")).lower()
    storefront = clean_text(taxonomy_snapshot.get("storefront")).lower()
    locale = clean_text(taxonomy_snapshot.get("locale"))
    schema_cache: dict[str, list[Any]] = {}
    preparations: list[dict[str, Any]] = []
    issues_by_product: dict[int, list[ValidationIssue]] = {}
    ai_run_id: int | None = None
    ai_mapped_products = ai_review_products = ai_failed_products = 0

    for position, assignment in enumerate(assignments, start=1):
        product_id = int(assignment["canonical_product_id"])
        category_id = clean_text(assignment.get("category_external_id"))
        category = taxonomy_category(int(taxonomy_snapshot["id"]), category_id)
        if not category:
            preparation = ProductFeedPreparation(
                marketplace=marketplace,
                category_external_id=category_id,
                product_payload={},
                offer_payload={},
                missing_fields=["category"],
                issues=[
                    ValidationIssue(
                        "ERROR",
                        "CATEGORY_NOT_IN_SNAPSHOT",
                        "La categoria assegnata non esiste più nello snapshot attivo.",
                        "category",
                        {"category_external_id": category_id},
                    )
                ],
                validation_status="BLOCKED",
                readiness_score=0.0,
                payload_hash=json_hash({"product_id": product_id, "category": category_id, "status": "BLOCKED"}),
            )
        else:
            if category_id not in schema_cache:
                try:
                    schema_result = ensure_category_attributes(
                        seller_id=seller_id,
                        account_id=account_id,
                        snapshot=taxonomy_snapshot,
                        category_external_id=category_id,
                        environment=environment,
                    )
                    schema_cache[category_id] = list(schema_result["attributes"])
                except Exception as exc:
                    schema_cache[category_id] = []
                    category = None
                    preparation = ProductFeedPreparation(
                        marketplace=marketplace,
                        category_external_id=category_id,
                        product_payload={},
                        offer_payload={},
                        missing_fields=["category_schema"],
                        issues=[
                            ValidationIssue(
                                "ERROR",
                                "CATEGORY_SCHEMA_LOAD_FAILED",
                                f"Impossibile caricare gli attributi ufficiali della categoria: {exc}",
                                "category",
                                {"category_external_id": category_id},
                            )
                        ],
                        validation_status="BLOCKED",
                        readiness_score=0.0,
                        payload_hash=json_hash({"product_id": product_id, "category": category_id, "error": str(exc)}),
                    )
            if category is not None:
                persisted_ai = ai_attribute_mappings_for_product(
                    seller_id=seller_id,
                    account_id=account_id,
                    source_snapshot_id=source_snapshot_id,
                    product_id=product_id,
                    category_external_id=category_id,
                )
                supplemental_values = {key: item.get("value") for key, item in persisted_ai.items()}
                preparation = prepare_product_feed(
                    assignment,
                    marketplace=marketplace,
                    category=category,
                    attributes=schema_cache[category_id],
                    storefront=storefront,
                    locale=locale,
                    supplemental_values=supplemental_values,
                    supplemental_mapping_records=persisted_ai,
                )
                missing_required = [
                    clean_text(issue.field_name)
                    for issue in preparation.issues
                    if clean_text(issue.code) == "MISSING_REQUIRED_ATTRIBUTE" and clean_text(issue.field_name)
                ]
                if ai_profile_id and missing_required:
                    if ai_run_id is None:
                        ai_run_id = start_ai_run(
                            seller_id=seller_id,
                            account_id=account_id,
                            taxonomy_snapshot_id=int(taxonomy_snapshot["id"]),
                            source_snapshot_id=source_snapshot_id,
                            profile_id=int(ai_profile_id),
                            purpose="ATTRIBUTE_MAPPING",
                            product_count=len(assignments),
                            settings={
                                "minimum_confidence": float(ai_minimum_confidence),
                                "policy": "SOURCE_EVIDENCE_REQUIRED",
                            },
                        )
                    try:
                        new_ai = map_attributes_with_ai(
                            run_id=ai_run_id,
                            seller_id=seller_id,
                            account_id=account_id,
                            taxonomy_snapshot_id=int(taxonomy_snapshot["id"]),
                            source_snapshot_id=source_snapshot_id,
                            profile_id=int(ai_profile_id),
                            product={**assignment, "id": product_id},
                            category_external_id=category_id,
                            attributes=schema_cache[category_id],
                            missing_attribute_ids=missing_required,
                            minimum_confidence=float(ai_minimum_confidence),
                        )
                    except Exception:
                        ai_failed_products += 1
                    else:
                        if new_ai:
                            ai_mapped_products += 1
                            persisted_ai.update(new_ai)
                            supplemental_values = {key: item.get("value") for key, item in persisted_ai.items()}
                            preparation = prepare_product_feed(
                                assignment,
                                marketplace=marketplace,
                                category=category,
                                attributes=schema_cache[category_id],
                                storefront=storefront,
                                locale=locale,
                                supplemental_values=supplemental_values,
                                supplemental_mapping_records=persisted_ai,
                            )
                        else:
                            ai_review_products += 1

        preparation_id = save_feed_preparation(
            seller_id=seller_id,
            account_id=account_id,
            taxonomy_snapshot_id=int(taxonomy_snapshot["id"]),
            source_snapshot_id=source_snapshot_id,
            product_id=product_id,
            category_external_id=category_id,
            marketplace=marketplace,
            storefront=storefront,
            locale=locale,
            product_payload=preparation.product_payload,
            offer_payload=preparation.offer_payload,
            mapped_attributes=preparation.mapped_attributes,
            missing_fields=preparation.missing_fields,
            issues=preparation.issues,
            validation_status=preparation.validation_status,
            readiness_score=preparation.readiness_score,
            payload_hash=preparation.payload_hash,
        )
        issues_by_product[product_id] = list(preparation.issues)
        preparations.append(
            {
                "preparation_id": preparation_id,
                "canonical_product_id": product_id,
                "ean": clean_text(assignment.get("ean")),
                "supplier_sku": clean_text(assignment.get("supplier_sku")),
                "title": clean_text(assignment.get("title")),
                "category_external_id": category_id,
                "category_label": clean_text(assignment.get("category_label")),
                "validation_status": preparation.validation_status,
                "readiness_score": preparation.readiness_score,
                "missing_fields": preparation.missing_fields,
                "issues": preparation.issues,
                "payload_hash": preparation.payload_hash,
                "product_payload": preparation.product_payload,
                "offer_payload": preparation.offer_payload,
                "mapped_attributes": preparation.mapped_attributes,
            }
        )
        if progress:
            progress(position, len(assignments), clean_text(assignment.get("title")))

    if ai_run_id is not None:
        finish_ai_run(
            ai_run_id,
            status="COMPLETED",
            success_count=ai_mapped_products,
            review_count=ai_review_products,
            failed_count=ai_failed_products,
        )

    validation_run_id = record_validation_run(
        seller_id=seller_id,
        account_id=account_id,
        taxonomy_snapshot_id=int(taxonomy_snapshot["id"]),
        job_id=None,
        issues_by_product=issues_by_product,
    )
    return {
        "validation_run_id": validation_run_id,
        "product_count": len(preparations),
        "ready": sum(1 for item in preparations if item["validation_status"] == "READY"),
        "warnings": sum(1 for item in preparations if item["validation_status"] == "VALID_WITH_WARNINGS"),
        "blocked": sum(1 for item in preparations if item["validation_status"] == "BLOCKED"),
        "ai_run_id": ai_run_id,
        "ai_mapped_products": ai_mapped_products,
        "ai_review_products": ai_review_products,
        "ai_failed_products": ai_failed_products,
        "preparations": preparations,
    }


__all__ = [
    "approve_category_assignment",
    "classify_source_products",
    "resolve_review_categories_with_ai",
    "prepare_assigned_product_feeds",
]
