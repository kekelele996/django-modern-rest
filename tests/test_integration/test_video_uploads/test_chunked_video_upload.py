import hashlib
from http import HTTPStatus
from pathlib import Path

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import IntegrityError
from django.test.client import BOUNDARY, MULTIPART_CONTENT, encode_multipart
from django.urls import reverse

from dmr.test import DMRClient
from server.apps.video_uploads import storage
from server.apps.video_uploads.models import (
    UploadedChunk,
    Video,
    VideoUploadSession,
)

pytestmark = pytest.mark.django_db

_COLLECTION_URL = reverse('api:video_uploads:session_collection')
_FILENAME = 'lesson-30min.mp4'
_CHUNK_SIZE = 512


@pytest.fixture
def upload_root(settings, tmp_path) -> Path:
    """Point all upload storage at a per-test temporary directory."""
    root = tmp_path / 'media'
    settings.MEDIA_ROOT = str(root)
    return root


def _content() -> bytes:
    # 1600 bytes -> 4 chunks of 512 bytes, last one is 64 bytes:
    return b'0123456789abcdef' * 100


def _split(content: bytes, chunk_size: int) -> list[bytes]:
    return [
        content[offset : offset + chunk_size]
        for offset in range(0, len(content), chunk_size)
    ]


def _init_session(
    client: DMRClient,
    content: bytes,
    *,
    chunk_size: int = _CHUNK_SIZE,
    checksum: str | None = None,
    filename: str = _FILENAME,
):
    response = client.post(
        _COLLECTION_URL,
        data={
            'filename': filename,
            'total_size': len(content),
            'chunk_size': chunk_size,
            'checksum': checksum or hashlib.sha256(content).hexdigest(),
        },
    )
    assert response.status_code == HTTPStatus.CREATED, response.content
    return response.json()


def _detail_url(session_id: str) -> str:
    return reverse(
        'api:video_uploads:session_detail',
        kwargs={'session_id': session_id},
    )


def _chunk_url(session_id: str, index: int) -> str:
    return reverse(
        'api:video_uploads:session_chunk',
        kwargs={'session_id': session_id, 'index': index},
    )


def _complete_url(session_id: str) -> str:
    return reverse(
        'api:video_uploads:session_complete',
        kwargs={'session_id': session_id},
    )


def _put_chunk(
    client: DMRClient,
    session_id: str,
    index: int,
    payload: bytes,
):
    # Django's test client only multipart-encodes POST, PUT needs explicit
    # encoding, just like real chunk uploads:
    return client.put(
        _chunk_url(session_id, index),
        data=encode_multipart(
            BOUNDARY,
            {
                'chunk': SimpleUploadedFile(
                    f'chunk-{index}',
                    payload,
                    content_type='application/octet-stream',
                ),
            },
        ),
        content_type=MULTIPART_CONTENT,
    )


def _published_path(upload_root: Path, session_id: str) -> Path:
    return upload_root / 'videos' / session_id / _FILENAME


def _upload_all(
    client: DMRClient,
    session_id: str,
    chunks: list[bytes],
    order: list[int] | None = None,
) -> None:
    for index in order or range(len(chunks)):
        response = _put_chunk(client, session_id, index, chunks[index])
        assert response.status_code == HTTPStatus.OK, response.content
        assert response.json()['state'] == 'stored'


