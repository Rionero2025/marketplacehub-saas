"""Catalog Intelligence foundation for Marketplace Hub.

The package deliberately keeps marketplace rules outside the AI layer.  The
marketplace adapters synchronize the official taxonomy, deterministic services
normalize and validate source data, and AI providers are only allowed to
propose mappings against that synchronized schema.
"""

from __future__ import annotations

CATALOG_INTELLIGENCE_VERSION = 271

from services.catalog_intelligence.schema import ensure_schema

__all__ = ["CATALOG_INTELLIGENCE_VERSION", "ensure_schema"]
