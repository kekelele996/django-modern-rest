import base64
import json
from collections.abc import Callable
from http import HTTPStatus
from types import SimpleNamespace
from typing import Any, ClassVar, cast

import pytest
from django.contrib.auth.models import AnonymousUser
from django.http import (
    HttpRequest,
    HttpResponse,
    HttpResponseBase,
    StreamingHttpResponse,
)
from typing_extensions import override

from dmr.idempotency import Idempotency
from dmr.idempotency.middleware import _processing_record, default_scope
from dmr.idempotency.records import (
    COMPLETE,
    IdempotencyRecord,
    StoredHttpResponse,
)
from dmr.idempotency.storage import DjangoCacheStorage
from dmr.plugins.pydantic import PydanticSerializer
from dmr.test import DMRAsyncRequestFactory, DMRRequestFactory

_IDEMPOTENCY_HEADER = 'Idempotency-Key'
_PATH = '/charge/'
_LOCK_TIMEOUT = 30
_LOCK_OWNER = 'owner'
_OTHER_AMOUNT = 250
_USER_PK = 42


def _payload(*, amount: int = 100) -> dict[str, Any]:
    return {'order_id': 'order-1', 'amount': amount}


def _post(
    dmr_rf: DMRRequestFactory,
    *,
    key: str | None = 'key-1',
    amount: int = 100,
) -> HttpRequest:
    headers = {} if key is None else {_IDEMPOTENCY_HEADER: key}
    return dmr_rf.post(
        _PATH,
        data=_payload(amount=amount),
        content_type='application/json',
        headers=headers,
    )


def _make_view(
    calls: dict[str, int],
    *,
    raises: bool = False,
    stream: bool = False,
    status_code: HTTPStatus = HTTPStatus.CREATED,
) -> Callable[..., HttpResponseBase]:
    def view(
        request: HttpRequest,
        *args: Any,
        **kwargs: Any,
    ) -> HttpResponseBase:
        calls['count'] += 1
        if raises:
            raise RuntimeError('boom')
        if stream:
            return StreamingHttpResponse(iter([b'{"charged":true}']))
        response = HttpResponse(
            b'{"charged":true}',
            status=status_code,
            content_type='application/json',
        )
        response['X-Charge-Id'] = f'charge-{calls["count"]}'
        response.set_cookie('sessionid', 'stored-session')
        return response

    return view


def _seed_processing(
    middleware: Idempotency,
    request: HttpRequest,
    key: str,
) -> None:
    fingerprint = middleware._fingerprint(request)
    cache_key = middleware.storage.make_key(
        middleware._scope(request),
        key,
    )
    middleware.storage.store(
        cache_key,
        _processing_record(fingerprint, _LOCK_OWNER),
        timeout=_LOCK_TIMEOUT,
    )


def _completed_record(fingerprint: str) -> IdempotencyRecord:
    """Response supposedly stored by another concurrent request."""
    stored_response: StoredHttpResponse = {
        'status_code': 200,
        'reason': 'OK',
        'body': base64.b64encode(b'{"other":true}').decode('ascii'),
        'headers': [('Content-Type', 'application/json')],
        'cookies': [],
    }
    return {
        'status': COMPLETE,
        'fingerprint': fingerprint,
        'response': stored_response,
    }


class _LostLockStorage(DjangoCacheStorage):
    """``add`` always fails and nothing is ever stored."""

    @override
    def acquire(
        self,
        cache_key: str,
        record: IdempotencyRecord,
        timeout: int,
    ) -> bool:
        return False

    @override
    def get_record(self, cache_key: str) -> IdempotencyRecord | None:
        """Nothing is ever stored."""


class _RecoveredLockStorage(DjangoCacheStorage):
    """``add`` fails on the first attempt and succeeds on the second."""

    attempts: ClassVar[int] = 0

    @override
    def __post_init__(self) -> None:
        super().__post_init__()
        type(self).attempts = 0

    @override
    def acquire(
        self,
        cache_key: str,
        record: IdempotencyRecord,
        timeout: int,
    ) -> bool:
        type(self).attempts += 1
        return type(self).attempts == 2

    @override
    def get_record(self, cache_key: str) -> IdempotencyRecord | None:
        """The record only appears after a successful acquisition."""


class _AsyncLostLockStorage(_LostLockStorage):
    """Async version of :class:`_LostLockStorage`."""

    @override
    async def aacquire(
        self,
        cache_key: str,
        record: IdempotencyRecord,
        timeout_seconds: int,
    ) -> bool:
        return False

    @override
    async def aget_record(self, cache_key: str) -> IdempotencyRecord | None:
        """Nothing is ever stored."""


