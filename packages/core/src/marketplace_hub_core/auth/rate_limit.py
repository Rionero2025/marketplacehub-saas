from __future__ import annotations

from collections import defaultdict, deque
from datetime import datetime
from typing import Protocol

from redis import Redis


class LoginRateLimiter(Protocol):
    def is_blocked(self, key: str, *, now: datetime, limit: int, window_seconds: int) -> bool: ...

    def record_failure(self, key: str, *, now: datetime, window_seconds: int) -> None: ...

    def clear(self, key: str) -> None: ...


class MemoryLoginRateLimiter:
    def __init__(self) -> None:
        self.attempts: dict[str, deque[float]] = defaultdict(deque)

    def _active(self, key: str, *, now: datetime, window_seconds: int) -> deque[float]:
        attempts = self.attempts[key]
        cutoff = now.timestamp() - window_seconds
        while attempts and attempts[0] <= cutoff:
            attempts.popleft()
        return attempts

    def is_blocked(self, key: str, *, now: datetime, limit: int, window_seconds: int) -> bool:
        return len(self._active(key, now=now, window_seconds=window_seconds)) >= limit

    def record_failure(self, key: str, *, now: datetime, window_seconds: int) -> None:
        attempts = self._active(key, now=now, window_seconds=window_seconds)
        attempts.append(now.timestamp())

    def clear(self, key: str) -> None:
        self.attempts.pop(key, None)


class RedisLoginRateLimiter:
    def __init__(self, client: Redis) -> None:
        self.client = client

    def is_blocked(self, key: str, *, now: datetime, limit: int, window_seconds: int) -> bool:
        redis_key = f"auth:login:{key}"
        count = self.client.get(redis_key)
        return int(count or 0) >= limit

    def record_failure(self, key: str, *, now: datetime, window_seconds: int) -> None:
        redis_key = f"auth:login:{key}"
        self.client.eval(
            "local n=redis.call('INCR',KEYS[1]); "
            "if n==1 then redis.call('EXPIRE',KEYS[1],ARGV[1]) end; return n",
            1,
            redis_key,
            window_seconds,
        )

    def clear(self, key: str) -> None:
        self.client.delete(f"auth:login:{key}")
