from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(slots=True)
class Capability:
    key: str
    supported: bool
    message: str = ""
    status_code: int | None = None
    details: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class TaxonomyCategory:
    external_id: str
    parent_external_id: str = ""
    code: str = ""
    label: str = ""
    path: str = ""
    level: int = 0
    is_leaf: bool = False
    product_type: str = ""
    required_attributes: list[str] = field(default_factory=list)
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class TaxonomyAttribute:
    external_id: str
    category_external_id: str = ""
    code: str = ""
    label: str = ""
    data_type: str = "TEXT"
    requirement_level: str = "OPTIONAL"
    required: bool = False
    multiple: bool = False
    variant: bool = False
    unit: str = ""
    locale: str = ""
    value_list_code: str = ""
    constraints: dict[str, Any] = field(default_factory=dict)
    values: list[dict[str, Any]] = field(default_factory=list)
    conditions: list[dict[str, Any]] = field(default_factory=list)
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class TaxonomyBundle:
    marketplace: str
    scope_key: str
    storefront: str = ""
    locale: str = ""
    categories: list[TaxonomyCategory] = field(default_factory=list)
    attributes: list[TaxonomyAttribute] = field(default_factory=list)
    locales: list[dict[str, Any]] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class Evidence:
    canonical_field: str
    source_field: str
    source_value: Any
    source_path: str = ""
    source_file: str = ""
    source_row: int = 0
    source_hash: str = ""


@dataclass(slots=True)
class CanonicalProduct:
    source_row_key: str
    source_row_number: int
    ean: str = ""
    supplier_sku: str = ""
    brand: str = ""
    model: str = ""
    title: str = ""
    description: str = ""
    normalized: dict[str, Any] = field(default_factory=dict)
    raw: dict[str, Any] = field(default_factory=dict)
    evidence: list[Evidence] = field(default_factory=list)
    completeness_score: float = 0.0
    content_hash: str = ""

@dataclass(slots=True)
class CategoryCandidate:
    category_external_id: str
    category_label: str
    category_path: str
    score: float
    source: str = "LOCAL_TAXONOMY"
    signals: dict[str, Any] = field(default_factory=dict)
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class ValidationIssue:
    severity: str
    code: str
    message: str
    field_name: str = ""
    details: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class ProductFeedPreparation:
    marketplace: str
    category_external_id: str
    product_payload: dict[str, Any]
    offer_payload: dict[str, Any] = field(default_factory=dict)
    mapped_attributes: dict[str, Any] = field(default_factory=dict)
    missing_fields: list[str] = field(default_factory=list)
    issues: list[ValidationIssue] = field(default_factory=list)
    validation_status: str = "BLOCKED"
    readiness_score: float = 0.0
    payload_hash: str = ""


__all__ = [
    "CanonicalProduct",
    "CategoryCandidate",
    "Capability",
    "Evidence",
    "ProductFeedPreparation",
    "TaxonomyAttribute",
    "TaxonomyBundle",
    "TaxonomyCategory",
    "ValidationIssue",
]