class _AsyncRecoveredLockStorage(_RecoveredLockStorage):
    """Async version of :class:`_RecoveredLockStorage`."""

    attempts: ClassVar[int] = 0

    @override
    async def aacquire(
        self,
        cache_key: str,
        record: IdempotencyRecord,
        timeout_seconds: int,
    ) -> bool:
        type(self).attempts += 1
        return type(self).attempts == 2

    @override
    async def aget_record(self, cache_key: str) -> IdempotencyRecord | None:
        """The record only appears after a successful acquisition."""


class _CompletedByOtherStorage(DjangoCacheStorage):
    """First lookup sees nothing, retry sees another request's response."""

    calls: ClassVar[int] = 0
    fingerprint: ClassVar[str] = ''

    @override
    def __post_init__(self) -> None:
        super().__post_init__()
        type(self).calls = 0
        type(self).fingerprint = ''

    @override
    def acquire(
        self,
        cache_key: str,
        record: IdempotencyRecord,
        timeout: int,
    ) -> bool:
        type(self).fingerprint = record['fingerprint']
        return False

    @override
    def get_record(self, cache_key: str) -> IdempotencyRecord | None:  # noqa: WPS615
        """Nothing on the first lookup, a completed record afterwards."""
        type(self).calls += 1
        record: IdempotencyRecord | None = None
        if type(self).calls > 1:
            record = _completed_record(type(self).fingerprint)
        return record


class _AsyncCompletedByOtherStorage(_CompletedByOtherStorage):
    """Async version of :class:`_CompletedByOtherStorage`."""

    @override
    async def aacquire(
        self,
        cache_key: str,
        record: IdempotencyRecord,
        timeout_seconds: int,
    ) -> bool:
        type(self).fingerprint = record['fingerprint']
        return False

    @override
    async def aget_record(
        self,
        cache_key: str,
    ) -> IdempotencyRecord | None:
        type(self).calls += 1
        record: IdempotencyRecord | None = None
        if type(self).calls > 1:
            record = _completed_record(type(self).fingerprint)
        return record


def _run_twice_sync(
    dmr_rf: DMRRequestFactory,
) -> tuple[dict[str, int], list[HttpResponseBase]]:
    """Execute the same idempotent request two times."""
    calls: dict[str, int] = {'count': 0}
    middleware = Idempotency(serializer=PydanticSerializer)
    view = _make_view(calls)
    responses = [
        middleware(view)(_post(dmr_rf)),
        middleware(view)(_post(dmr_rf)),
    ]
    return calls, responses


def test_replays_first_response_sync(dmr_rf: DMRRequestFactory) -> None:
    """The second identical request gets the exact first response."""
    calls, responses = _run_twice_sync(dmr_rf)
    first, second = responses

    assert isinstance(first, HttpResponse)
    assert isinstance(second, HttpResponse)
    assert calls == {'count': 1}
    assert second.status_code == HTTPStatus.CREATED
    assert second.content == first.content
    assert second.headers['X-Charge-Id'] == 'charge-1'
    assert second.cookies['sessionid'].value == 'stored-session'
    assert len(second.content) == len(first.content)


def test_conflict_on_different_payload(dmr_rf: DMRRequestFactory) -> None:
    """Reusing the key with another payload is a 409 conflict."""
    middleware = Idempotency(serializer=PydanticSerializer)
    calls: dict[str, int] = {'count': 0}
    view = _make_view(calls)

    middleware(view)(_post(dmr_rf))
    conflict = middleware(view)(_post(dmr_rf, amount=_OTHER_AMOUNT))

    assert isinstance(conflict, HttpResponse)
    assert conflict.status_code == HTTPStatus.CONFLICT
    assert json.loads(conflict.content) == {
        'detail': [
            {
                'msg': (
                    'Idempotency key was already used '
                    'with a different request payload'
                ),
            },
        ],
    }
    assert calls == {'count': 1}


def test_request_without_key_runs_every_time(
    dmr_rf: DMRRequestFactory,
) -> None:
    """Requests without the header are never deduplicated."""
    middleware = Idempotency(serializer=PydanticSerializer)
    calls: dict[str, int] = {'count': 0}
    view = _make_view(calls)

    middleware(view)(_post(dmr_rf, key=None))
    middleware(view)(_post(dmr_rf, key=None))

    assert calls == {'count': 2}


