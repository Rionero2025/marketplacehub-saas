from __future__ import annotations

"""Constrained AI assistance for Catalog Intelligence.

The marketplace taxonomy and deterministic validator remain authoritative.  The
AI layer can only choose among real taxonomy candidates or map a real source
fact to an official marketplace attribute.  Every accepted mapping keeps an
explicit evidence path back to the supplier record and is revalidated by the
normal deterministic feed validator before publication.
"""

import json
from collections.abc import Iterable, Mapping
from typing import Any

from services.ai_providers import complete_json, get_profile
from services.catalog_intelligence.models import TaxonomyAttribute
from services.catalog_intelligence.schema import ensure_schema
from services.catalog_intelligence.utils import clean_text, json_hash, load_json, slug
from services.db import execute, json_text, now_iso, row, rows

AI_CATALOG_ENGINE_VERSION = 258
_EMPTY = (None, "", [], {})


CATEGORY_RESPONSE_SCHEMA: dict[str, Any] = {
    "name": "catalog_category_resolution",
    "schema": {
        "type": "object",
        "properties": {
            "category_external_id": {"type": "string"},
            "confidence": {"type": "number", "minimum": 0, "maximum": 1},
            "evidence_paths": {"type": "array", "items": {"type": "string"}},
            "reason": {"type": "string"},
        },
        "required": ["category_external_id", "confidence", "evidence_paths", "reason"],
        "additionalProperties": False,
    },
    "strict": True,
}

ATTRIBUTE_RESPONSE_SCHEMA: dict[str, Any] = {
    "name": "catalog_attribute_mapping",
    "schema": {
        "type": "object",
        "properties": {
            "mappings": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "attribute_external_id": {"type": "string"},
                        "value": {
                            "anyOf": [
                                {"type": "string"},
                                {"type": "number"},
                                {"type": "boolean"},
                                {"type": "array", "items": {"type": "string"}},
                            ]
                        },
                        "evidence_path": {"type": "string"},
                        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
                        "reason": {"type": "string"},
                    },
                    "required": [
                        "attribute_external_id", "value", "evidence_path", "confidence", "reason"
                    ],
                    "additionalProperties": False,
                },
            }
        },
        "required": ["mappings"],
        "additionalProperties": False,
    },
    "strict": True,
}


def _safe_scalar(value: Any, *, limit: int = 700) -> Any:
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    text = clean_text(value)
    if len(text) > limit:
        return text[:limit] + "…"
    return text


def _flatten(value: Any, prefix: str, out: dict[str, Any], *, depth: int = 0, max_items: int = 180) -> None:
    if len(out) >= max_items or depth > 4:
        return
    if isinstance(value, Mapping):
        for key, item in value.items():
            if len(out) >= max_items:
                break
            token = clean_text(key)
            if not token:
                continue
            path = f"{prefix}.{token}" if prefix else token
            _flatten(item, path, out, depth=depth + 1, max_items=max_items)
        return
    if isinstance(value, (list, tuple, set)):
        # Keep short lists readable, but do not create hundreds of prompt fields.
        compact = [_safe_scalar(item, limit=250) for item in list(value)[:20] if item not in _EMPTY]
        if compact:
            out[prefix] = compact
        return
    if value not in _EMPTY and prefix:
        out[prefix] = _safe_scalar(value)


def product_evidence(product: Mapping[str, Any], *, max_items: int = 180) -> dict[str, Any]:
    """Return bounded, addressable supplier facts that AI is allowed to cite."""
    normalized = load_json(product.get("normalized_json"), {})
    raw = load_json(product.get("raw_json"), {})
    if not isinstance(normalized, Mapping):
        normalized = {}
    if not isinstance(raw, Mapping):
        raw = {}
    out: dict[str, Any] = {}
    core = {
        "ean": product.get("ean") or normalized.get("ean"),
        "supplier_sku": product.get("supplier_sku") or normalized.get("supplier_sku"),
        "brand": product.get("brand") or normalized.get("brand"),
        "model": product.get("model") or normalized.get("model"),
        "title": product.get("title") or normalized.get("title"),
        "description": product.get("description") or normalized.get("description"),
        "short_description": normalized.get("short_description"),
    }
    _flatten(core, "product", out, max_items=max_items)
    source = normalized.get("source_attributes")
    if not isinstance(source, Mapping):
        source = raw
    _flatten(source, "source", out, max_items=max_items)
    # Explicit normalized technical fields can be useful when supplier naming is obscure.
    technical = {
        key: value
        for key, value in normalized.items()
        if key not in {"source_attributes", "images", "documents"} and value not in _EMPTY
    }
    _flatten(technical, "normalized", out, max_items=max_items)
    return dict(list(out.items())[:max_items])


