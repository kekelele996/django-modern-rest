import hmac
import math
import uuid
from pathlib import Path
from typing import Final, Literal

from django.core.files.uploadedfile import UploadedFile
from django.db import DatabaseError, transaction

from server.apps.video_uploads import storage
from server.apps.video_uploads.models import (
    UploadedChunk,
    Video,
    VideoUploadSession,
)
from server.apps.video_uploads.serializers import (
    SessionInitSchema,
    SessionStatusSchema,
    VideoSchema,
)

_CHECKSUM_MISMATCH_ERROR: Final = 'checksum_mismatch'

ChunkState = Literal['stored', 'duplicate', 'replaced']


class SessionNotFoundError(Exception):
    """Upload session with the given id does not exist."""

    def __init__(self, session_id: uuid.UUID) -> None:
        super().__init__(f'Upload session {session_id} does not exist')
        self.session_id = session_id


class SessionCompletedError(Exception):
    """Session is already completed and can no longer change."""


class ChunkIndexOutOfRangeError(Exception):
    """Chunk index is outside the ``0..total_chunks`` range."""

    def __init__(self, index: int, total_chunks: int) -> None:
        super().__init__(
            f'Chunk index {index} is out of range, '
            f'there are only {total_chunks} chunks',
        )
        self.index = index
        self.total_chunks = total_chunks


class ChunkSizeMismatchError(Exception):
    """Received chunk does not have the expected deterministic size."""

    def __init__(self, index: int, actual: int, expected: int) -> None:
        super().__init__(
            f'Chunk {index} has size {actual}, expected {expected}',
        )
        self.index = index
        self.actual = actual
        self.expected = expected


class MissingChunksError(Exception):
    """Completion requested while some chunks were never received."""

    def __init__(self, missing: list[int]) -> None:
        super().__init__(f'Missing chunks: {missing}')
        self.missing = missing


class ChecksumMismatchError(Exception):
    """Assembled file digest does not match the promised checksum."""

    def __init__(self, actual: str, expected: str) -> None:
        super().__init__(
            f'Checksum mismatch: got {actual}, expected {expected}',
        )
        self.actual = actual
        self.expected = expected


def init_session(payload: SessionInitSchema) -> VideoUploadSession:
    """Create an empty resumable upload session."""
    total_chunks = math.ceil(payload.total_size / payload.chunk_size)
    return VideoUploadSession.objects.create(
        filename=payload.filename,
        total_size=payload.total_size,
        chunk_size=payload.chunk_size,
        total_chunks=total_chunks,
        checksum=payload.checksum,
    )


def get_session(session_id: uuid.UUID) -> VideoUploadSession:
    """Return the session or raise a domain ``not found`` error."""
    session = VideoUploadSession.objects.filter(id=session_id).first()
    if session is None:
        raise SessionNotFoundError(session_id)
    return session


def session_status(session: VideoUploadSession) -> SessionStatusSchema:
    """Build the status payload: what is received and what is missing."""
    chunks = list(session.chunks.values('index', 'size'))
    received = sorted(chunk['index'] for chunk in chunks)
    received_set = set(received)
    missing = [
        index
        for index in range(session.total_chunks)
        if index not in received_set
    ]
    return SessionStatusSchema(
        id=session.id,
        filename=session.filename,
        total_size=session.total_size,
        chunk_size=session.chunk_size,
        total_chunks=session.total_chunks,
        checksum=session.checksum,
        status=session.status,
        received_chunks=received,
        missing_chunks=missing,
        received_size=sum(chunk['size'] for chunk in chunks),
        is_complete=not missing,
        last_error=session.last_error,
        created_at=session.created_at,
        updated_at=session.updated_at,
    )


