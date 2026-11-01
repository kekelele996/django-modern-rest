from typing import Final

import pytest

from dmr.idempotency.records import (
    COMPLETE,
    PROCESSING,
    IdempotencyRecord,
    StoredHttpResponse,
)
from dmr.idempotency.storage import DjangoCacheStorage

_LOCK_TIMEOUT: Final = 30


def _processing_record() -> IdempotencyRecord:
    return {
        'status': PROCESSING,
        'fingerprint': 'fp',
        'token': 'owner-token',
    }


def _complete_record() -> IdempotencyRecord:
    response: StoredHttpResponse = {
        'status_code': 201,
        'reason': 'Created',
        'body': 'e30=',
        'headers': [],
        'cookies': [],
    }
    return {
        'status': COMPLETE,
        'fingerprint': 'fp',
        'response': response,
    }


def test_make_key_is_scoped_and_hashed() -> None:
    """Keys are deterministic, scoped, and never contain the raw value."""
    storage = DjangoCacheStorage(key_prefix='test-idem')

    key = storage.make_key('user:1', 'client-key')

    assert key == storage.make_key('user:1', 'client-key')
    assert key != storage.make_key('user:2', 'client-key')
    assert key != storage.make_key('user:1', 'other-client-key')
    assert key.startswith('test-idem:')
    assert 'client-key' not in key


def test_sync_lifecycle() -> None:
    """Acquire and read the processing state synchronously."""
    storage = DjangoCacheStorage()
    cache_key = storage.make_key('', 'lifecycle')
    processing = _processing_record()

    assert (
        storage.acquire(
            cache_key,
            processing,
            timeout=_LOCK_TIMEOUT,
        )
        is True
    )
    # The same key can't be acquired again while being processed:
    assert (
        storage.acquire(
            cache_key,
            processing,
            timeout=_LOCK_TIMEOUT,
        )
        is False
    )
    assert storage.get_record(cache_key) == processing


def test_sync_store_and_release() -> None:
    """Finished records stay, wrong tokens and missing keys are ignored."""
    storage = DjangoCacheStorage()
    cache_key = storage.make_key('', 'lifecycle-release')
    storage.acquire(
        cache_key,
        _processing_record(),
        timeout=_LOCK_TIMEOUT,
    )

    # Releasing with another token must not delete the lock:
    storage.release(cache_key, 'other-token')
    assert storage.get_record(cache_key) is not None

    storage.store(
        cache_key,
        _complete_record(),
        timeout=_LOCK_TIMEOUT,
    )
    # Completed records must not be released either:
    storage.release(cache_key, 'owner-token')
    assert storage.get_record(cache_key) == _complete_record()

    # The real owner of a processing lock can release it:
    other_key = storage.make_key('', 'lifecycle-release-owner')
    storage.acquire(
        other_key,
        _processing_record(),
        timeout=_LOCK_TIMEOUT,
    )
    storage.release(other_key, 'owner-token')
    assert storage.get_record(other_key) is None
    # Releasing a missing key is a no-op:
    storage.release(storage.make_key('', 'missing'), 'owner-token')


@pytest.mark.asyncio
async def test_async_lifecycle() -> None:
    """Acquire and read the processing state async."""
    storage = DjangoCacheStorage()
    cache_key = storage.make_key('', 'async-lifecycle')
    processing = _processing_record()

    assert (
        await storage.aacquire(
            cache_key,
            processing,
            timeout_seconds=_LOCK_TIMEOUT,
        )
        is True
    )
    assert (
        await storage.aacquire(
            cache_key,
            processing,
            timeout_seconds=_LOCK_TIMEOUT,
        )
        is False
    )
    assert await storage.aget_record(cache_key) == processing


@pytest.mark.asyncio
async def test_async_release() -> None:
    """Only the lock owner can release it, missing keys are ignored."""
    storage = DjangoCacheStorage()
    cache_key = storage.make_key('', 'async-release-owner')

    await storage.arelease(
        storage.make_key('', 'async-missing'),
        'owner-token',
    )
    await storage.aacquire(
        cache_key,
        _processing_record(),
        timeout_seconds=_LOCK_TIMEOUT,
    )
    await storage.arelease(cache_key, 'other-token')
    await storage.arelease(cache_key, 'owner-token')
    assert await storage.aget_record(cache_key) is None


@pytest.mark.asyncio
async def test_async_store_keeps_completed() -> None:
    """Finished responses are not removed by a late release."""
    storage = DjangoCacheStorage()
    cache_key = storage.make_key('', 'async-complete')

    await storage.aacquire(
        cache_key,
        _processing_record(),
        timeout_seconds=_LOCK_TIMEOUT,
    )
    await storage.astore(
        cache_key,
        _complete_record(),
        timeout_seconds=_LOCK_TIMEOUT,
    )
    await storage.arelease(cache_key, 'owner-token')
    assert await storage.aget_record(cache_key) == _complete_record()