def _evidence_valid(paths: Iterable[Any], evidence: Mapping[str, Any]) -> list[str]:
    valid: list[str] = []
    for value in paths:
        path = clean_text(value)
        if path and path in evidence and path not in valid:
            valid.append(path)
    return valid


def _allowed_values(attribute: TaxonomyAttribute, *, limit: int = 120) -> tuple[list[dict[str, Any]], bool]:
    result: list[dict[str, Any]] = []
    for item in list(attribute.values or [])[:limit]:
        if isinstance(item, Mapping):
            result.append(
                {
                    key: _safe_scalar(item.get(key), limit=180)
                    for key in ("code", "id", "value", "key", "label", "name")
                    if item.get(key) not in _EMPTY
                }
            )
        else:
            result.append({"value": _safe_scalar(item, limit=180)})
    return result, len(attribute.values or []) > limit


def start_ai_run(
    *, seller_id: int, account_id: int, taxonomy_snapshot_id: int, source_snapshot_id: int,
    profile_id: int, purpose: str, product_count: int, settings: Mapping[str, Any] | None = None,
) -> int:
    ensure_schema()
    return execute(
        """
        INSERT INTO catalog_ai_runs(
            seller_id,marketplace_account_id,taxonomy_snapshot_id,source_snapshot_id,
            ai_profile_id,purpose,status,product_count,settings_json,started_at
        ) VALUES(?,?,?,?,?,?, 'RUNNING',?,?,?)
        """,
        (
            int(seller_id), int(account_id), int(taxonomy_snapshot_id), int(source_snapshot_id),
            int(profile_id), clean_text(purpose).upper(), max(0, int(product_count)),
            json_text(dict(settings or {})), now_iso(),
        ),
    )


def finish_ai_run(
    run_id: int, *, status: str = "COMPLETED", success_count: int = 0,
    review_count: int = 0, failed_count: int = 0, error: str = "",
) -> None:
    execute(
        """
        UPDATE catalog_ai_runs SET status=?,success_count=?,review_count=?,failed_count=?,
               error=?,completed_at=? WHERE id=?
        """,
        (
            clean_text(status).upper(), max(0, int(success_count)), max(0, int(review_count)),
            max(0, int(failed_count)), clean_text(error)[:4000], now_iso(), int(run_id),
        ),
    )