def test_empty_key_runs_every_time(dmr_rf: DMRRequestFactory) -> None:
    """An empty header value is treated as a missing key."""
    middleware = Idempotency(serializer=PydanticSerializer)
    request = dmr_rf.post(
        _PATH,
        data=_payload(),
        content_type='application/json',
        headers={_IDEMPOTENCY_HEADER: ''},
    )

    assert middleware._idempotency_key(request) is None


def test_safe_method_is_ignored(dmr_rf: DMRRequestFactory) -> None:
    """The header is ignored for methods that don't create side effects."""
    middleware = Idempotency(serializer=PydanticSerializer)
    calls: dict[str, int] = {'count': 0}
    request = dmr_rf.get(_PATH, headers={_IDEMPOTENCY_HEADER: 'key-1'})

    def get_view(request: HttpRequest) -> HttpResponse:
        calls['count'] += 1
        return HttpResponse(status=HTTPStatus.OK)

    middleware(get_view)(request)
    middleware(get_view)(
        dmr_rf.get(_PATH, headers={_IDEMPOTENCY_HEADER: 'key-1'}),
    )

    assert calls == {'count': 2}


def test_configured_methods(dmr_rf: DMRRequestFactory) -> None:
    """Only the configured HTTP methods participate in idempotency."""
    middleware = Idempotency(
        serializer=PydanticSerializer,
        methods=frozenset(('POST',)),
    )

    delete_request = dmr_rf.generic(
        'DELETE',
        _PATH,
        headers={_IDEMPOTENCY_HEADER: 'key-1'},
    )
    post_request = _post(dmr_rf)

    assert middleware._idempotency_key(delete_request) is None
    assert middleware._idempotency_key(post_request) == 'key-1'


def test_in_progress_request_conflicts(dmr_rf: DMRRequestFactory) -> None:
    """The same key arriving during processing gets 409."""
    middleware = Idempotency(serializer=PydanticSerializer)
    calls: dict[str, int] = {'count': 0}
    request = _post(dmr_rf)
    _seed_processing(middleware, request, 'key-1')

    response = middleware(_make_view(calls))(request)

    assert isinstance(response, HttpResponse)
    assert response.status_code == HTTPStatus.CONFLICT
    assert json.loads(response.content) == {
        'detail': [
            {
                'msg': (
                    'A request with the same idempotency key '
                    'is still being processed'
                ),
            },
        ],
    }
    assert calls == {'count': 0}


def test_lock_released_on_exception(dmr_rf: DMRRequestFactory) -> None:
    """A crashed request releases the lock, so the retry can run."""
    middleware = Idempotency(serializer=PydanticSerializer)
    calls: dict[str, int] = {'count': 0}

    with pytest.raises(RuntimeError, match='boom'):
        middleware(_make_view(calls, raises=True))(_post(dmr_rf))

    cache_key = middleware.storage.make_key('', 'key-1')
    assert middleware.storage.get_record(cache_key) is None

    response = middleware(_make_view(calls))(_post(dmr_rf))
    assert isinstance(response, HttpResponse)
    assert response.status_code == HTTPStatus.CREATED
    assert calls == {'count': 2}


def test_server_error_is_not_stored(dmr_rf: DMRRequestFactory) -> None:
    """5xx responses stay retryable with the same key."""
    middleware = Idempotency(serializer=PydanticSerializer)
    calls: dict[str, int] = {'count': 0}
    view = _make_view(calls, status_code=HTTPStatus.INTERNAL_SERVER_ERROR)

    first = middleware(view)(_post(dmr_rf))
    second = middleware(view)(_post(dmr_rf))

    assert first.status_code == HTTPStatus.INTERNAL_SERVER_ERROR
    assert second.status_code == HTTPStatus.INTERNAL_SERVER_ERROR
    assert calls == {'count': 2}


def test_streaming_response_is_not_stored(dmr_rf: DMRRequestFactory) -> None:
    """Streaming responses are passed through and never cached."""
    middleware = Idempotency(serializer=PydanticSerializer)
    calls: dict[str, int] = {'count': 0}
    view = _make_view(calls, stream=True)

    first = middleware(view)(_post(dmr_rf))
    second = middleware(view)(_post(dmr_rf))

    assert isinstance(first, StreamingHttpResponse)
    assert isinstance(second, StreamingHttpResponse)
    assert calls == {'count': 2}
    assert (
        middleware.storage.get_record(
            middleware.storage.make_key('', 'key-1'),
        )
        is None
    )


