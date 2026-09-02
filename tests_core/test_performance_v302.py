from marketplace_core.performance import TTLCache


def test_ttl_cache_hit_and_invalidate():
    cache = TTLCache(ttl_seconds=30, max_entries=4)
    cache.set(("seller", 3), {"name": "Fintrade"})
    assert cache.get(("seller", 3))["name"] == "Fintrade"
    assert cache.stats().hits == 1
    assert cache.invalidate(lambda key: key[0] == "seller") == 1
    assert cache.get(("seller", 3)) is None


def test_ttl_cache_get_or_set_runs_factory_once():
    cache = TTLCache(ttl_seconds=30)
    calls = []
    def factory():
        calls.append(1)
        return 42
    assert cache.get_or_set("answer", factory) == 42
    assert cache.get_or_set("answer", factory) == 42
    assert len(calls) == 1
