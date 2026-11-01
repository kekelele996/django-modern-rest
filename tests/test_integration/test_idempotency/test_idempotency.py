import json
from http import HTTPStatus
from typing import Any, Final

import pytest
from django.urls import reverse

from dmr.idempotency.middleware import _processing_record
from dmr.test import DMRAsyncClient, DMRClient, DMRRequestFactory
from server.apps.middlewares import views  # type: ignore[import-not-found]

_CHARGES_ENDPOINT: Final = 'api:middlewares:charges'
_ASYNC_CHARGES_ENDPOINT: Final = 'api:middlewares:async_charges'
_IDEMPOTENCY_HEADER: Final = 'Idempotency-Key'
_OTHER_AMOUNT: Final = 250
_LOCK_TIMEOUT: Final = 30
_WORKER_ID: Final = 'other-worker'


def _payload(*, amount: int = 100) -> dict[str, Any]:
    return {'order_id': 'order-1', 'amount': amount}


def _post(
    client: DMRClient,
    url: str,
    *,
    key: str | None,
    amount: int = 100,
) -> Any:
    headers = {} if key is None else {_IDEMPOTENCY_HEADER: key}
    return client.post(
        url,
        data=json.dumps(_payload(amount=amount)),
        content_type='application/json',
        headers=headers,
    )


def test_same_key_creates_single_charge(dmr_client: DMRClient) -> None:
    """Retrying with the same key creates exactly one charge record."""
    url = reverse(_CHARGES_ENDPOINT)

    first = _post(dmr_client, url, key='charge-key-1')
    retry = _post(dmr_client, url, key='charge-key-1')

    assert first.status_code == HTTPStatus.CREATED
    assert retry.status_code == HTTPStatus.CREATED
    assert (
        first.json()
        == retry.json()
        == {
            'record_id': 1,
            'order_id': 'order-1',
            'amount': 100,
            'status': 'charged',
        }
    )
    assert views.charge_count() == 1


def test_same_key_different_payload_conflicts(
    dmr_client: DMRClient,
) -> None:
    """Reusing a key for a different payload is rejected with 409."""
    url = reverse(_CHARGES_ENDPOINT)

    first = _post(dmr_client, url, key='charge-key-2')
    conflict = _post(
        dmr_client,
        url,
        key='charge-key-2',
        amount=_OTHER_AMOUNT,
    )

    assert first.status_code == HTTPStatus.CREATED
    assert conflict.status_code == HTTPStatus.CONFLICT
    assert conflict.headers['Content-Type'] == 'application/json'
    assert conflict.json() == {
        'detail': [
            {
                'msg': (
                    'Idempotency key was already used '
                    'with a different request payload'
                ),
            },
        ],
    }
    assert views.charge_count() == 1


def test_replay_is_identical_after_response(dmr_client: DMRClient) -> None:
    """Any later retry, after the first response was sent, sees the same one."""
    url = reverse(_CHARGES_ENDPOINT)

    first = _post(dmr_client, url, key='charge-key-3')
    second = _post(dmr_client, url, key='charge-key-3')
    third = _post(dmr_client, url, key='charge-key-3')

    assert first.status_code == second.status_code == third.status_code
    assert first.content == second.content == third.content
    assert second.headers['Content-Type'] == 'application/json'
    assert views.charge_count() == 1


def test_requests_without_key_run_every_time(
    dmr_client: DMRClient,
) -> None:
    """Without the header every request is executed independently."""
    url = reverse(_CHARGES_ENDPOINT)

    first = _post(dmr_client, url, key=None)
    second = _post(dmr_client, url, key=None)

    assert first.json()['record_id'] == 1
    assert second.json()['record_id'] == 2
    assert views.charge_count() == 2


def test_different_keys_create_separate_charges(
    dmr_client: DMRClient,
) -> None:
    """Different keys are independent requests."""
    url = reverse(_CHARGES_ENDPOINT)

    first = _post(dmr_client, url, key='charge-key-4')
    second = _post(dmr_client, url, key='charge-key-5')

    assert first.json()['record_id'] == 1
    assert second.json()['record_id'] == 2
    assert views.charge_count() == 2


def test_in_progress_request_is_conflicted(
    dmr_client: DMRClient,
    dmr_rf: DMRRequestFactory,
) -> None:
    """A concurrent request with the same key gets 409, not a second charge."""
    url = reverse(_CHARGES_ENDPOINT)
    key = 'charge-key-in-progress'

    # Simulate another worker still processing this key:
    pending_request = dmr_rf.post(
        url,
        data=json.dumps(_payload()),
        content_type='application/json',
        headers={_IDEMPOTENCY_HEADER: key},
    )
    cache_key = views.idempotency.storage.make_key(
        views.idempotency._scope(pending_request),
        key,
    )
    views.idempotency.storage.store(
        cache_key,
        _processing_record(
            views.idempotency._fingerprint(pending_request),
            _WORKER_ID,
        ),
        timeout=_LOCK_TIMEOUT,
    )

    conflict = _post(dmr_client, url, key=key)

    assert conflict.status_code == HTTPStatus.CONFLICT
    assert conflict.json() == {
        'detail': [
            {
                'msg': (
                    'A request with the same idempotency key '
                    'is still being processed'
                ),
            },
        ],
    }
    assert views.charge_count() == 0


@pytest.mark.asyncio
async def test_async_same_key_creates_single_charge(
    dmr_async_client: DMRAsyncClient,
) -> None:
    """Async controllers execute once and replay the same response."""
    url = reverse(_ASYNC_CHARGES_ENDPOINT)

    async def post(key: str, amount: int = 100) -> Any:
        return await dmr_async_client.post(
            url,
            data=json.dumps(_payload(amount=amount)),
            content_type='application/json',
            headers={_IDEMPOTENCY_HEADER: key},
        )

    first = await post('async-charge-key-1')
    retry = await post('async-charge-key-1')

    assert first.status_code == HTTPStatus.CREATED
    assert retry.status_code == HTTPStatus.CREATED
    assert first.content == retry.content
    assert first.json() == {
        'record_id': 1,
        'order_id': 'order-1',
        'amount': 100,
        'status': 'charged',
    }
    assert views.charge_count() == 1


@pytest.mark.asyncio
async def test_async_different_payload_conflicts(
    dmr_async_client: DMRAsyncClient,
) -> None:
    """Async controllers reject key reuse with a different payload."""
    url = reverse(_ASYNC_CHARGES_ENDPOINT)

    first = await dmr_async_client.post(
        url,
        data=json.dumps(_payload()),
        content_type='application/json',
        headers={_IDEMPOTENCY_HEADER: 'async-charge-key-2'},
    )
    conflict = await dmr_async_client.post(
        url,
        data=json.dumps(_payload(amount=_OTHER_AMOUNT)),
        content_type='application/json',
        headers={_IDEMPOTENCY_HEADER: 'async-charge-key-2'},
    )

    assert first.status_code == HTTPStatus.CREATED
    assert conflict.status_code == HTTPStatus.CONFLICT
    assert views.charge_count() == 1
