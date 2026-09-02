from __future__ import annotations

import os

from services import shared_cache
from services.cache_invalidation import invalidate_for_sql


def _local_backend():
    os.environ.pop("MARKETPLACE_HUB_REDIS_URL", None)
    os.environ.pop("REDIS_URL", None)
    shared_cache._reset_cache_for_tests()
    return shared_cache.cache_backend()


def test_local_shared_cache_get_set_and_namespace_invalidate():
    backend = _local_backend()
    assert backend.kind == "local"
    shared_cache.cache_set("alpha", "one", {"value": 1}, ttl_seconds=60)
    shared_cache.cache_set("alpha", "two", {"value": 2}, ttl_seconds=60)
    shared_cache.cache_set("beta", "one", 3, ttl_seconds=60)
    assert shared_cache.cache_get("alpha", "one") == {"value": 1}
    assert shared_cache.cache_invalidate("alpha") == 2
    assert shared_cache.cache_get("alpha", "one") is None
    assert shared_cache.cache_get("beta", "one") == 3


def test_sql_mutation_invalidation_targets_only_known_namespaces():
    _local_backend()
    shared_cache.cache_set("users", "id:7", {"id": 7}, ttl_seconds=60)
    shared_cache.cache_set("sellers", "x", [1], ttl_seconds=60)
    invalidated = invalidate_for_sql("UPDATE app_users SET active=0 WHERE id=?")
    assert invalidated == {"users"}
    assert shared_cache.cache_get("users", "id:7") is None
    assert shared_cache.cache_get("sellers", "x") == [1]


def test_price_list_write_invalidates_accessible_lists():
    _local_backend()
    shared_cache.cache_set("accessible_lists", 3, [{"id": 22}], ttl_seconds=60)
    invalidated = invalidate_for_sql("UPDATE price_lists SET active=0 WHERE id=?")
    assert "accessible_lists" in invalidated
    assert shared_cache.cache_get("accessible_lists", 3) is None
