from typing import Final, Literal, NotRequired

from typing_extensions import TypedDict

#: The first request holding the key is still executing.
PROCESSING: Final = 'processing'

#: The first request has finished and its response is stored.
COMPLETE: Final = 'complete'


class StoredHttpResponse(TypedDict):
    """
    Serialized representation of a finished response.

    Attributes:
        status_code: HTTP status code of the original response.
        reason: Reason phrase of the original response.
        body: Raw response body, encoded as base64.
        headers: All response headers except the hop-by-hop ones,
            stored as ``(name, value)`` pairs.
        cookies: All ``Set-Cookie`` entries, each serialized
            with :meth:`http.cookies.Morsel.OutputString`.

    """

    status_code: int
    reason: str
    body: str
    headers: list[tuple[str, str]]
    cookies: list[str]


class ProcessingRecord(TypedDict):
    """A request with this key is still being executed."""

    status: Literal['processing']
    fingerprint: str
    token: NotRequired[str]


class CompleteRecord(TypedDict):
    """The request has finished and its response is stored for replay."""

    status: Literal['complete']
    fingerprint: str
    response: StoredHttpResponse


#: What is stored in the cache for a single idempotency key.
IdempotencyRecord = ProcessingRecord | CompleteRecord