def test_full_upload_out_of_order_publishes_only_when_complete(
    dmr_client: DMRClient,
    upload_root: Path,
) -> None:
    """Chunks arriving out of order are assembled by index at completion."""
    content = _content()
    chunks = _split(content, _CHUNK_SIZE)
    session = _init_session(dmr_client, content)
    session_id = session['id']

    # Nothing is published while the upload is still in progress:
    _upload_all(dmr_client, session_id, chunks, order=[2, 0, 3])
    assert Video.objects.count() == 0
    assert not _published_path(upload_root, session_id).exists()

    status = dmr_client.get(_detail_url(session_id)).json()
    assert status['received_chunks'] == [0, 2, 3]
    assert status['missing_chunks'] == [1]
    assert status['is_complete'] is False

    response = _put_chunk(dmr_client, session_id, 1, chunks[1])
    assert response.json()['state'] == 'stored'

    response = dmr_client.post(_complete_url(session_id))
    assert response.status_code == HTTPStatus.CREATED, response.content
    video = response.json()
    assert video == {
        'id': video['id'],
        'session_id': session_id,
        'filename': _FILENAME,
        'size': len(content),
        'checksum': hashlib.sha256(content).hexdigest(),
        'url': video['url'],
        'created_at': video['created_at'],
    }
    assert video['url'].endswith(f'videos/{session_id}/{_FILENAME}')

    # The published file is the exact original byte sequence:
    published = _published_path(upload_root, session_id)
    assert published.read_bytes() == content
    assert Video.objects.count() == 1

    # Staging artifacts are gone, the session is now completed:
    assert not (upload_root / 'staging' / session_id).exists()
    status = dmr_client.get(_detail_url(session_id)).json()
    assert status['status'] == 'completed'
    assert status['is_complete'] is True


def test_resume_after_interruption(
    dmr_client: DMRClient,
    upload_root: Path,
) -> None:
    """After reconnecting, the client asks what is missing and continues."""
    content = _content()
    chunks = _split(content, _CHUNK_SIZE)
    session_id = _init_session(dmr_client, content)['id']

    # Network dies after two chunks (and one uncertain retransmit):
    _upload_all(dmr_client, session_id, chunks, order=[0, 1])

    # A fresh client process only knows the session id, so it asks first:
    status = dmr_client.get(_detail_url(session_id)).json()
    assert status['received_chunks'] == [0, 1]
    assert status['missing_chunks'] == [2, 3]
    assert status['received_size'] == 2 * _CHUNK_SIZE

    # Continue from chunk 2 without reuploading 0 and 1:
    response = _put_chunk(dmr_client, session_id, 2, chunks[2])
    assert response.json()['state'] == 'stored'
    _put_chunk(dmr_client, session_id, 3, chunks[3])

    response = dmr_client.post(_complete_url(session_id))
    assert response.status_code == HTTPStatus.CREATED, response.content
    assert _published_path(upload_root, session_id).read_bytes() == content


