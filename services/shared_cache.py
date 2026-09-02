from __future__ import annotations

import json
import os
import threading
import time
from collections import OrderedDict
from typing import Any, Callable, Hashable


class _LocalTTLCache:
    def __init__(self, *, ttl_seconds: float, max_entries: int) -> None:
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

    def invalidate(self, predicate: Callable[[Hashable], bool] | None = None) -> int:
        with self._lock:
            keys = list(self._items) if predicate is None else [key for key in self._items if predicate(key)]
            for key in keys:
                self._items.pop(key, None)
            return len(keys)

    def stats(self) -> dict[str, int]:
        with self._lock:
            return {"hits": self._hits, "misses": self._misses, "entries": len(self._items)}

_MISSING = object()
_LOCK = threading.RLock()
_BACKEND = None


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return float(default)


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return int(default)


def _namespace() -> str:
    value = str(os.getenv("MARKETPLACE_HUB_CACHE_NAMESPACE") or "marketplacehub:v307").strip()
    return value.strip(":") or "marketplacehub:v307"


def _redis_url() -> str:
    return str(
        os.getenv("MARKETPLACE_HUB_REDIS_URL")
        or os.getenv("REDIS_URL")
        or ""
    ).strip()


def _key(namespace: str, key: Hashable) -> str:
    if isinstance(key, (dict, list, tuple, set)):
        try:
            rendered = json.dumps(key, sort_keys=True, separators=(",", ":"), default=str)
        except TypeError:
            rendered = repr(key)
    else:
        rendered = str(key)
    return f"{str(namespace).strip(':')}:{rendered}"


class LocalSharedCache:
    kind = "local"

    def __init__(self) -> None:
        self.cache = _LocalTTLCache(
            ttl_seconds=_env_float("MARKETPLACE_HUB_CACHE_TTL_SECONDS", 20.0),
            max_entries=_env_int("MARKETPLACE_HUB_CACHE_MAX_ENTRIES", 2048),
        )

    def get(self, namespace: str, key: Hashable, default: Any = None) -> Any:
        return self.cache.get(_key(namespace, key), default)

    def set(self, namespace: str, key: Hashable, value: Any, *, ttl_seconds: float | None = None) -> Any:
        return self.cache.set(_key(namespace, key), value, ttl_seconds=ttl_seconds)

    def delete(self, namespace: str, key: Hashable) -> int:
        wanted = _key(namespace, key)
        return self.cache.invalidate(lambda current: current == wanted)

    def invalidate(self, namespace: str) -> int:
        prefix = f"{str(namespace).strip(':')}:"
        return self.cache.invalidate(lambda current: str(current).startswith(prefix))

    def get_or_set(
        self,
        namespace: str,
        key: Hashable,
        factory: Callable[[], Any],
        *,
        ttl_seconds: float | None = None,
    ) -> Any:
        marker = object()
        value = self.get(namespace, key, marker)
        if value is not marker:
            return value
        return self.set(namespace, key, factory(), ttl_seconds=ttl_seconds)

    def info(self) -> dict[str, Any]:
        stats = self.cache.stats()
        return {"backend": self.kind, **stats, "connected": True}


class RedisSharedCache:
    kind = "redis"

    def __init__(self, url: str) -> None:
        import redis

        self.client = redis.Redis.from_url(
            url,
            decode_responses=True,
            socket_connect_timeout=1.5,
            socket_timeout=1.5,
            health_check_interval=30,
        )
        self.prefix = _namespace()
        self.client.ping()

    def _redis_key(self, namespace: str, key: Hashable) -> str:
        return f"{self.prefix}:{_key(namespace, key)}"

    @staticmethod
    def _encode(value: Any) -> str:
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)

    @staticmethod
    def _decode(value: str | None, default: Any) -> Any:
        if value is None:
            return default
        try:
            return json.loads(value)
        except (TypeError, ValueError, json.JSONDecodeError):
            return default

    def get(self, namespace: str, key: Hashable, default: Any = None) -> Any:
        try:
            return self._decode(self.client.get(self._redis_key(namespace, key)), default)
        except Exception:
            return default

    def set(self, namespace: str, key: Hashable, value: Any, *, ttl_seconds: float | None = None) -> Any:
        ttl = ttl_seconds
        if ttl is None:
            ttl = _env_float("MARKETPLACE_HUB_CACHE_TTL_SECONDS", 20.0)
        seconds = max(1, int(round(float(ttl))))
        self.client.set(self._redis_key(namespace, key), self._encode(value), ex=seconds)
        return value

    def delete(self, namespace: str, key: Hashable) -> int:
        try:
            return int(self.client.delete(self._redis_key(namespace, key)) or 0)
        except Exception:
            return 0

    def invalidate(self, namespace: str) -> int:
        pattern = f"{self.prefix}:{str(namespace).strip(':')}:*"
        deleted = 0
        batch: list[str] = []
        try:
            for item in self.client.scan_iter(match=pattern, count=250):
                batch.append(item)
                if len(batch) >= 250:
                    deleted += int(self.client.delete(*batch) or 0)
                    batch.clear()
            if batch:
                deleted += int(self.client.delete(*batch) or 0)
        except Exception:
            return deleted
        return deleted

    def get_or_set(
        self,
        namespace: str,
        key: Hashable,
        factory: Callable[[], Any],
        *,
        ttl_seconds: float | None = None,
    ) -> Any:
        value = self.get(namespace, key, _MISSING)
        if value is not _MISSING:
            return value
        value = factory()
        return self.set(namespace, key, value, ttl_seconds=ttl_seconds)

    def info(self) -> dict[str, Any]:
        try:
            self.client.ping()
            return {"backend": self.kind, "connected": True, "namespace": self.prefix}
        except Exception as exc:
            return {"backend": self.kind, "connected": False, "namespace": self.prefix, "error": str(exc)}


def cache_backend():
    """Return Redis when configured/reachable, otherwise a process-local cache.

    Production SaaS can set MARKETPLACE_HUB_REDIS_URL/REDIS_URL. Local development
    and the current Streamlit runtime keep working without Redis.
    """
    global _BACKEND
    if _BACKEND is not None:
        return _BACKEND
    with _LOCK:
        if _BACKEND is not None:
            return _BACKEND
        url = _redis_url()
        if url:
            try:
                _BACKEND = RedisSharedCache(url)
                return _BACKEND
            except Exception:
                # Cache is an optimization; never make the business app unavailable.
                pass
        _BACKEND = LocalSharedCache()
        return _BACKEND


def cache_get(namespace: str, key: Hashable, default: Any = None) -> Any:
    return cache_backend().get(namespace, key, default)


def cache_set(namespace: str, key: Hashable, value: Any, *, ttl_seconds: float | None = None) -> Any:
    return cache_backend().set(namespace, key, value, ttl_seconds=ttl_seconds)


def cache_get_or_set(
    namespace: str,
    key: Hashable,
    factory: Callable[[], Any],
    *,
    ttl_seconds: float | None = None,
) -> Any:
    return cache_backend().get_or_set(namespace, key, factory, ttl_seconds=ttl_seconds)


def cache_delete(namespace: str, key: Hashable) -> int:
    return cache_backend().delete(namespace, key)


def cache_invalidate(namespace: str) -> int:
    return cache_backend().invalidate(namespace)


def cache_info() -> dict[str, Any]:
    return cache_backend().info()


def _reset_cache_for_tests() -> None:
    global _BACKEND
    with _LOCK:
        _BACKEND = None