def record_ai_result(
    *, run_id: int, product_id: int, task_type: str, input_hash: str, status: str,
    provider: str = "", model: str = "", response: Mapping[str, Any] | None = None,
    accepted: Mapping[str, Any] | None = None, rejection_reason: str = "",
) -> int:
    return execute(
        """
        INSERT INTO catalog_ai_product_results(
            ai_run_id,canonical_product_id,task_type,input_hash,status,provider,model,
            response_json,accepted_json,rejection_reason,created_at
        ) VALUES(?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            int(run_id), int(product_id), clean_text(task_type).upper(), clean_text(input_hash),
            clean_text(status).upper(), clean_text(provider), clean_text(model),
            json_text(dict(response or {})), json_text(dict(accepted or {})),
            clean_text(rejection_reason)[:3000], now_iso(),
        ),
    )


def ai_runs(*, seller_id: int, account_id: int | None = None, limit: int = 100) -> list[dict]:
    ensure_schema()
    sql = "SELECT * FROM catalog_ai_runs WHERE seller_id=?"
    params: list[Any] = [int(seller_id)]
    if account_id is not None:
        sql += " AND marketplace_account_id=?"
        params.append(int(account_id))
    sql += " ORDER BY id DESC LIMIT ?"
    params.append(max(1, int(limit)))
    return rows(sql, params)


def ai_attribute_mappings_for_product(
    *, seller_id: int, account_id: int, source_snapshot_id: int, product_id: int,
    category_external_id: str, accepted_only: bool = True,
) -> dict[str, dict[str, Any]]:
    ensure_schema()
    sql = """
        SELECT * FROM product_ai_attribute_mappings
        WHERE seller_id=? AND marketplace_account_id=? AND source_snapshot_id=?
          AND canonical_product_id=? AND category_external_id=?
    """
    params: list[Any] = [
        int(seller_id), int(account_id), int(source_snapshot_id), int(product_id),
        clean_text(category_external_id),
    ]
    if accepted_only:
        sql += " AND status='ACCEPTED'"
    sql += " ORDER BY attribute_external_id,id DESC"
    result: dict[str, dict[str, Any]] = {}
    for item in rows(sql, params):
        external_id = clean_text(item.get("attribute_external_id"))
        if not external_id or external_id in result:
            continue
        result[external_id] = {
            "value": load_json(item.get("value_json"), None),
            "source": clean_text(item.get("evidence_path")),
            "source_kind": "AI_EVIDENCE",
            "confidence": float(item.get("confidence") or 0.0),
            "reason": clean_text(item.get("reason")),
            "ai_mapping_id": int(item.get("id") or 0),
        }
    return result


def _save_attribute_mapping(
    *, seller_id: int, account_id: int, taxonomy_snapshot_id: int, source_snapshot_id: int,
    product_id: int, category_external_id: str, attribute_external_id: str, value: Any,
    evidence_path: str, evidence_value: Any, confidence: float, reason: str,
    profile_id: int, ai_run_id: int, status: str,
) -> int:
    existing = row(
        """
        SELECT id FROM product_ai_attribute_mappings
        WHERE marketplace_account_id=? AND source_snapshot_id=? AND canonical_product_id=?
          AND category_external_id=? AND attribute_external_id=?
        """,
        (
            int(account_id), int(source_snapshot_id), int(product_id), clean_text(category_external_id),
            clean_text(attribute_external_id),
        ),
    )
    values = (
        json_text(value), clean_text(evidence_path), json_text(evidence_value),
        max(0.0, min(1.0, float(confidence))), clean_text(reason)[:1500], int(profile_id),
        int(ai_run_id), clean_text(status).upper(), now_iso(),
    )
    if existing:
        execute(
            """
            UPDATE product_ai_attribute_mappings SET value_json=?,evidence_path=?,evidence_value_json=?,
                   confidence=?,reason=?,ai_profile_id=?,ai_run_id=?,status=?,updated_at=? WHERE id=?
            """,
            (*values, int(existing["id"])),
        )
        return int(existing["id"])
    return execute(
        """
        INSERT INTO product_ai_attribute_mappings(
            seller_id,marketplace_account_id,taxonomy_snapshot_id,source_snapshot_id,
            canonical_product_id,category_external_id,attribute_external_id,value_json,
            evidence_path,evidence_value_json,confidence,reason,ai_profile_id,ai_run_id,status,
            created_at,updated_at
        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            int(seller_id), int(account_id), int(taxonomy_snapshot_id), int(source_snapshot_id),
            int(product_id), clean_text(category_external_id), clean_text(attribute_external_id),
            json_text(value), clean_text(evidence_path), json_text(evidence_value),
            max(0.0, min(1.0, float(confidence))), clean_text(reason)[:1500], int(profile_id),
            int(ai_run_id), clean_text(status).upper(), now_iso(), now_iso(),
        ),
    )


def _profile(profile_id: int, seller_id: int) -> dict[str, Any]:
    profile = get_profile(int(profile_id), int(seller_id))
    if not profile or not bool(profile.get("enabled", 1)):
        raise ValueError("Il profilo IA selezionato non esiste o non è attivo.")
    return profile