def receive_chunk(
    session: VideoUploadSession,
    index: int,
    uploaded_file: UploadedFile,
) -> tuple[UploadedChunk, ChunkState]:
    """
    Persist one chunk and return its state.

    An identical retransmit is a no-op (the stored chunk is never written
    a second time), a retransmit with different content replaces the chunk
    so that failed verifications remain recoverable.
    """
    if session.status == VideoUploadSession.Status.completed:
        raise SessionCompletedError
    if not 0 <= index < session.total_chunks:
        raise ChunkIndexOutOfRangeError(index, session.total_chunks)

    existing = session.chunks.filter(index=index).first()
    if existing is not None:
        return _retransmit(session, existing, uploaded_file)

    checksum, size = storage.store_chunk(str(session.id), index, uploaded_file)
    expected = expected_chunk_size(session, index)
    if size != expected:
        # The bad upload must not leave a partial chunk behind:
        storage.discard_file(storage.chunk_path(str(session.id), index))
        raise ChunkSizeMismatchError(index, size, expected)
    chunk = UploadedChunk.objects.create(
        session=session,
        index=index,
        checksum=checksum,
        size=size,
    )
    return chunk, 'stored'


def complete_session(
    session: VideoUploadSession,
) -> tuple[Video, bool]:
    """
    Assemble and publish the video.

    Returns ``(video, created)``: completing an already completed session
    is idempotent and returns the existing video with ``created=False``.
    """
    if session.status == VideoUploadSession.Status.completed:
        return session.video, False

    missing = session_status(session).missing_chunks
    if missing:
        raise MissingChunksError(missing)

    assembled_path, actual_checksum, actual_size = storage.assemble(
        str(session.id),
        session.total_chunks,
    )
    try:
        return _publish_if_checksum_matches(
            session,
            assembled_path,
            actual_checksum,
            actual_size,
        )
    finally:
        storage.discard_file(assembled_path)


def abort_session(session: VideoUploadSession) -> None:
    """Discard an unfinished session together with its staging files."""
    if session.status == VideoUploadSession.Status.completed:
        raise SessionCompletedError
    storage.discard_staging(str(session.id))
    session.delete()


def expected_chunk_size(session: VideoUploadSession, index: int) -> int:
    """Size a chunk must have given the session geometry."""
    if index < session.total_chunks - 1:
        return session.chunk_size
    return session.total_size - session.chunk_size * (session.total_chunks - 1)


def video_schema(video: Video) -> VideoSchema:
    """Build the published video payload."""
    return VideoSchema(
        id=video.pk,
        session_id=video.session_id,
        filename=video.filename,
        size=video.size,
        checksum=video.checksum,
        url=storage.published_url(video.file.name),
        created_at=video.created_at,
    )


def _retransmit(
    session: VideoUploadSession,
    existing: UploadedChunk,
    uploaded_file: UploadedFile,
) -> tuple[UploadedChunk, ChunkState]:
    checksum, size = storage.hash_upload(uploaded_file)
    if checksum == existing.checksum and size == existing.size:
        # Same payload twice: do not touch the stored chunk at all.
        return existing, 'duplicate'

    expected = expected_chunk_size(session, existing.index)
    if size != expected:
        # A wrong retransmit is rejected without touching the good chunk:
        raise ChunkSizeMismatchError(existing.index, size, expected)
    uploaded_file.seek(0)
    new_checksum, new_size = storage.store_chunk(
        str(session.id),
        existing.index,
        uploaded_file,
    )
    existing.checksum = new_checksum
    existing.size = new_size
    existing.save(update_fields=('checksum', 'size', 'updated_at'))
    return existing, 'replaced'


def _publish_if_checksum_matches(
    session: VideoUploadSession,
    assembled_path: Path,
    actual_checksum: str,
    actual_size: int,
) -> tuple[Video, bool]:
    if actual_size != session.total_size or not hmac.compare_digest(
        actual_checksum,
        session.checksum,
    ):
        # Refuse completion, but keep every chunk in staging so the client
        # can retransmit bad chunks and retry completion.
        session.last_error = _CHECKSUM_MISMATCH_ERROR
        session.save(update_fields=('last_error', 'updated_at'))
        raise ChecksumMismatchError(actual_checksum, session.checksum)

    relative_name = storage.publish(
        str(session.id),
        assembled_path,
        session.filename,
    )
    try:
        with transaction.atomic():
            video = Video.objects.create(
                session=session,
                filename=session.filename,
                file=relative_name,
                size=actual_size,
                checksum=actual_checksum,
            )
            session.status = VideoUploadSession.Status.completed
            session.last_error = ''
            session.save(
                update_fields=('status', 'last_error', 'updated_at'),
            )
    except DatabaseError:
        storage.delete_published(relative_name)
        raise
    storage.discard_staging(str(session.id))
    return video, True
