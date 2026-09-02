from __future__ import annotations

import re

from services.shared_cache import cache_invalidate

# Only namespaces with explicit cached read paths are listed here. Generic writes
# can keep using services.db.execute without knowing anything about the cache.
_TABLE_NAMESPACES: dict[str, tuple[str, ...]] = {
    "app_users": ("users",),
    "sellers": ("sellers", "accessible_lists"),
    "suppliers": ("accessible_lists",),
    "price_lists": ("accessible_lists",),
    "price_list_access": ("accessible_lists",),
    "catalog_tenant_access": ("accessible_lists",),
    "agency_clients": ("accessible_lists",),
    "tenant_sellers": ("accessible_lists",),
    "marketplace_accounts": ("marketplace_accounts",),
}

_MUTATION = re.compile(r"^\s*(INSERT|UPDATE|DELETE|REPLACE|ALTER|DROP|CREATE)\b", re.I)


def invalidate_for_sql(sql: str) -> set[str]:
    text = str(sql or "")
    if not _MUTATION.search(text):
        return set()
    lowered = text.lower()
    invalidated: set[str] = set()
    for table, namespaces in _TABLE_NAMESPACES.items():
        if re.search(rf"\b{re.escape(table.lower())}\b", lowered):
            for namespace in namespaces:
                if namespace not in invalidated:
                    cache_invalidate(namespace)
                    invalidated.add(namespace)
    return invalidated
