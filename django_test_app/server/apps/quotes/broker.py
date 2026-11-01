"""
In-memory resumable event broker for the market quotes page.

It solves the reconnect problems of a plain SSE stream:

1. Every published event gets a strictly increasing sequence number,
   so clients can always tell which events are new
2. On reconnect the client reports its last received sequence number
   (via the standard ``Last-Event-ID`` header) and receives exactly
   the events it missed - never the last one twice, never a gap
3. The most recent ``history_size`` events (100 by default)
   are kept for replay
4. When the missed events have already expired from the history,
   the subscription reports :class:`ResumeOutcome.IMPOSSIBLE`
   instead of silently reconnecting into the middle of the stream
5. Streams of different keys (users) never share state

The broker is async-native and lives in a single process.
It is intentionally simple: a real deployment with multiple
workers would replace it with a shared broker (Redis / etc),
while keeping the same resume protocol.
"""

import asyncio
import contextlib
import enum
from collections import deque
from collections.abc import AsyncGenerator, AsyncIterator, Hashable
from dataclasses import dataclass
from typing import Generic, TypeVar

_EventT = TypeVar('_EventT')

#: A single buffered or live event: ``(sequence number, payload)``.
_EventEntry = tuple[int, _EventT]
#: Queue of one subscriber's live events.
_EventQueue = asyncio.Queue[_EventEntry[_EventT]]
#: Ordered replay snapshot.
_Replay = tuple[_EventEntry[_EventT], ...]

#: Default number of events kept for replay on reconnect.
DEFAULT_HISTORY_SIZE = 100


class ResumeOutcome(enum.Enum):
    """What the broker decided about a new subscription."""

    #: Client did not report any sequence number: live stream only.
    FRESH = 'fresh'

    #: Client reported a sequence number inside the replay window.
    RESUMED = 'resumed'

    #: Missed events are gone (or the id is ahead of the server):
    #: the client must do a full resync.
    IMPOSSIBLE = 'impossible'


class ResumeReason(enum.Enum):
    """Why a resume turned out to be :attr:`ResumeOutcome.IMPOSSIBLE`."""

    #: The next event after the reported id is older than the history.
    HISTORY_EXPIRED = 'history_expired'

    #: The reported id is newer than anything the server knows.
    SEQUENCE_AHEAD = 'sequence_ahead'


#: ``(outcome, reason, replay)`` decision of a resume attempt.
_ResumeResult = tuple[ResumeOutcome, ResumeReason | None, _Replay[_EventT]]


@dataclass(frozen=True, slots=True)
class Subscription(Generic[_EventT]):
    """
    Result of subscribing to a stream.

    Attributes:
        outcome: Whether the subscription is fresh, resumed or impossible.
        reason: Why the resume is impossible, ``None`` otherwise.
        replay: Events missed since ``last_seq`` (ordered, oldest first).
            They must be sent before reading :attr:`events`.
        oldest_seq: Sequence number of the oldest event still in the
            replay window, ``None`` if the history is empty.
        newest_seq: Sequence number of the newest published event,
            ``0`` if nothing has ever been published.
        events: Live events, each as ``(sequence_number, payload)``.

    """

    outcome: ResumeOutcome
    reason: ResumeReason | None
    replay: _Replay[_EventT]
    oldest_seq: int | None
    newest_seq: int
    events: AsyncIterator[_EventEntry[_EventT]]


@dataclass(slots=True)
class _StreamState(Generic[_EventT]):
    """All per-key state: sequence counter, history and subscribers."""

    history: deque[_EventEntry[_EventT]]
    queues: set[_EventQueue[_EventT]]
    lock: asyncio.Lock
    newest_seq: int = 0


