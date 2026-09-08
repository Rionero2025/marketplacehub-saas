from __future__ import annotations

import asyncio
from dataclasses import dataclass
from hashlib import sha256
from threading import Lock
from uuid import uuid4

from redis import Redis
from redis.exceptions import RedisError

MAX_CONCURRENT_TRACKING_OPERATIONS = 2
TRACKING_ADMISSION_TTL_MS = 5 * 60 * 1_000


class TrackingAdmissionBusyError(RuntimeError):
    pass


class TrackingAdmissionCapacityError(RuntimeError):
    pass


class TrackingAdmissionUnavailableError(RuntimeError):
    pass


@dataclass(frozen=True)
class TrackingAdmissionLease:
    token: str
    actor_key: str
    account_key: str


def _scope_keys(actor_id, seller_id, account_id, _environment):
    actor = sha256(f"actor:{actor_id}".encode()).hexdigest()
    account = sha256(f"account:{seller_id}:{account_id}".encode()).hexdigest()
    return actor, account


class MemoryTrackingAdmission:
    """Per-process fallback used when the API has no shared Redis connection."""

    def __init__(self, maximum=MAX_CONCURRENT_TRACKING_OPERATIONS):
        self.maximum = maximum
        self._mutex = Lock()
        self._actors = {}
        self._accounts = {}
        self._tokens = set()

    def acquire(self, actor_id, seller_id, account_id, environment):
        actor_key, account_key = _scope_keys(actor_id, seller_id, account_id, environment)
        with self._mutex:
            if actor_key in self._actors or account_key in self._accounts:
                raise TrackingAdmissionBusyError
            if len(self._tokens) >= self.maximum:
                raise TrackingAdmissionCapacityError
            token = str(uuid4())
            self._actors[actor_key] = token
            self._accounts[account_key] = token
            self._tokens.add(token)
        return TrackingAdmissionLease(token, actor_key, account_key)

    def release(self, lease):
        with self._mutex:
            if self._actors.get(lease.actor_key) == lease.token:
                self._actors.pop(lease.actor_key, None)
            if self._accounts.get(lease.account_key) == lease.token:
                self._accounts.pop(lease.account_key, None)
            self._tokens.discard(lease.token)


class RedisTrackingAdmission:
    """Atomic cross-process leases with automatic crash recovery."""

    _GLOBAL_KEY = "tracking:admission:{orders}:active"
    _ACQUIRE = """
local clock = redis.call('TIME')
local now = (clock[1] * 1000) + math.floor(clock[2] / 1000)
redis.call('ZREMRANGEBYSCORE', KEYS[1], '-inf', now)
if redis.call('EXISTS', KEYS[2]) == 1 or redis.call('EXISTS', KEYS[3]) == 1 then
  return 0
end
if redis.call('ZCARD', KEYS[1]) >= tonumber(ARGV[2]) then
  return -1
end
redis.call('SET', KEYS[2], ARGV[1], 'PX', ARGV[3])
redis.call('SET', KEYS[3], ARGV[1], 'PX', ARGV[3])
redis.call('ZADD', KEYS[1], now + tonumber(ARGV[3]), ARGV[1])
redis.call('PEXPIRE', KEYS[1], ARGV[3])
return 1
"""
    _RELEASE = """
if redis.call('GET', KEYS[2]) == ARGV[1] then redis.call('DEL', KEYS[2]) end
if redis.call('GET', KEYS[3]) == ARGV[1] then redis.call('DEL', KEYS[3]) end
redis.call('ZREM', KEYS[1], ARGV[1])
if redis.call('ZCARD', KEYS[1]) == 0 then redis.call('DEL', KEYS[1]) end
return 1
"""

    def __init__(
        self, client: Redis, maximum=MAX_CONCURRENT_TRACKING_OPERATIONS,
        ttl_ms=TRACKING_ADMISSION_TTL_MS,
    ):
        self.client = client
        self.maximum = maximum
        self.ttl_ms = ttl_ms

    @staticmethod
    def _redis_scope_key(kind, digest):
        return f"tracking:admission:{{orders}}:{kind}:{digest}"

    def acquire(self, actor_id, seller_id, account_id, environment):
        actor_key, account_key = _scope_keys(actor_id, seller_id, account_id, environment)
        lease = TrackingAdmissionLease(str(uuid4()), actor_key, account_key)
        keys = (
            self._GLOBAL_KEY,
            self._redis_scope_key("actor", actor_key),
            self._redis_scope_key("account", account_key),
        )
        try:
            outcome = int(self.client.eval(
                self._ACQUIRE, len(keys), *keys,
                lease.token, self.maximum, self.ttl_ms,
            ))
        except (RedisError, TypeError, ValueError) as exc:
            raise TrackingAdmissionUnavailableError from exc
        if outcome == 0:
            raise TrackingAdmissionBusyError
        if outcome == -1:
            raise TrackingAdmissionCapacityError
        if outcome != 1:
            raise TrackingAdmissionUnavailableError
        return lease

    def release(self, lease):
        keys = (
            self._GLOBAL_KEY,
            self._redis_scope_key("actor", lease.actor_key),
            self._redis_scope_key("account", lease.account_key),
        )
        try:
            self.client.eval(self._RELEASE, len(keys), *keys, lease.token)
        except RedisError:
            # The bounded lease expires without intervention if cleanup cannot
            # reach Redis during response finalization.
            return


class AsyncTrackingAdmission:
    """Cancellation-safe local and distributed admission coordinator."""

    def __init__(self, shared):
        if isinstance(shared, MemoryTrackingAdmission):
            self.local = shared
            self.shared = None
        else:
            self.local = MemoryTrackingAdmission(maximum=shared.maximum)
            self.shared = shared
        self._cleanup_tasks = set()

    @property
    def pending_cleanups(self):
        return len(self._cleanup_tasks)

    def _remember(self, task):
        self._cleanup_tasks.add(task)

        def consume(done):
            self._cleanup_tasks.discard(done)
            if not done.cancelled():
                done.exception()

        task.add_done_callback(consume)

    async def _release_late_acquisition(self, acquisition):
        try:
            lease = await acquisition
        except BaseException:
            return
        try:
            await asyncio.to_thread(self.shared.release, lease)
        except BaseException:
            # Redis leases have a bounded TTL even if final cleanup is interrupted.
            return

    async def acquire(self, actor_id, seller_id, account_id, environment):
        arguments = (actor_id, seller_id, account_id, environment)
        local_lease = self.local.acquire(*arguments)
        if self.shared is None:
            return local_lease, None
        acquisition = asyncio.create_task(asyncio.to_thread(self.shared.acquire, *arguments))
        try:
            shared_lease = await asyncio.shield(acquisition)
        except (
            TrackingAdmissionBusyError,
            TrackingAdmissionCapacityError,
            TrackingAdmissionUnavailableError,
        ):
            self.local.release(local_lease)
            raise
        except BaseException:
            self.local.release(local_lease)
            cleanup = asyncio.create_task(self._release_late_acquisition(acquisition))
            self._remember(cleanup)
            raise
        return local_lease, shared_lease

    async def release(self, leases):
        local_lease, shared_lease = leases
        self.local.release(local_lease)
        if shared_lease is None:
            return
        cleanup = asyncio.create_task(asyncio.to_thread(self.shared.release, shared_lease))
        self._remember(cleanup)
        await asyncio.shield(cleanup)


def create_tracking_admission(queue):
    connection = getattr(queue, "connection", None)
    if connection is not None:
        return RedisTrackingAdmission(connection)
    return MemoryTrackingAdmission()