def test_custom_should_store_predicate(dmr_rf: DMRRequestFactory) -> None:
    """Callers can decide which responses are cached."""
    middleware = Idempotency(
        serializer=PydanticSerializer,
        should_store=lambda response: False,
    )
    calls: dict[str, int] = {'count': 0}

    middleware(_make_view(calls))(_post(dmr_rf))
    middleware(_make_view(calls))(_post(dmr_rf))

    assert calls == {'count': 2}


def _scoped_post(dmr_rf: DMRRequestFactory, client: str) -> HttpRequest:
    """Build a request carrying the given client scope."""
    return dmr_rf.post(
        _PATH,
        data=_payload(),
        content_type='application/json',
        headers={_IDEMPOTENCY_HEADER: 'key-1', 'X-Client': client},
    )


def test_scope_separates_clients(dmr_rf: DMRRequestFactory) -> None:
    """The same key in different scopes is treated as different requests."""
    middleware = Idempotency(
        serializer=PydanticSerializer,
        scope=lambda request: request.headers['X-Client'],
    )
    calls: dict[str, int] = {'count': 0}
    view = _make_view(calls)

    middleware(view)(_scoped_post(dmr_rf, 'client-a'))
    replay = middleware(view)(_scoped_post(dmr_rf, 'client-a'))
    other = middleware(view)(_scoped_post(dmr_rf, 'client-b'))

    assert replay.status_code == HTTPStatus.CREATED
    assert other.status_code == HTTPStatus.CREATED
    assert calls == {'count': 2}


def test_default_scope(dmr_rf: DMRRequestFactory) -> None:
    """Default scope separates authenticated users, anonymous is empty."""
    anonymous_request = _post(dmr_rf)
    anonymous_request.user = AnonymousUser()
    authed_request = _post(dmr_rf, key='key-2')
    authed_request.user = cast(
        Any,
        SimpleNamespace(is_authenticated=True, pk=_USER_PK),
    )

    assert not default_scope(anonymous_request)
    assert default_scope(authed_request) == f'user:{_USER_PK}'

    global_scope = Idempotency(serializer=PydanticSerializer, scope=None)
    assert not global_scope._scope(authed_request)


def test_lost_lock_returns_conflict_sync(dmr_rf: DMRRequestFactory) -> None:
    """When the lock keeps failing for no reason, return 409."""
    middleware = Idempotency(
        serializer=PydanticSerializer,
        storage=_LostLockStorage(),
    )
    calls: dict[str, int] = {'count': 0}

    response = middleware(_make_view(calls))(_post(dmr_rf))

    assert response.status_code == HTTPStatus.CONFLICT
    assert calls == {'count': 0}


def test_recovered_lock_executes_sync(dmr_rf: DMRRequestFactory) -> None:
    """A lock that appears on the second attempt still executes once."""
    storage = _RecoveredLockStorage()
    middleware = Idempotency(
        serializer=PydanticSerializer,
        storage=storage,
    )
    calls: dict[str, int] = {'count': 0}

    response = middleware(_make_view(calls))(_post(dmr_rf))

    assert response.status_code == HTTPStatus.CREATED
    assert storage.attempts == 2
    assert calls == {'count': 1}


def test_other_request_completed_sync(dmr_rf: DMRRequestFactory) -> None:
    """A record appearing after a failed acquisition is replayed as well."""
    middleware = Idempotency(
        serializer=PydanticSerializer,
        storage=_CompletedByOtherStorage(),
    )
    calls: dict[str, int] = {'count': 0}
    request = _post(dmr_rf)

    response = middleware(_make_view(calls))(request)

    assert isinstance(response, HttpResponse)
    assert response.status_code == HTTPStatus.OK
    assert response.content == b'{"other":true}'
    assert calls == {'count': 0}
    assert getattr(request, 'idempotency_replayed', False) is True