def test_duplicate_chunk_is_not_written_twice(
    dmr_client: DMRClient,
    upload_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Retransmitting the same identical chunk is a storage no-op."""
    content = _content()
    chunks = _split(content, _CHUNK_SIZE)
    session_id = _init_session(dmr_client, content)['id']

    written: list[int] = []
    original_store = storage.store_chunk

    def tracking_store(session_hex: str, index: int, uploaded_file):
        written.append(index)
        return original_store(session_hex, index, uploaded_file)

    monkeypatch.setattr(storage, 'store_chunk', tracking_store)

    first = _put_chunk(dmr_client, session_id, 0, chunks[0])
    assert first.json()['state'] == 'stored'
    second = _put_chunk(dmr_client, session_id, 0, chunks[0])
    assert second.status_code == HTTPStatus.OK
    assert second.json()['state'] == 'duplicate'
    third = _put_chunk(dmr_client, session_id, 0, chunks[0])
    assert third.json()['state'] == 'duplicate'

    # Persisted exactly once and recorded exactly once:
    assert written == [0]
    assert (
        UploadedChunk.objects.filter(
            session_id=session_id,
            index=0,
        ).count()
        == 1
    )


def test_completion_with_missing_chunks_stays_resumable(
    dmr_client: DMRClient,
    upload_root: Path,
) -> None:
    """Early completion fails with 409 and lists what is still missing."""
    content = _content()
    chunks = _split(content, _CHUNK_SIZE)
    session_id = _init_session(dmr_client, content)['id']
    _upload_all(dmr_client, session_id, chunks, order=[0, 1])

    response = dmr_client.post(_complete_url(session_id))
    assert response.status_code == HTTPStatus.CONFLICT
    assert response.json() == {
        'detail': [
            {
                'msg': 'Chunk 2 has not been received',
                'type': 'missing_chunk',
                'loc': ['chunks', 2],
            },
            {
                'msg': 'Chunk 3 has not been received',
                'type': 'missing_chunk',
                'loc': ['chunks', 3],
            },
        ],
    }
    assert Video.objects.count() == 0

    # The temporary state survived, the upload can continue right away:
    _put_chunk(dmr_client, session_id, 2, chunks[2])
    _put_chunk(dmr_client, session_id, 3, chunks[3])
    response = dmr_client.post(_complete_url(session_id))
    assert response.status_code == HTTPStatus.CREATED, response.content
    assert _published_path(upload_root, session_id).read_bytes() == content


def test_checksum_mismatch_rejected_and_recoverable(
    dmr_client: DMRClient,
    upload_root: Path,
) -> None:
    """A wrong total digest refuses completion but keeps resumable state."""
    content = _content()
    chunks = _split(content, _CHUNK_SIZE)
    session_id = _init_session(dmr_client, content)['id']

    # Chunk 1 is corrupted on the wire (same size, different bytes):
    corrupted = bytes((byte + 1) % 256 for byte in chunks[1])
    _upload_all(dmr_client, session_id, chunks, order=[0, 2, 3])
    _put_chunk(dmr_client, session_id, 1, corrupted)

    response = dmr_client.post(_complete_url(session_id))
    assert response.status_code == HTTPStatus.UNPROCESSABLE_ENTITY
    error = response.json()['detail'][0]
    assert error['type'] == 'checksum_mismatch'
    assert 'expected' in error['msg']

    # Nothing was published, but every chunk is still staged for retry:
    assert Video.objects.count() == 0
    session = VideoUploadSession.objects.get(id=session_id)
    assert session.status == VideoUploadSession.Status.uploading
    assert session.last_error == 'checksum_mismatch'
    staged = sorted(
        (upload_root / 'staging' / session_id).glob('*.part'),
    )
    assert len(staged) == 4

    # Retransmit the bad chunk with the correct payload, then complete:
    fixed = _put_chunk(dmr_client, session_id, 1, chunks[1])
    assert fixed.json()['state'] == 'replaced'
    response = dmr_client.post(_complete_url(session_id))
    assert response.status_code == HTTPStatus.CREATED, response.content
    assert _published_path(upload_root, session_id).read_bytes() == content
    assert Video.objects.get().checksum == hashlib.sha256(content).hexdigest()


def test_wrong_chunk_size_is_rejected(
    dmr_client: DMRClient,
    upload_root: Path,
) -> None:
    """A chunk that does not match the session geometry is refused."""
    content = _content()
    session_id = _init_session(dmr_client, content)['id']

    response = _put_chunk(dmr_client, session_id, 0, b'too-short')
    assert response.status_code == HTTPStatus.UNPROCESSABLE_ENTITY
    assert response.json()['detail'][0]['loc'] == ['chunks', 0]

    assert UploadedChunk.objects.count() == 0
    assert not list(
        (upload_root / 'staging' / session_id).glob('*.part'),
    )


def test_wrong_size_retransmit_keeps_good_chunk(
    dmr_client: DMRClient,
    upload_root: Path,
) -> None:
    """A bad retransmit is rejected without damaging the stored chunk."""
    content = _content()
    chunks = _split(content, _CHUNK_SIZE)
    session_id = _init_session(dmr_client, content)['id']

    good = _put_chunk(dmr_client, session_id, 0, chunks[0])
    assert good.json()['state'] == 'stored'

    bad = _put_chunk(dmr_client, session_id, 0, b'wrong-size')
    assert bad.status_code == HTTPStatus.UNPROCESSABLE_ENTITY

    # Original chunk is untouched, completion still works out of the box:
    stored = UploadedChunk.objects.get(session_id=session_id, index=0)
    assert stored.size == _CHUNK_SIZE
    for index in range(1, len(chunks)):
        _put_chunk(dmr_client, session_id, index, chunks[index])
    response = dmr_client.post(_complete_url(session_id))
    assert response.status_code == HTTPStatus.CREATED, response.content
    assert _published_path(upload_root, session_id).read_bytes() == content


def test_chunk_index_out_of_range(
    dmr_client: DMRClient,
    upload_root: Path,
) -> None:
    """Uploading beyond the declared number of chunks is a 404."""
    content = _content()
    session_id = _init_session(dmr_client, content)['id']

    response = _put_chunk(dmr_client, session_id, 99, b'x')
    assert response.status_code == HTTPStatus.NOT_FOUND
    assert response.json()['detail'][0]['loc'] == ['chunks', 99]


def test_chunk_upload_requires_file_part(
    dmr_client: DMRClient,
    upload_root: Path,
) -> None:
    """A multipart request without the `chunk` part is a 400."""
    content = _content()
    session_id = _init_session(dmr_client, content)['id']

    response = dmr_client.put(
        _chunk_url(session_id, 0),
        data=encode_multipart(BOUNDARY, {}),
        content_type=MULTIPART_CONTENT,
    )
    assert response.status_code == HTTPStatus.BAD_REQUEST


def test_session_not_found(dmr_client: DMRClient) -> None:
    """Unknown sessions produce 404 on every session-scoped endpoint."""
    session_id = '11111111-1111-1111-1111-111111111111'
    assert (
        dmr_client.get(_detail_url(session_id)).status_code
        == HTTPStatus.NOT_FOUND
    )
    assert (
        dmr_client.post(_complete_url(session_id)).status_code
        == HTTPStatus.NOT_FOUND
    )
    assert (
        dmr_client.delete(_detail_url(session_id)).status_code
        == HTTPStatus.NOT_FOUND
    )
    assert (
        _put_chunk(dmr_client, session_id, 0, b'x').status_code
        == HTTPStatus.NOT_FOUND
    )


def test_abort_drops_staging_and_blocks_completed(
    dmr_client: DMRClient,
    upload_root: Path,
) -> None:
    """Aborting removes the temp state; completed sessions cannot be aborted."""
    content = _content()
    chunks = _split(content, _CHUNK_SIZE)
    session_id = _init_session(dmr_client, content)['id']
    _upload_all(dmr_client, session_id, chunks, order=[0, 1])

    response = dmr_client.delete(_detail_url(session_id))
    assert response.status_code == HTTPStatus.NO_CONTENT
    assert response.content == b''
    assert not VideoUploadSession.objects.filter(id=session_id).exists()
    assert UploadedChunk.objects.count() == 0
    assert not (upload_root / 'staging' / session_id).exists()
    assert (
        dmr_client.get(_detail_url(session_id)).status_code
        == HTTPStatus.NOT_FOUND
    )

    # A completed session can no longer be aborted:
    completed_id = _init_session(dmr_client, content)['id']
    _upload_all(dmr_client, completed_id, chunks)
    dmr_client.post(_complete_url(completed_id))
    response = dmr_client.delete(_detail_url(completed_id))
    assert response.status_code == HTTPStatus.CONFLICT


def test_complete_and_chunk_upload_are_idempotent_after_completion(
    dmr_client: DMRClient,
    upload_root: Path,
) -> None:
    """Repeated completion returns the same video, chunks are refused 409."""
    content = _content()
    chunks = _split(content, _CHUNK_SIZE)
    session_id = _init_session(dmr_client, content)['id']
    _upload_all(dmr_client, session_id, chunks)

    first = dmr_client.post(_complete_url(session_id))
    assert first.status_code == HTTPStatus.CREATED
    second = dmr_client.post(_complete_url(session_id))
    assert second.status_code == HTTPStatus.OK
    assert second.json()['id'] == first.json()['id']
    assert Video.objects.count() == 1
    assert _published_path(upload_root, session_id).read_bytes() == content

    response = _put_chunk(dmr_client, session_id, 0, chunks[0])
    assert response.status_code == HTTPStatus.CONFLICT


@pytest.mark.parametrize(
    ('payload', 'loc_tail'),
    [
        (
            {
                'filename': _FILENAME,
                'total_size': 100,
                'chunk_size': 512,
                'checksum': 'a' * 64,
            },
            'parsed_body',
        ),
        (
            {
                'filename': _FILENAME,
                'total_size': 0,
                'chunk_size': 1,
                'checksum': 'a' * 64,
            },
            'total_size',
        ),
        (
            {
                'filename': _FILENAME,
                'total_size': 10,
                'chunk_size': 1,
                'checksum': 'not-a-sha256',
            },
            'checksum',
        ),
        (
            {
                'filename': '',
                'total_size': 10,
                'chunk_size': 1,
                'checksum': 'a' * 64,
            },
            'filename',
        ),
    ],
)
def test_init_validation(
    dmr_client: DMRClient,
    payload: dict[str, object],
    loc_tail: str,
) -> None:
    """Invalid session geometry or checksum is refused with a 400."""
    response = dmr_client.post(_COLLECTION_URL, data=payload)
    assert response.status_code == HTTPStatus.BAD_REQUEST
    assert response.json()['detail'][0]['loc'][-1] == loc_tail
    assert VideoUploadSession.objects.count() == 0


def test_chunk_smaller_than_declared_total_size(
    dmr_client: DMRClient,
    upload_root: Path,
) -> None:
    """Last-chunk geometry works for non-divisible sizes as well."""
    content = b'hello world'  # 11 bytes, chunk_size 4 -> 4+4+3
    chunks = _split(content, 4)
    session_id = _init_session(
        dmr_client,
        content,
        chunk_size=4,
    )['id']
    _upload_all(dmr_client, session_id, chunks)
    response = dmr_client.post(_complete_url(session_id))
    assert response.status_code == HTTPStatus.CREATED, response.content
    assert _published_path(upload_root, session_id).read_bytes() == content


def test_init_normalizes_uppercase_checksum(
    dmr_client: DMRClient,
) -> None:
    """Checksums are compared case-insensitively and stored in lowercase."""
    content = _content()
    uppercase = hashlib.sha256(content).hexdigest().upper()
    session = _init_session(dmr_client, content, checksum=uppercase)
    assert session['checksum'] == uppercase.lower()


def test_published_file_is_removed_when_database_write_fails(
    dmr_client: DMRClient,
    upload_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failed finalization cleans the file and keeps the resumable state."""
    content = _content()
    chunks = _split(content, _CHUNK_SIZE)
    session_id = _init_session(dmr_client, content)['id']
    _upload_all(dmr_client, session_id, chunks)

    def fail_create(**kwargs):
        raise IntegrityError('simulated database failure')

    monkeypatch.setattr(Video.objects, 'create', fail_create)
    with pytest.raises(IntegrityError, match='simulated'):
        dmr_client.post(_complete_url(session_id))
    monkeypatch.undo()

    # The orphan published file is gone, staging survived for retry:
    assert not _published_path(upload_root, session_id).exists()
    session = VideoUploadSession.objects.get(id=session_id)
    assert session.status == VideoUploadSession.Status.uploading
    assert len(list((upload_root / 'staging' / session_id).glob('*.part'))) == 4

    # Retrying completion after the database recovered succeeds:
    response = dmr_client.post(_complete_url(session_id))
    assert response.status_code == HTTPStatus.CREATED, response.content
    assert _published_path(upload_root, session_id).read_bytes() == content
    assert Video.objects.count() == 1