def resolve_category_with_ai(
    *, run_id: int, seller_id: int, account_id: int, profile_id: int,
    product: Mapping[str, Any], candidates: Iterable[Mapping[str, Any]],
    minimum_confidence: float = 0.86,
) -> dict[str, Any]:
    """Choose only among the supplied official candidate IDs."""
    candidate_rows = [dict(item) for item in candidates if clean_text(item.get("category_external_id"))]
    if not candidate_rows:
        return {"status": "REVIEW", "reason": "Nessuna categoria candidata disponibile."}
    evidence = product_evidence(product)
    allowed = {clean_text(item.get("category_external_id")): item for item in candidate_rows}
    prompt_data = {
        "product_facts": evidence,
        "allowed_categories": [
            {
                "category_external_id": clean_text(item.get("category_external_id")),
                "label": clean_text(item.get("category_label")),
                "path": clean_text(item.get("category_path")),
                "deterministic_score": float(item.get("score") or 0.0),
                "source": clean_text(item.get("candidate_source") or item.get("source")),
            }
            for item in candidate_rows[:8]
        ],
    }
    system = (
        "Sei il motore Catalog Intelligence. Devi scegliere ESCLUSIVAMENTE una categoria dall'elenco "
        "allowed_categories. Non puoi creare categorie, codici o fatti. Usa soltanto product_facts. "
        "evidence_paths deve contenere chiavi esistenti in product_facts che giustificano la scelta. "
        "Se le prove non sono sufficienti scegli comunque la candidata più prudente ma assegna confidence bassa."
    )
    prompt = "Classifica questo prodotto nel modo più preciso possibile:\n" + json.dumps(
        prompt_data, ensure_ascii=False, separators=(",", ":")
    )
    profile = _profile(profile_id, seller_id)
    input_hash = json_hash(prompt_data)
    try:
        parsed, used_profile, result = complete_json(
            [profile], system=system, prompt=prompt, purpose="catalog_category_mapping",
            account_id=account_id, json_schema=CATEGORY_RESPONSE_SCHEMA,
        )
    except Exception as exc:
        record_ai_result(
            run_id=run_id, product_id=int(product["id"]), task_type="CATEGORY", input_hash=input_hash,
            status="ERROR", rejection_reason=str(exc),
        )
        raise
    chosen_id = clean_text(parsed.get("category_external_id"))
    confidence = max(0.0, min(1.0, float(parsed.get("confidence") or 0.0)))
    valid_paths = _evidence_valid(parsed.get("evidence_paths") or [], evidence)
    rejection = ""
    if chosen_id not in allowed:
        rejection = "Il provider ha restituito una categoria fuori dall'elenco ufficiale consentito."
    elif not valid_paths:
        rejection = "Il provider non ha citato alcuna prova presente nel feed sorgente."
    accepted = {
        "category_external_id": chosen_id if chosen_id in allowed else "",
        "confidence": confidence,
        "evidence_paths": valid_paths,
        "reason": clean_text(parsed.get("reason")),
        "auto_approve": bool(chosen_id in allowed and valid_paths and confidence >= float(minimum_confidence)),
    }
    record_ai_result(
        run_id=run_id, product_id=int(product["id"]), task_type="CATEGORY", input_hash=input_hash,
        status="REJECTED" if rejection else "ACCEPTED",
        provider=clean_text(used_profile.get("provider")), model=clean_text(result.model),
        response=parsed, accepted=accepted if not rejection else {}, rejection_reason=rejection,
    )
    if rejection:
        return {"status": "REVIEW", "reason": rejection, "raw": parsed}
    selected = allowed[chosen_id]
    return {
        "status": "AUTO_APPROVED" if accepted["auto_approve"] else "REVIEW",
        "category_external_id": chosen_id,
        "category_label": clean_text(selected.get("category_label")),
        "category_path": clean_text(selected.get("category_path")),
        "confidence": confidence * 100.0,
        "evidence_paths": valid_paths,
        "reason": accepted["reason"],
        "provider": clean_text(used_profile.get("provider")),
        "model": clean_text(result.model),
    }


