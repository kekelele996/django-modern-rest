"""
Tests for the resumable market quotes SSE stream.

Covers the reconnect contract:

- every quote event carries a strictly increasing sequence number
- reconnect with ``Last-Event-ID`` replays exactly the missed events,
  never replaying the last one and never skipping middle events
- the last 100 events are replayable
- older (and future / stale) sequence numbers get an explicit
  ``resume_failed`` event instead of a silently broken stream
- brand-new connections keep the original live event format
- two different users never receive each other's events
"""

import asyncio
import contextlib
import json
from collections.abc import AsyncIterator
from dataclasses import dataclass
from http import HTTPStatus
from typing import Any, Final

import pytest
from django.http import HttpResponse, HttpResponseBase

from dmr.streaming.stream import StreamingResponse
from dmr.test import DMRAsyncRequestFactory
from server.apps.quotes.broker import DEFAULT_HISTORY_SIZE  # type: ignore[import-not-found]
from server.apps.quotes.views import (  # type: ignore[import-not-found]
    Quote,
    QuotePublishController,
    QuoteStreamController,
    quotes_broker,
)

#: User ids used by individual tests, keeps intent explicit.
USER_A = 101
USER_B = 202
PUBLISH_USER = 303

#: Sample price, avoids magic numbers in assertions.
_APPLE_PRICE: Final = 178.23

#: SSE field prefixes, order follows the wire rendering.
_FIELD_PREFIXES: Final = (
    ('id', 'id: '),
    ('event', 'event: '),
    ('data', 'data: '),
)


@dataclass(frozen=True, slots=True)
class _Block:
    """One parsed SSE block, comment / ping blocks parse to ``None``."""

    id: int | None
    event: str | None
    data: dict[str, Any] | None


def _parse_block(raw: bytes) -> _Block | None:
    lines = [line.decode() for line in raw.splitlines() if line.strip()]
    if not lines or all(line.startswith(':') for line in lines):
        return None  # SSE comment, e.g. the `: ping` keep-alive.

    fields: dict[str, list[str]] = {
        'id': [],
        'event': [],
        'data': [],
    }
    for line in lines:
        for name, prefix in _FIELD_PREFIXES:
            if line.startswith(prefix):
                fields[name].append(line.removeprefix(prefix))

    return _Block(
        id=int(fields['id'][0]) if fields['id'] else None,
        event=fields['event'][0] if fields['event'] else None,
        data=(
            json.loads('\n'.join(fields['data'])) if fields['data'] else None
        ),
    )