@pytest.mark.asyncio
async def test_replays_first_response_async(
    dmr_async_rf: DMRAsyncRequestFactory,
) -> None:
    """Async controllers also replay the stored response."""
    middleware = Idempotency(serializer=PydanticSerializer)
    calls: dict[str, int] = {'count': 0}

    async def view(
        request: HttpRequest,
        *args: Any,
        **kwargs: Any,
    ) -> HttpResponse:
        calls['count'] += 1
        response = HttpResponse(
            b'{"charged":true}',
            status=HTTPStatus.CREATED,
            content_type='application/json',
        )
        response['X-Charge-Id'] = f'charge-{calls["count"]}'
        return response

    def post() -> HttpRequest:
        return dmr_async_rf.post(
            _PATH,
            data=_payload(),
            content_type='application/json',
            headers={_IDEMPOTENCY_HEADER: 'key-1'},
        )

    first = await middleware(view)(post())
    second_request = post()
    second = await middleware(view)(second_request)

    assert isinstance(first, HttpResponse)
    assert isinstance(second, HttpResponse)
    assert first.content == second.content
    assert second.headers['X-Charge-Id'] == 'charge-1'
    assert calls == {'count': 1}
    assert getattr(second_request, 'idempotency_replayed', False) is True


@pytest.mark.asyncio
async def test_async_conflict_and_passthrough(
    dmr_async_rf: DMRAsyncRequestFactory,
) -> None:
    """Different payload conflicts, missing key passes through."""
    middleware = Idempotency(serializer=PydanticSerializer)
    calls: dict[str, int] = {'count': 0}

    async def view(
        request: HttpRequest,
        *args: Any,
        **kwargs: Any,
    ) -> HttpResponse:
        calls['count'] += 1
        return HttpResponse(status=HTTPStatus.CREATED)

    await middleware(view)(
        dmr_async_rf.post(
            _PATH,
            data=_payload(),
            content_type='application/json',
            headers={_IDEMPOTENCY_HEADER: 'key-1'},
        ),
    )
    conflict = await middleware(view)(
        dmr_async_rf.post(
            _PATH,
            data=_payload(amount=_OTHER_AMOUNT),
            content_type='application/json',
            headers={_IDEMPOTENCY_HEADER: 'key-1'},
        ),
    )
    assert conflict.status_code == HTTPStatus.CONFLICT

    await middleware(view)(
        dmr_async_rf.post(
            _PATH,
            data=_payload(),
            content_type='application/json',
        ),
    )
    assert calls == {'count': 2}


@pytest.mark.asyncio
async def test_async_in_progress_conflict(
    dmr_async_rf: DMRAsyncRequestFactory,
) -> None:
    """Seen processing state also conflicts on the async path."""
    middleware = Idempotency(serializer=PydanticSerializer)
    calls: dict[str, int] = {'count': 0}
    request = dmr_async_rf.post(
        _PATH,
        data=_payload(),
        content_type='application/json',
        headers={_IDEMPOTENCY_HEADER: 'key-1'},
    )
    _seed_processing(middleware, request, 'key-1')

    async def view(
        request: HttpRequest,
        *args: Any,
        **kwargs: Any,
    ) -> HttpResponse:
        calls['count'] += 1
        return HttpResponse(status=HTTPStatus.CREATED)

    response = await middleware(view)(request)
    assert response.status_code == HTTPStatus.CONFLICT
    assert calls == {'count': 0}


@pytest.mark.asyncio
async def test_async_lock_released_on_exception(
    dmr_async_rf: DMRAsyncRequestFactory,
) -> None:
    """Async crashed requests release the lock for a retry."""
    middleware = Idempotency(serializer=PydanticSerializer)
    calls: dict[str, int] = {'count': 0}

    async def failing(
        request: HttpRequest,
        *args: Any,
        **kwargs: Any,
    ) -> HttpResponse:
        calls['count'] += 1
        raise RuntimeError('boom')

    async def ok(
        request: HttpRequest,
        *args: Any,
        **kwargs: Any,
    ) -> HttpResponse:
        calls['count'] += 1
        return HttpResponse(status=HTTPStatus.CREATED)

    def post() -> HttpRequest:
        return dmr_async_rf.post(
            _PATH,
            data=_payload(),
            content_type='application/json',
            headers={_IDEMPOTENCY_HEADER: 'key-1'},
        )

    with pytest.raises(RuntimeError, match='boom'):
        await middleware(failing)(post())
    response = await middleware(ok)(post())
    assert response.status_code == HTTPStatus.CREATED
    assert calls == {'count': 2}