def map_attributes_with_ai(
    *, run_id: int, seller_id: int, account_id: int, taxonomy_snapshot_id: int,
    source_snapshot_id: int, profile_id: int, product: Mapping[str, Any],
    category_external_id: str, attributes: Iterable[TaxonomyAttribute],
    missing_attribute_ids: Iterable[str], minimum_confidence: float = 0.72,
) -> dict[str, dict[str, Any]]:
    """Map missing official attributes only when a cited supplier fact exists."""
    by_id = {clean_text(item.external_id): item for item in attributes if clean_text(item.external_id)}
    requested = [clean_text(item) for item in missing_attribute_ids if clean_text(item) in by_id]
    requested = list(dict.fromkeys(requested))[:30]
    if not requested:
        return {}
    evidence = product_evidence(product)
    targets: list[dict[str, Any]] = []
    for external_id in requested:
        attribute = by_id[external_id]
        allowed, truncated = _allowed_values(attribute)
        targets.append(
            {
                "attribute_external_id": external_id,
                "code": clean_text(attribute.code),
                "label": clean_text(attribute.label),
                "data_type": clean_text(attribute.data_type),
                "unit": clean_text(attribute.unit),
                "multiple": bool(attribute.multiple),
                "required": bool(attribute.required),
                "constraints": dict(attribute.constraints or {}),
                "allowed_values": allowed,
                "allowed_values_truncated": truncated,
            }
        )
    prompt_data = {"product_facts": evidence, "attributes_to_fill": targets}
    system = (
        "Sei il mapper attributi di Catalog Intelligence. Non devi inventare caratteristiche. "
        "Puoi restituire un mapping solo se il valore deriva da una chiave reale di product_facts e devi indicare "
        "quella chiave in evidence_path. Usa esclusivamente attribute_external_id presenti in attributes_to_fill. "
        "Se l'attributo ha allowed_values, restituisci un codice/valore ammesso quando la prova sorgente lo supporta. "
        "Puoi convertire unità o normalizzare formati, ma non aggiungere una caratteristica assente. "
        "Ometti dall'array gli attributi per cui non esiste una prova sufficiente."
    )
    prompt = "Mappa gli attributi mancanti:\n" + json.dumps(
        prompt_data, ensure_ascii=False, separators=(",", ":")
    )
    profile = _profile(profile_id, seller_id)
    input_hash = json_hash(prompt_data)
    try:
        parsed, used_profile, result = complete_json(
            [profile], system=system, prompt=prompt, purpose="catalog_attribute_mapping",
            account_id=account_id, json_schema=ATTRIBUTE_RESPONSE_SCHEMA,
        )
    except Exception as exc:
        record_ai_result(
            run_id=run_id, product_id=int(product["id"]), task_type="ATTRIBUTES", input_hash=input_hash,
            status="ERROR", rejection_reason=str(exc),
        )
        raise
    accepted: dict[str, dict[str, Any]] = {}
    rejected: list[dict[str, Any]] = []
    for mapping in list(parsed.get("mappings") or []):
        if not isinstance(mapping, Mapping):
            continue
        external_id = clean_text(mapping.get("attribute_external_id"))
        evidence_path = clean_text(mapping.get("evidence_path"))
        confidence = max(0.0, min(1.0, float(mapping.get("confidence") or 0.0)))
        reason = clean_text(mapping.get("reason"))
        value = mapping.get("value")
        rejection = ""
        if external_id not in requested:
            rejection = "Attributo non richiesto o non presente nello schema ufficiale."
        elif evidence_path not in evidence:
            rejection = "Percorso di prova inesistente nel feed sorgente."
        elif value in _EMPTY:
            rejection = "Valore vuoto."
        elif confidence < float(minimum_confidence):
            rejection = "Confidence inferiore alla soglia di applicazione."
        status = "REJECTED" if rejection else "ACCEPTED"
        if not rejection:
            accepted[external_id] = {
                "value": value,
                "source": evidence_path,
                "source_kind": "AI_EVIDENCE",
                "confidence": confidence,
                "reason": reason,
            }
        else:
            rejected.append({"attribute_external_id": external_id, "reason": rejection})
        if external_id in requested:
            _save_attribute_mapping(
                seller_id=seller_id, account_id=account_id,
                taxonomy_snapshot_id=taxonomy_snapshot_id, source_snapshot_id=source_snapshot_id,
                product_id=int(product["id"]), category_external_id=category_external_id,
                attribute_external_id=external_id, value=value, evidence_path=evidence_path,
                evidence_value=evidence.get(evidence_path), confidence=confidence, reason=reason or rejection,
                profile_id=profile_id, ai_run_id=run_id, status=status,
            )
    record_ai_result(
        run_id=run_id, product_id=int(product["id"]), task_type="ATTRIBUTES", input_hash=input_hash,
        status="ACCEPTED" if accepted else "REVIEW",
        provider=clean_text(used_profile.get("provider")), model=clean_text(result.model),
        response=parsed, accepted={"mappings": accepted, "rejected": rejected},
        rejection_reason="" if accepted else "Nessun mapping IA applicabile con prova sorgente valida.",
    )
    return accepted


__all__ = [
    "AI_CATALOG_ENGINE_VERSION",
    "ai_attribute_mappings_for_product",
    "ai_runs",
    "finish_ai_run",
    "map_attributes_with_ai",
    "product_evidence",
    "record_ai_result",
    "resolve_category_with_ai",
    "start_ai_run",
]
