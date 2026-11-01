import dataclasses
import hashlib

from django.core.cache import DEFAULT_CACHE_ALIAS, BaseCache, caches

from dmr.idempotency.records import PROCESSING, IdempotencyRecord
from dmr.internal.json import json_dumps_bytes, json_loads


@dataclasses.dataclass(slots=True, frozen=True, kw_only=True)
class DjangoCacheStorage:
    """
    Stores idempotency state in Django's cache framework.

    Provides both sync and async API, so the same storage instance
    works with both sync and async controllers.

    The lock acquisition uses :meth:`django.core.cache.BaseCache.add`,
    which is atomic for production-grade backends like Redis
    and Memcached. ``LocMemCache`` is only safe within a single process,
    use a shared cache backend when running multiple worker processes.

    Args:
        cache_name: Cache alias from the ``CACHES`` setting.
        key_prefix: Prefix for all cache keys used by idempotency.

    .. seealso::

        https://docs.djangoproject.com/en/stable/topics/cache/

    .. versionadded:: 0.17.0

    """

    cache_name: str = DEFAULT_CACHE_ALIAS
    key_prefix: str = 'dmr:idempotency'
    _cache: BaseCache = dataclasses.field(init=False)

    def __post_init__(self) -> None:
        """Initialize the selected cache backend."""
        object.__setattr__(self, '_cache', caches[self.cache_name])

    def make_key(self, scope: str, idempotency_key: str) -> str:
        """
        Build the cache key for an idempotency key.

        The raw client value is never used as a cache key directly:
        it has no length or charset limits, so both the optional
        *scope* (e.g. the authenticated user) and the key itself
        are hashed.
        """
        digest = hashlib.sha256(
            f'{scope}\x00{idempotency_key}'.encode(),
        ).hexdigest()
        return f'{self.key_prefix}:{digest}'

    # Sync API:

    def acquire(
        self,
        cache_key: str,
        record: IdempotencyRecord,
        timeout: int,
    ) -> bool:
        """Atomically store the processing record when the key is free."""
        return self._cache.add(
            cache_key,
            self._dump(record),
            timeout,
        )

    def get_record(self, cache_key: str) -> IdempotencyRecord | None:
        """Return the stored record, or ``None`` when the key is free."""
        return self._load(self._cache.get(cache_key))

    def store(
        self,
        cache_key: str,
        record: IdempotencyRecord,
        timeout: int,
    ) -> None:
        """Replace the processing record with the finished one."""
        self._cache.set(cache_key, self._dump(record), timeout=timeout)

    def release(self, cache_key: str, token: str) -> None:
        """
        Release the processing lock, but only when it is still ours.

        Nothing happens when the record has expired, was completed,
        or was acquired by another request.
        """
        record = self.get_record(cache_key)
        if (
            record is not None
            and record['status'] == PROCESSING
            and record.get('token') == token
        ):
            self._cache.delete(cache_key)

    # Async API:

    async def aacquire(
        self,
        cache_key: str,
        record: IdempotencyRecord,
        timeout_seconds: int,
    ) -> bool:
        """Async version of :meth:`acquire`."""
        return await self._cache.aadd(
            cache_key,
            self._dump(record),
            timeout_seconds,
        )

    async def aget_record(self, cache_key: str) -> IdempotencyRecord | None:
        """Async version of :meth:`get_record`."""
        return self._load(await self._cache.aget(cache_key))

    async def astore(
        self,
        cache_key: str,
        record: IdempotencyRecord,
        timeout_seconds: int,
    ) -> None:
        """Async version of :meth:`store`."""
        await self._cache.aset(
            cache_key,
            self._dump(record),
            timeout=timeout_seconds,
        )

    async def arelease(self, cache_key: str, token: str) -> None:
        """Async version of :meth:`release`."""
        record = await self.aget_record(cache_key)
        if (
            record is not None
            and record['status'] == PROCESSING
            and record.get('token') == token
        ):
            await self._cache.adelete(cache_key)

    def _dump(self, record: IdempotencyRecord) -> bytes:
        return json_dumps_bytes(record)

    def _load(self, stored: bytes | None) -> IdempotencyRecord | None:
        if stored is None:
            return None
        return json_loads(stored)  # type: ignore[no-any-return]