def _resolve_resume(
    state: _StreamState[_EventT],
    last_seq: int | None,
) -> _ResumeResult[_EventT]:
    if last_seq is None:
        # Brand-new connection: live stream only, no replay.
        return ResumeOutcome.FRESH, None, ()

    if state.newest_seq == 0 or last_seq < 0:
        # Nothing is remembered (e.g. the server restarted),
        # or the id is invalid: nothing can be resumed safely.
        return ResumeOutcome.IMPOSSIBLE, ResumeReason.HISTORY_EXPIRED, ()

    if last_seq > state.newest_seq:
        # The id comes from the future or from a different stream
        # incarnation, we cannot prove there is no gap.
        return ResumeOutcome.IMPOSSIBLE, ResumeReason.SEQUENCE_AHEAD, ()

    oldest_seq = state.history[0][0]
    if last_seq + 1 < oldest_seq:
        # The first event the client needs already expired.
        return ResumeOutcome.IMPOSSIBLE, ResumeReason.HISTORY_EXPIRED, ()

    # Resume strictly AFTER last_seq: when the client already has
    # the newest event, `replay` is simply empty (no last-event
    # replay) and it continues with the live tail.
    replay = tuple(entry for entry in state.history if entry[0] > last_seq)
    return ResumeOutcome.RESUMED, None, replay


class ResumableBroker(Generic[_EventT]):
    """
    Fan-out broker with per-key increasing sequences and bounded replay.

    Each key (in our case - a user id) gets a fully independent stream:
    its own sequence counter, its own event history and its own set
    of live subscribers. Events published for one key can never reach
    subscribers of another key.
    """

    __slots__ = ('_history_size', '_streams', '_streams_lock')

    def __init__(self, history_size: int = DEFAULT_HISTORY_SIZE) -> None:
        if history_size < 1:
            raise ValueError('history_size must be at least 1')
        self._history_size = history_size
        self._streams: dict[Hashable, _StreamState[_EventT]] = {}
        self._streams_lock = asyncio.Lock()

    async def publish(self, key: Hashable, payload: _EventT) -> int:
        """
        Publish ``payload`` to the stream identified by ``key``.

        Assigns the next sequence number, appends the event to the replay
        history and delivers it to every currently connected subscriber.
        Returns the assigned sequence number.
        """
        state = await self._get_state(key)
        async with state.lock:
            state.newest_seq += 1
            seq = state.newest_seq
            entry: _EventEntry[_EventT] = (seq, payload)
            state.history.append(entry)
            for queue in state.queues:
                queue.put_nowait(entry)
        return seq

    @contextlib.asynccontextmanager
    async def subscribe(
        self,
        key: Hashable,
        last_seq: int | None,
    ) -> AsyncGenerator[Subscription[_EventT], None]:
        """
        Subscribe to the stream of ``key``.

        ``last_seq`` is the last sequence number the client received,
        or ``None`` for a brand-new connection. The returned
        :class:`Subscription` carries the resume decision and the events
        that must be replayed; its ``events`` iterator then yields
        all live events published afterwards.

        Registration and replay snapshot are taken under the same lock
        as publishing, so an event can neither be missing from both
        ``replay`` and ``events`` nor appear in both.
        """
        state = await self._get_state(key)
        queue: _EventQueue[_EventT] = asyncio.Queue()
        async with state.lock:
            outcome, reason, replay = _resolve_resume(state, last_seq)
            state.queues.add(queue)

        try:
            yield Subscription(
                outcome=outcome,
                reason=reason,
                replay=replay,
                oldest_seq=state.history[0][0] if state.history else None,
                newest_seq=state.newest_seq,
                events=_queue_iter(queue),
            )
        finally:
            async with state.lock:
                state.queues.discard(queue)

    async def clear(self, key: Hashable | None = None) -> None:
        """
        Drop stream state.

        Without arguments clears every stream - mainly useful in tests.
        """
        async with self._streams_lock:
            if key is None:
                self._streams.clear()
            else:
                self._streams.pop(key, None)

    async def _get_state(
        self,
        key: Hashable,
    ) -> _StreamState[_EventT]:
        async with self._streams_lock:
            state = self._streams.get(key)
            if state is None:
                state = _StreamState(
                    history=deque(maxlen=self._history_size),
                    queues=set(),
                    lock=asyncio.Lock(),
                )
                self._streams[key] = state
            return state


async def _queue_iter(
    queue: _EventQueue[_EventT],
) -> AsyncIterator[_EventEntry[_EventT]]:
    while True:
        yield await queue.get()