@pytest.mark.asyncio
async def test_async_server_error_not_stored(
    dmr_async_rf: DMRAsyncRequestFactory,
) -> None:
    """Async 5xx responses stay retryable."""
    middleware = Idempotency(serializer=PydanticSerializer)
    calls: dict[str, int] = {'count': 0}

    async def view(
        request: HttpRequest,
        *args: Any,
        **kwargs: Any,
    ) -> HttpResponse:
        calls['count'] += 1
        return HttpResponse(status=HTTPStatus.INTERNAL_SERVER_ERROR)

    def post() -> HttpRequest:
        return dmr_async_rf.post(
            _PATH,
            data=_payload(),
            content_type='application/json',
            headers={_IDEMPOTENCY_HEADER: 'key-1'},
        )

    first = await middleware(view)(post())
    second = await middleware(view)(post())
    assert first.status_code == HTTPStatus.INTERNAL_SERVER_ERROR
    assert second.status_code == HTTPStatus.INTERNAL_SERVER_ERROR
    assert calls == {'count': 2}


@pytest.mark.asyncio
async def test_async_streaming_not_stored(
    dmr_async_rf: DMRAsyncRequestFactory,
) -> None:
    """Async streaming responses are not cached."""
    middleware = Idempotency(serializer=PydanticSerializer)
    calls: dict[str, int] = {'count': 0}

    async def view(
        request: HttpRequest,
        *args: Any,
        **kwargs: Any,
    ) -> StreamingHttpResponse:
        calls['count'] += 1
        return StreamingHttpResponse(iter([b'chunk']))

    def post() -> HttpRequest:
        return dmr_async_rf.post(
            _PATH,
            data=_payload(),
            content_type='application/json',
            headers={_IDEMPOTENCY_HEADER: 'key-1'},
        )

    first = await middleware(view)(post())
    second = await middleware(view)(post())
    assert isinstance(first, StreamingHttpResponse)
    assert isinstance(second, StreamingHttpResponse)
    assert calls == {'count': 2}


@pytest.mark.asyncio
async def test_async_lost_lock_returns_conflict(
    dmr_async_rf: DMRAsyncRequestFactory,
) -> None:
    """Async path also returns 409 when the lock can't be acquired."""
    middleware = Idempotency(
        serializer=PydanticSerializer,
        storage=_AsyncLostLockStorage(),
    )
    calls: dict[str, int] = {'count': 0}

    async def view(
        request: HttpRequest,
        *args: Any,
        **kwargs: Any,
    ) -> HttpResponse:
        calls['count'] += 1
        return HttpResponse(status=HTTPStatus.CREATED)

    response = await middleware(view)(
        dmr_async_rf.post(
            _PATH,
            data=_payload(),
            content_type='application/json',
            headers={_IDEMPOTENCY_HEADER: 'key-1'},
        ),
    )
    assert response.status_code == HTTPStatus.CONFLICT
    assert calls == {'count': 0}


@pytest.mark.asyncio
async def test_async_recovered_lock_executes(
    dmr_async_rf: DMRAsyncRequestFactory,
) -> None:
    """Second async acquisition attempt that succeeds still runs the view."""
    storage = _AsyncRecoveredLockStorage()
    middleware = Idempotency(
        serializer=PydanticSerializer,
        storage=storage,
    )
    calls: dict[str, int] = {'count': 0}

    async def view(
        request: HttpRequest,
        *args: Any,
        **kwargs: Any,
    ) -> HttpResponse:
        calls['count'] += 1
        return HttpResponse(status=HTTPStatus.CREATED)

    response = await middleware(view)(
        dmr_async_rf.post(
            _PATH,
            data=_payload(),
            content_type='application/json',
            headers={_IDEMPOTENCY_HEADER: 'key-1'},
        ),
    )
    assert response.status_code == HTTPStatus.CREATED
    assert storage.attempts == 2
    assert calls == {'count': 1}


@pytest.mark.asyncio
async def test_async_other_request_completed(
    dmr_async_rf: DMRAsyncRequestFactory,
) -> None:
    """Async path replays a record that appeared during acquisition."""
    middleware = Idempotency(
        serializer=PydanticSerializer,
        storage=_AsyncCompletedByOtherStorage(),
    )
    calls: dict[str, int] = {'count': 0}
    request = dmr_async_rf.post(
        _PATH,
        data=_payload(),
        content_type='application/json',
        headers={_IDEMPOTENCY_HEADER: 'key-1'},
    )

    async def view(
        request: HttpRequest,
        *args: Any,
        **kwargs: Any,
    ) -> HttpResponse:
        calls['count'] += 1
        return HttpResponse(status=HTTPStatus.CREATED)

    response = await middleware(view)(request)
    assert isinstance(response, HttpResponse)
    assert response.status_code == HTTPStatus.OK
    assert response.content == b'{"other":true}'
    assert calls == {'count': 0}
    assert getattr(request, 'idempotency_replayed', False) is True
