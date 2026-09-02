from __future__ import annotations

import threading
import time
from collections import OrderedDict
from dataclasses import dataclass
from typing import Any, Callable, Hashable


@dataclass(frozen=True)
class CacheStats:
    hits: int
    misses: int
    entries: int


class TTLCache:
    """Small process-local cache used by Core services.

    The interface is intentionally Redis-ready: callers depend on get/set/invalidate
    semantics rather than Streamlit cache decorators. A Redis implementation can
    replace this backend later without touching business services.
    """

    def __init__(self, *, ttl_seconds: float = 10.0, max_entries: int = 256):
        self.ttl_seconds = max(0.0, float(ttl_seconds))
        self.max_entries = max(1, int(max_entries))
        self._items: OrderedDict[Hashable, tuple[float, Any]] = OrderedDict()
        self._lock = threading.RLock()
        self._hits = 0
        self._misses = 0

    def get(self, key: Hashable, default: Any = None) -> Any:
        now = time.monotonic()
        with self._lock:
            item = self._items.get(key)
            if item is None:
                self._misses += 1
                return default
            expires_at, value = item
            if expires_at < now:
                self._items.pop(key, None)
                self._misses += 1
                return default
            self._items.move_to_end(key)
            self._hits += 1
            return value

    def set(self, key: Hashable, value: Any, *, ttl_seconds: float | None = None) -> Any:
        ttl = self.ttl_seconds if ttl_seconds is None else max(0.0, float(ttl_seconds))
        with self._lock:
            self._items[key] = (time.monotonic() + ttl, value)
            self._items.move_to_end(key)
            while len(self._items) > self.max_entries:
                self._items.popitem(last=False)
        return value

    def get_or_set(self, key: Hashable, factory: Callable[[], Any], *, ttl_seconds: float | None = None) -> Any:
        marker = object()
        value = self.get(key, marker)
        if value is not marker:
            return value
        value = factory()
        return self.set(key, value, ttl_seconds=ttl_seconds)

    def invalidate(self, predicate: Callable[[Hashable], bool] | None = None) -> int:
        with self._lock:
            keys = list(self._items) if predicate is None else [key for key in self._items if predicate(key)]
            for key in keys:
                self._items.pop(key, None)
            return len(keys)

    def stats(self) -> CacheStats:
        with self._lock:
            return CacheStats(self._hits, self._misses, len(self._items))
