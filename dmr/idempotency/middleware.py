import base64
import dataclasses
import hashlib
import inspect
import uuid
from collections.abc import Awaitable, Callable
from http import HTTPMethod, HTTPStatus
from typing import TYPE_CHECKING, Any, Final, cast, overload

from django.http import (
    HttpRequest,
    HttpResponse,
    HttpResponseBase,
    StreamingHttpResponse,
)

from dmr.errors import format_error
from dmr.idempotency.records import (
    COMPLETE,
    PROCESSING,
    IdempotencyRecord,
    StoredHttpResponse,
)
from dmr.idempotency.storage import DjangoCacheStorage
from dmr.response import build_response

if TYPE_CHECKING:
    from dmr.serializer import BaseSerializer

#: Default request header that carries the idempotency key.
DEFAULT_IDEMPOTENCY_HEADER: Final = 'Idempotency-Key'

#: How long finished responses are replayed for.
DEFAULT_TTL_SECONDS: Final = 24 * 60 * 60

#: How long an in-progress request keeps its lock before it can be retried.
DEFAULT_LOCK_TIMEOUT_SECONDS: Final = 60

#: Methods that create side effects and therefore need idempotency.
UNSAFE_METHODS: Final = frozenset((
    HTTPMethod.POST,
    HTTPMethod.PUT,
    HTTPMethod.PATCH,
    HTTPMethod.DELETE,
))

#: Headers that must not be copied into a replayed response.
_SKIPPED_HEADERS: Final = frozenset((
    'content-length',  # Recomputed from the stored body.
    'transfer-encoding',  # Streaming responses are never stored.
))

_DIFFERENT_REQUEST_MESSAGE: Final = (
    'Idempotency key was already used with a different request payload'
)
_IN_PROGRESS_MESSAGE: Final = (
    'A request with the same idempotency key is still being processed'
)

#: Type of a wrapped Django view callable.
_GetResponse = Callable[..., HttpResponseBase]
_AsyncGetResponse = Callable[..., Awaitable[HttpResponseBase]]
_ViewArgs = tuple[Any, ...]
_ViewKwargs = dict[str, Any]


def _processing_record(
    fingerprint: str,
    token: str,
) -> IdempotencyRecord:
    """Build the in-progress record for a newly acquired key."""
    return {
        'status': PROCESSING,
        'fingerprint': fingerprint,
        'token': token,
    }


def _complete_record(
    fingerprint: str,
    stored_response: StoredHttpResponse,
) -> IdempotencyRecord:
    """Build the finished record with the stored response."""
    return {
        'status': COMPLETE,
        'fingerprint': fingerprint,
        'response': stored_response,
    }


def default_scope(request: HttpRequest) -> str:
    """
    Scope idempotency keys by the authenticated user.

    Requests of the same user share their keys, anonymous requests
    share a single empty scope. Returning a constant value (or
    configuring ``scope=None``) makes keys global, which is only safe
    when clients generate globally unique keys.
    """
    user = getattr(request, 'user', None)
    if user is not None and getattr(user, 'is_authenticated', False):
        return f'user:{user.pk}'
    return ''