class _StreamReader:
    """
    Pump of one open SSE connection.

    Iteration starts immediately on creation, so the broker subscription
    is registered *before* tests publish anything - exactly like a real
    browser holding an open EventSource connection.
    """

    __slots__ = ('_agen', '_closed', '_queue', '_task')

    def __init__(self, response: StreamingResponse) -> None:
        # `__aiter__` is runtime-only on `StreamingResponse`,
        # mirroring `tests/infra/streaming.py`.
        self._agen: AsyncIterator[bytes] = aiter(response)  # type: ignore[arg-type]
        self._queue: asyncio.Queue[tuple[_Block, bytes]] = asyncio.Queue()
        self._task = asyncio.create_task(self._pump())
        self._closed = False

    async def aclose(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._task.cancel()
        with contextlib.suppress(
            asyncio.CancelledError,
            StopAsyncIteration,
        ):
            await self._task

    async def expect_silence(self, *, wait: float = 0.05) -> None:
        """No event may arrive within the waiting window."""
        with pytest.raises(TimeoutError):
            async with asyncio.timeout(wait):
                await self._queue.get()

    async def next(self, *, wait: float = 2.0) -> _Block:
        block, _ = await self.next_raw(wait=wait)
        return block

    async def next_raw(
        self,
        *,
        wait: float = 2.0,
    ) -> tuple[_Block, bytes]:
        async with asyncio.timeout(wait):
            return await self._queue.get()

    async def start(self) -> '_StreamReader':
        """Let the pump reach the broker subscription point."""
        # Two event-loop hops: pump -> response producer -> event task.
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        return self

    async def _pump(self) -> None:
        async for chunk in self._agen:
            block = _parse_block(chunk)
            if block is not None:
                await self._queue.put((block, chunk))


async def _collect_blocks(
    reader: _StreamReader,
    count: int,
) -> list[_Block]:
    return [await reader.next() for _ in range(count)]


async def _collect_ids(
    reader: _StreamReader,
    count: int,
) -> list[int]:
    blocks = await _collect_blocks(reader, count)
    ids: list[int] = []
    for block in blocks:
        assert block.id is not None  # quote events always carry an id
        ids.append(block.id)
    return ids


async def _collect_ids_and_close(
    reader: _StreamReader,
    count: int,
) -> list[int]:
    ids = await _collect_ids(reader, count)
    await reader.aclose()
    return ids


async def _next_and_close(reader: _StreamReader) -> _Block:
    block = await reader.next()
    await reader.aclose()
    return block


async def _resume_next_id(
    dmr_async_rf: DMRAsyncRequestFactory,
    user_id: int,
    last_event_id: int,
) -> int:
    reader = await _open_reader(
        dmr_async_rf,
        user_id,
        last_event_id=last_event_id,
    )
    block = await reader.next()
    await reader.aclose()
    assert block.id is not None
    return block.id


async def _open_response(
    dmr_async_rf: DMRAsyncRequestFactory,
    user_id: int,
    *,
    last_event_id: int | str | None = None,
) -> HttpResponseBase:
    headers = (
        {} if last_event_id is None else {'Last-Event-ID': str(last_event_id)}
    )
    request = dmr_async_rf.get(
        f'/api/quotes/stream/?user_id={user_id}',
        headers=headers,
    )
    return await dmr_async_rf.wrap(
        QuoteStreamController.as_view()(request),
    )


async def _open_reader(
    dmr_async_rf: DMRAsyncRequestFactory,
    user_id: int,
    *,
    last_event_id: int | None = None,
) -> _StreamReader:
    response = await _open_response(
        dmr_async_rf,
        user_id,
        last_event_id=last_event_id,
    )
    assert isinstance(response, StreamingResponse)
    return await _StreamReader(response).start()


async def _publish_many(user_id: int, count: int) -> None:
    for seq in range(1, count + 1):
        await quotes_broker.publish(
            user_id,
            Quote(symbol=f'SYM{seq}', price=float(seq)),
        )


async def _publish_seqs(user_id: int, seqs: list[int]) -> None:
    for seq in seqs:
        await quotes_broker.publish(
            user_id,
            Quote(symbol=f'SYM{seq}', price=float(seq)),
        )


def _quote(seq: int) -> Quote:
    return Quote(symbol=f'SYM{seq}', price=float(seq))


async def _assert_live_isolation(dmr_async_rf: DMRAsyncRequestFactory) -> None:
    reader_a = await _open_reader(dmr_async_rf, USER_A)
    await _publish_many(USER_B, 4)
    await reader_a.expect_silence()
    await _publish_many(USER_A, 2)
    assert await _collect_ids_and_close(reader_a, 2) == [1, 2]


async def _assert_replay_isolation(
    dmr_async_rf: DMRAsyncRequestFactory,
) -> None:
    id_a = await _resume_next_id(dmr_async_rf, USER_A, last_event_id=1)
    id_b = await _resume_next_id(dmr_async_rf, USER_B, last_event_id=3)
    assert (id_a, id_b) == (2, 4)


@pytest.fixture(autouse=True)
async def _clear_quotes_broker() -> AsyncIterator[None]:
    """Every test starts with an empty broker."""
    await quotes_broker.clear()
    yield
    await quotes_broker.clear()


async def test_fresh_connection_keeps_original_event_format(  # noqa: WPS118
    dmr_async_rf: DMRAsyncRequestFactory,
) -> None:
    """New connections get live events in the plain SSE format + id."""
    reader = await _open_reader(dmr_async_rf, USER_A)

    # Nothing happened yet, no history is pushed to a fresh connection:
    await reader.expect_silence()

    seq = await quotes_broker.publish(
        USER_A,
        Quote(symbol='AAPL', price=_APPLE_PRICE),
    )
    assert seq == 1

    block, raw = await reader.next_raw()
    assert raw == (
        b'id: 1\r\nevent: quote\r\n'
        b'data: {"symbol":"AAPL","price":178.23}\r\n\r\n'
    )
    assert block == _Block(
        id=1,
        event='quote',
        data={'symbol': 'AAPL', 'price': _APPLE_PRICE},
    )
    await reader.aclose()


async def test_events_have_strictly_increasing_ids(
    dmr_async_rf: DMRAsyncRequestFactory,
) -> None:
    """Sequence numbers are dense and increasing per user."""
    reader = await _open_reader(dmr_async_rf, USER_A)

    await _publish_many(USER_A, 3)
    blocks = await _collect_blocks(reader, 3)

    assert [block.id for block in blocks] == [1, 2, 3]
    assert {block.event for block in blocks} == {'quote'}
    assert [block.data for block in blocks] == [
        {'symbol': 'SYM1', 'price': 1.0},
        {'symbol': 'SYM2', 'price': 2.0},
        {'symbol': 'SYM3', 'price': 3.0},
    ]
    await reader.aclose()


async def test_reconnect_replays_missed_events_without_gap_or_duplicate(  # noqa: WPS118
    dmr_async_rf: DMRAsyncRequestFactory,
) -> None:
    """After a drop, the client receives exactly events 3..7 once."""
    await _publish_many(USER_A, 5)

    # Reconnect: the client last saw event 2.
    reader = await _open_reader(dmr_async_rf, USER_A, last_event_id=2)

    assert await _collect_ids(reader, 3) == [3, 4, 5]

    # Live tail continues right after the replay:
    await _publish_seqs(USER_A, [6, 7])
    assert await _collect_ids_and_close(reader, 2) == [6, 7]


async def test_reconnect_at_newest_event_does_not_replay_it(  # noqa: WPS118
    dmr_async_rf: DMRAsyncRequestFactory,
) -> None:
    """Reporting the newest id must not replay the last event again."""
    await _publish_many(USER_A, 4)

    reader = await _open_reader(dmr_async_rf, USER_A, last_event_id=4)
    await reader.expect_silence()

    await quotes_broker.publish(USER_A, _quote(5))
    assert await _collect_ids_and_close(reader, 1) == [5]


async def test_last_hundred_events_are_replayable(
    dmr_async_rf: DMRAsyncRequestFactory,
) -> None:
    """Exactly the last 100 events can be backfilled on reconnect."""
    await _publish_many(USER_A, DEFAULT_HISTORY_SIZE + 5)

    # Oldest retained event is 6, so last_event_id=5 still allows
    # resuming with a full 100-event replay:
    reader = await _open_reader(dmr_async_rf, USER_A, last_event_id=5)

    blocks = await _collect_blocks(reader, DEFAULT_HISTORY_SIZE)
    assert [block.id for block in blocks] == list(
        range(6, DEFAULT_HISTORY_SIZE + 6),
    )
    assert blocks[-1].data == {'symbol': 'SYM105', 'price': 105.0}
    await reader.aclose()


async def test_reconnect_beyond_window_gets_explicit_failure(  # noqa: WPS118
    dmr_async_rf: DMRAsyncRequestFactory,
) -> None:
    """Too old ids get one `resume_failed` event, then live continues."""
    await _publish_many(USER_A, DEFAULT_HISTORY_SIZE + 5)

    reader = await _open_reader(dmr_async_rf, USER_A, last_event_id=4)

    failure, raw = await reader.next_raw()
    assert failure.event == 'resume_failed'
    # The failure notice must not advance the client's event position:
    assert failure.id is None
    assert failure.data == {
        'type': 'resume_failed',
        'reason': 'history_expired',
        'last_event_id': 4,
        'oldest_event_id': 6,
        'newest_event_id': 105,
    }
    assert b'id: ' not in raw

    # The stream is still usable: future live events keep coming.
    await quotes_broker.publish(USER_A, _quote(DEFAULT_HISTORY_SIZE + 6))
    block = await _next_and_close(reader)
    assert block.id == DEFAULT_HISTORY_SIZE + 6
    assert block.event == 'quote'


async def test_reconnect_with_future_id_gets_explicit_failure(  # noqa: WPS118
    dmr_async_rf: DMRAsyncRequestFactory,
) -> None:
    """An id ahead of the server cannot be resumed either."""
    await _publish_many(USER_A, 3)

    reader = await _open_reader(dmr_async_rf, USER_A, last_event_id=100)

    failure = await _next_and_close(reader)
    assert failure.event == 'resume_failed'
    assert failure.id is None
    assert failure.data == {
        'type': 'resume_failed',
        'reason': 'sequence_ahead',
        'last_event_id': 100,
        'oldest_event_id': 1,
        'newest_event_id': 3,
    }


async def test_stale_id_after_server_restart_gets_explicit_failure(  # noqa: WPS118
    dmr_async_rf: DMRAsyncRequestFactory,
) -> None:
    """With an empty history even id 1+ cannot be resumed."""
    reader = await _open_reader(dmr_async_rf, USER_A, last_event_id=7)

    failure = await _next_and_close(reader)
    assert failure.event == 'resume_failed'
    assert failure.data == {
        'type': 'resume_failed',
        'reason': 'history_expired',
        'last_event_id': 7,
        'oldest_event_id': None,
        'newest_event_id': 0,
    }


async def test_two_users_never_share_events(
    dmr_async_rf: DMRAsyncRequestFactory,
) -> None:
    """User A must neither receive user B's live nor replayed events."""
    await _assert_live_isolation(dmr_async_rf)
    await _assert_replay_isolation(dmr_async_rf)


async def test_same_user_fanout_delivers_to_both_connections(  # noqa: WPS118
    dmr_async_rf: DMRAsyncRequestFactory,
) -> None:
    """Two tabs of the same user both receive the same event once each."""
    readers = [
        await _open_reader(dmr_async_rf, USER_A),
        await _open_reader(dmr_async_rf, USER_A),
    ]

    await quotes_broker.publish(USER_A, Quote(symbol='AAPL', price=1))
    blocks = [await _next_and_close(reader) for reader in readers]

    assert blocks[0] == blocks[1]
    assert blocks[0].id == 1


async def test_disconnect_removes_subscriber(
    dmr_async_rf: DMRAsyncRequestFactory,
) -> None:
    """Closing the SSE connection must unsubscribe from the broker."""
    reader = await _open_reader(dmr_async_rf, USER_A)

    await quotes_broker.publish(USER_A, _quote(1))
    assert (await reader.next()).id == 1
    await reader.aclose()

    state = quotes_broker._streams[USER_A]
    assert state.queues == set()


async def test_concurrent_publish_orders_events_without_duplicates(  # noqa: WPS118
    dmr_async_rf: DMRAsyncRequestFactory,
) -> None:
    """Publishing while connected yields each seq exactly once, ordered."""
    reader = await _open_reader(dmr_async_rf, USER_A)

    total = 50
    await asyncio.gather(
        *[
            quotes_broker.publish(USER_A, _quote(seq))
            for seq in range(1, total + 1)
        ],
    )

    ids = await _collect_ids_and_close(reader, total)
    assert ids == list(range(1, total + 1))


async def test_publish_endpoint_assigns_seq_and_feeds_stream(  # noqa: WPS118
    dmr_async_rf: DMRAsyncRequestFactory,
) -> None:
    """The HTTP POST producer is part of the same per-user resume stream."""
    request = dmr_async_rf.post(
        f'/api/quotes/publish/?user_id={PUBLISH_USER}',
        data={'symbol': 'AAPL', 'price': 178.5},
    )
    response = await dmr_async_rf.wrap(
        QuotePublishController.as_view()(request),
    )
    assert isinstance(response, HttpResponse)
    assert not isinstance(response, StreamingResponse)
    assert response.status_code == HTTPStatus.CREATED
    assert json.loads(response.content) == {
        'id': 1,
        'quote': {'symbol': 'AAPL', 'price': 178.5},
    }

    # A reconnect for that user sees the HTTP-published event in history:
    reader = await _open_reader(
        dmr_async_rf,
        PUBLISH_USER,
        last_event_id=0,
    )
    block = await _next_and_close(reader)
    assert block.id == 1
    assert block.event == 'quote'
    assert block.data == {'symbol': 'AAPL', 'price': 178.5}


async def test_invalid_last_event_id_is_rejected_before_streaming(  # noqa: WPS118
    dmr_async_rf: DMRAsyncRequestFactory,
) -> None:
    """A non-integer `Last-Event-ID` is a regular 400, not a broken SSE."""
    response = await _open_response(dmr_async_rf, USER_A, last_event_id='abc')
    assert isinstance(response, HttpResponse)
    assert not isinstance(response, StreamingResponse)
    assert response.status_code == HTTPStatus.BAD_REQUEST
    assert json.loads(response.content)['detail']
