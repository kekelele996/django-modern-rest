"""
Market quotes resumable SSE stream.

Wire format of a regular quote event (unchanged for new connections)::

    id: 42
    event: quote
    data: {'symbol': 'AAPL', 'price': 178.23}

The ``id`` field carries the per-user increasing sequence number.
The browser ``EventSource`` API automatically remembers it and sends
it back as the ``Last-Event-ID`` request header on reconnect.

Resume protocol:

- no ``Last-Event-ID`` -> live stream, no replay
- ``Last-Event-ID`` inside the last 100 events -> missed events are
  replayed first, strictly after the reported id (never replaying it),
  followed by the live tail
- ``Last-Event-ID`` too old (or ahead of the server) -> a single
  ``resume_failed`` event is sent (without an ``id`` so the client's
  position is not moved); the client must fetch a fresh snapshot and
  resync, live events still follow for its own user only
"""

import types
from collections.abc import AsyncIterator, Mapping
from typing import Final, Literal, TypeAlias, final

import pydantic

from dmr import Body, Controller, Headers, Query
from dmr.plugins.pydantic import PydanticSerializer
from dmr.streaming import modify as streaming_modify
from dmr.streaming.sse import SSEController, SSEvent
from server.apps.quotes.broker import (
    DEFAULT_HISTORY_SIZE,
    ResumableBroker,
    ResumeOutcome,
    ResumeReason,
)

#: Wire values of the ``resume_failed`` event's ``reason`` field.
_ResumeReasonWire = Literal['history_expired', 'sequence_ahead']

#: Internal resume reasons mapped to their wire values.
_RESUME_REASON_TO_WIRE: Final[Mapping[ResumeReason, _ResumeReasonWire]] = (
    types.MappingProxyType({
        ResumeReason.HISTORY_EXPIRED: 'history_expired',
        ResumeReason.SEQUENCE_AHEAD: 'sequence_ahead',
    })
)


@final
class Quote(pydantic.BaseModel):
    """A single market quote, this is the original page event payload."""

    symbol: str = pydantic.Field(min_length=1)
    price: float = pydantic.Field(gt=0)


@final
class _ResumeFailed(pydantic.BaseModel):
    """
    Explicit signal that the stream cannot be resumed.

    It is rendered as ``event: resume_failed`` and tells the client
    that the gap can no longer be filled from the server's history.
    """

    type: Literal['resume_failed'] = 'resume_failed'
    reason: Literal['history_expired', 'sequence_ahead']
    last_event_id: int
    oldest_event_id: int | None
    newest_event_id: int


@final
class _PublishedQuote(pydantic.BaseModel):
    """Result of publishing a quote: the assigned sequence number."""

    id: int
    quote: Quote


@final
class _UserQuery(pydantic.BaseModel):
    # Query parameter, because the native EventSource API cannot set
    # custom headers: it always uses GET and passes cookies / query.
    user_id: int = pydantic.Field(gt=0)


@final
class _ReconnectHeaders(pydantic.BaseModel):
    # Sent automatically by EventSource on reconnect.
    last_event_id: int | None = pydantic.Field(
        default=None,
        alias='Last-Event-ID',
    )


_StreamEvent: TypeAlias = 'SSEvent[Quote | _ResumeFailed]'

#: Process-wide broker, one independent stream per user id.
quotes_broker: ResumableBroker[Quote] = ResumableBroker[Quote](
    history_size=DEFAULT_HISTORY_SIZE,
)


class QuoteStreamController(SSEController[PydanticSerializer]):
    """Per-user market quotes stream with replay on reconnect."""

    # SSE is ASGI-only by design and cannot be called over WSGI,
    # which the OpenAPI fuzzing tests use. The endpoint itself is
    # covered by the ASGI integration tests in `test_quotes/`.
    @streaming_modify(ignore_from_spec=True)
    async def get(
        self,
        parsed_query: Query[_UserQuery],
        parsed_headers: Headers[_ReconnectHeaders],
    ) -> AsyncIterator[_StreamEvent]:
        return self._stream_quotes(
            parsed_query.user_id,
            parsed_headers.last_event_id,
        )

    async def _stream_quotes(
        self,
        user_id: int,
        last_seq: int | None,
    ) -> AsyncIterator[_StreamEvent]:
        async with quotes_broker.subscribe(user_id, last_seq) as subscription:
            if (
                subscription.outcome is ResumeOutcome.IMPOSSIBLE
                and subscription.reason is not None
                and last_seq is not None
            ):
                # Explicit "cannot resume" notice. No `id` is set on
                # purpose: this technical event must not overwrite the
                # client's last position. The client should resync with
                # a fresh snapshot on its own.
                yield SSEvent(
                    _ResumeFailed(
                        reason=_RESUME_REASON_TO_WIRE[subscription.reason],
                        last_event_id=last_seq,
                        oldest_event_id=subscription.oldest_seq,
                        newest_event_id=subscription.newest_seq,
                    ),
                    event='resume_failed',
                )

            # Replay exactly the events the client missed,
            # then continue with the live tail.
            for seq, quote in subscription.replay:
                yield SSEvent(quote, event='quote', id=seq)

            async for seq, quote in subscription.events:
                yield SSEvent(quote, event='quote', id=seq)


class QuotePublishController(Controller[PydanticSerializer]):
    """Publishing endpoint, used by the quote feed and by the tests."""

    async def post(
        self,
        parsed_query: Query[_UserQuery],
        parsed_body: Body[Quote],
    ) -> _PublishedQuote:
        seq = await quotes_broker.publish(parsed_query.user_id, parsed_body)
        return _PublishedQuote(id=seq, quote=parsed_body)