@dataclasses.dataclass(frozen=True, slots=True, kw_only=True)
class Idempotency:
    """
    Django middleware factory that makes unsafe requests idempotent.

    The client sends a unique value in the *header* request header.
    The first request is executed normally and its full response
    (status code, body, headers and cookies) is stored in *storage*.
    Every retry with the same key and the same payload gets
    the stored response replayed, without calling the controller again.
    A retry that reuses the key with a different payload gets
    a ``409 Conflict`` response. A second request arriving while
    the first one is still being processed also gets ``409 Conflict``.

    Requests without the header, as well as safe HTTP methods,
    are executed normally without any caching.

    Use it with :func:`~dmr.decorators.wrap_middleware` and document
    the ``409`` response:

    .. code:: python

        from http import HTTPStatus
        from dmr import Controller, ResponseSpec
        from dmr.decorators import wrap_middleware
        from dmr.errors import ErrorModel
        from dmr.idempotency import DjangoCacheStorage, Idempotency
        from dmr.plugins.pydantic import PydanticSerializer
        from django.http import HttpResponse

        idempotency = Idempotency(serializer=PydanticSerializer)

        @wrap_middleware(
            idempotency,
            ResponseSpec(ErrorModel, status_code=HTTPStatus.CONFLICT),
        )
        def idempotent_json(response: HttpResponse) -> HttpResponse:
            return response

        @idempotent_json
        class OrderController(Controller[PydanticSerializer]):
            responses = idempotent_json.responses

            def post(self) -> dict[str, str]:
                return {'status': 'charged'}

    Args:
        serializer: Serializer used to render the ``409`` error body.
        storage: Where idempotency state and responses are stored.
        header: Name of the request header carrying the idempotency key.
        methods: HTTP methods for which the header is honored.
        ttl: Seconds to keep a finished response for replay.
        lock_timeout: Seconds to keep the lock of a request in progress.
        scope: Callable returning an additional cache key scope,
            :func:`default_scope` separates users by default,
            pass ``None`` to use a single global scope.
        conflict_status: Status returned for key conflicts.
        should_store: Optional predicate deciding whether a finished
            response must be stored. By default all non-``5xx``
            responses are stored, so transient server errors
            can be retried with the same key.

    Replayed responses additionally set
    ``request.idempotency_replayed = True`` for logging and metrics.

    .. versionadded:: 0.17.0

    """

    serializer: 'type[BaseSerializer]'
    storage: DjangoCacheStorage = dataclasses.field(
        default_factory=DjangoCacheStorage,
    )
    header: str = DEFAULT_IDEMPOTENCY_HEADER
    methods: frozenset[str] = UNSAFE_METHODS
    ttl: int = DEFAULT_TTL_SECONDS
    lock_timeout: int = DEFAULT_LOCK_TIMEOUT_SECONDS
    scope: Callable[[HttpRequest], str] | None = default_scope
    conflict_status: HTTPStatus = HTTPStatus.CONFLICT
    should_store: Callable[[HttpResponseBase], bool] | None = None

    @overload
    def __call__(
        self,
        get_response: _AsyncGetResponse,
    ) -> Callable[..., Awaitable[HttpResponseBase]]: ...

    @overload
    def __call__(
        self,
        get_response: _GetResponse,
    ) -> Callable[..., HttpResponseBase]: ...

    def __call__(
        self,
        get_response: _GetResponse | _AsyncGetResponse,
    ) -> Callable[..., HttpResponseBase | Awaitable[HttpResponseBase]]:
        """Wrap a sync or async Django view with idempotency logic."""
        if inspect.iscoroutinefunction(get_response):
            async_get_response = cast('_AsyncGetResponse', get_response)

            async def async_dispatch(  # noqa: WPS430
                request: HttpRequest,
                *args: Any,
                **kwargs: Any,
            ) -> HttpResponseBase:
                return await self._handle_async(
                    request,
                    async_get_response,
                    args,
                    kwargs,
                )

            return async_dispatch

        sync_get_response = cast('_GetResponse', get_response)

        def dispatch(  # noqa: WPS430
            request: HttpRequest,
            *args: Any,
            **kwargs: Any,
        ) -> HttpResponseBase:
            return self._handle_sync(
                request,
                sync_get_response,
                args,
                kwargs,
            )

        return dispatch

    def _handle_sync(
        self,
        request: HttpRequest,
        get_response: _GetResponse,
        view_args: _ViewArgs,
        view_kwargs: _ViewKwargs,
    ) -> HttpResponseBase:
        idempotency_key = self._idempotency_key(request)
        if idempotency_key is None:
            return get_response(request, *view_args, **view_kwargs)

        fingerprint = self._fingerprint(request)
        cache_key = self.storage.make_key(
            self._scope(request),
            idempotency_key,
        )

        token = self._acquire_sync(request, cache_key, fingerprint)
        if not isinstance(token, str):
            return token

        try:
            response = get_response(request, *view_args, **view_kwargs)
        except Exception:
            self.storage.release(cache_key, token)
            raise

        return self._finalize_sync(response, cache_key, token, fingerprint)

    def _finalize_sync(
        self,
        response: HttpResponseBase,
        cache_key: str,
        token: str,
        fingerprint: str,
    ) -> HttpResponseBase:
        """Store the finished response, or release the lock."""
        # Streaming responses can't be buffered and 5xx (by default)
        # must stay retryable, so release the lock in both cases:
        if isinstance(response, StreamingHttpResponse):
            self.storage.release(cache_key, token)
            return response
        if not self._should_store(response):
            self.storage.release(cache_key, token)
            return response

        self.storage.store(
            cache_key,
            _complete_record(fingerprint, self._serialize(response)),
            self.ttl,
        )
        return response

    def _acquire_sync(
        self,
        request: HttpRequest,
        cache_key: str,
        fingerprint: str,
    ) -> str | HttpResponse:
        """Acquire the processing lock, or return a replay/conflict response."""
        existing = self.storage.get_record(cache_key)
        if existing is not None:
            return self._replay_or_conflict(request, existing, fingerprint)

        token = uuid.uuid4().hex
        for _ in range(2):
            if self.storage.acquire(
                cache_key,
                _processing_record(fingerprint, token),
                self.lock_timeout,
            ):
                return token
            # A concurrent request may have stored something
            # between our `get` and `acquire`:
            record = self.storage.get_record(cache_key)
            if record is not None:
                return self._replay_or_conflict(
                    request,
                    record,
                    fingerprint,
                )
            # The record expired in between, try to acquire once more.
        return self._conflict(_IN_PROGRESS_MESSAGE)

    async def _handle_async(
        self,
        request: HttpRequest,
        get_response: _AsyncGetResponse,
        view_args: _ViewArgs,
        view_kwargs: _ViewKwargs,
    ) -> HttpResponseBase:
        idempotency_key = self._idempotency_key(request)
        if idempotency_key is None:
            return await get_response(request, *view_args, **view_kwargs)

        fingerprint = self._fingerprint(request)
        cache_key = self.storage.make_key(
            self._scope(request),
            idempotency_key,
        )

        token = await self._acquire_async(request, cache_key, fingerprint)
        if not isinstance(token, str):
            return token

        try:
            response = await get_response(request, *view_args, **view_kwargs)
        except Exception:
            await self.storage.arelease(cache_key, token)
            raise

        return await self._finalize_async(
            response,
            cache_key,
            token,
            fingerprint,
        )

    async def _finalize_async(
        self,
        response: HttpResponseBase,
        cache_key: str,
        token: str,
        fingerprint: str,
    ) -> HttpResponseBase:
        """Async version of :meth:`_finalize_sync`."""
        if isinstance(response, StreamingHttpResponse):
            await self.storage.arelease(cache_key, token)
            return response
        if not self._should_store(response):
            await self.storage.arelease(cache_key, token)
            return response

        await self.storage.astore(
            cache_key,
            _complete_record(fingerprint, self._serialize(response)),
            self.ttl,
        )
        return response

    async def _acquire_async(
        self,
        request: HttpRequest,
        cache_key: str,
        fingerprint: str,
    ) -> str | HttpResponse:
        """Async version of :meth:`_acquire_sync`."""
        existing = await self.storage.aget_record(cache_key)
        if existing is not None:
            return self._replay_or_conflict(request, existing, fingerprint)

        token = uuid.uuid4().hex
        for _ in range(2):
            if await self.storage.aacquire(
                cache_key,
                _processing_record(fingerprint, token),
                self.lock_timeout,
            ):
                return token
            record = await self.storage.aget_record(cache_key)
            if record is not None:
                return self._replay_or_conflict(
                    request,
                    record,
                    fingerprint,
                )
        return self._conflict(_IN_PROGRESS_MESSAGE)

    def _idempotency_key(self, request: HttpRequest) -> str | None:
        """Return the key for this request, or ``None`` to skip it."""
        if request.method is None or request.method not in self.methods:
            return None
        key = request.headers.get(self.header)
        if not key:
            return None
        return key

    def _scope(self, request: HttpRequest) -> str:
        if self.scope is None:
            return ''
        return self.scope(request) or ''

    def _fingerprint(self, request: HttpRequest) -> str:
        """Hash everything that must stay identical between retries."""
        hasher = hashlib.sha256()
        hasher.update((request.method or '').encode('utf-8'))
        hasher.update(b'\n')
        hasher.update(request.path.encode('utf-8'))
        hasher.update(b'\n')
        hasher.update(request.META.get('QUERY_STRING', '').encode('utf-8'))
        hasher.update(b'\n')
        hasher.update(request.body)
        return hasher.hexdigest()

    def _replay_or_conflict(
        self,
        request: HttpRequest,
        record: IdempotencyRecord,
        fingerprint: str,
    ) -> HttpResponse:
        """Replay a finished request or reject a conflicting one."""
        if record['fingerprint'] != fingerprint:
            return self._conflict(_DIFFERENT_REQUEST_MESSAGE)
        if record['status'] == COMPLETE:
            return self._replay(request, record['response'])
        return self._conflict(_IN_PROGRESS_MESSAGE)

    def _conflict(self, message: str) -> HttpResponse:
        return build_response(
            self.serializer,
            raw_data=format_error(message),
            status_code=self.conflict_status,
        )

    def _replay(
        self,
        request: HttpRequest,
        stored: StoredHttpResponse,
    ) -> HttpResponse:
        """Rebuild the original response from its stored parts."""
        response = HttpResponse(
            content=base64.b64decode(stored['body']),
            status=stored['status_code'],
            reason=stored['reason'],
        )
        for name, header_value in stored['headers']:
            response[name] = header_value
        for cookie in stored['cookies']:
            response.cookies.load(cookie)
        # Allows views, logging and metrics to tell replays apart:
        request.idempotency_replayed = True  # type: ignore[attr-defined]
        return response

    def _serialize(self, response: HttpResponseBase) -> StoredHttpResponse:
        """Serialize a finished response for storage."""
        # Streaming responses are released earlier, this always holds:
        assert isinstance(response, HttpResponse)  # noqa: S101
        return {
            'status_code': response.status_code,
            'reason': response.reason_phrase,
            'body': base64.b64encode(response.content).decode('ascii'),
            'headers': [
                (name, header_value)
                for name, header_value in response.items()
                if name.lower() not in _SKIPPED_HEADERS
            ],
            'cookies': [
                morsel.OutputString() for morsel in response.cookies.values()
            ],
        }

    def _should_store(self, response: HttpResponseBase) -> bool:
        if self.should_store is not None:
            return self.should_store(response)
        # Transient server errors must stay retryable with the same key:
        return response.status_code < HTTPStatus.INTERNAL_SERVER_ERROR
