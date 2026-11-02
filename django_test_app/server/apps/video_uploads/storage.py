import hashlib
import os
import shutil
from pathlib import Path
from typing import Final

from django.conf import settings
from django.core.files import File as DjangoFile
from django.core.files.storage import FileSystemStorage
from django.core.files.uploadedfile import UploadedFile

# Chunks are streamed in 1 MiB blocks, big videos never land in RAM:
_READ_BLOCK_SIZE: Final = 1024 * 1024
_STAGING_DIR: Final = 'staging'
_PUBLISHED_DIR: Final = 'videos'


def root() -> Path:
    """Root directory for both staging chunks and published videos."""
    return Path(
        getattr(settings, 'VIDEO_UPLOAD_ROOT', settings.MEDIA_ROOT),
    )


def staging_dir(session_id: str) -> Path:
    """Private staging directory of one upload session."""
    return root() / _STAGING_DIR / str(session_id)


def chunk_path(session_id: str, index: int) -> Path:
    """Path of the stored chunk file, named by its zero-based index."""
    return staging_dir(session_id) / f'{index:08d}.part'


def hash_upload(uploaded_file: UploadedFile) -> tuple[str, int]:
    """Hash an upload without persisting it, used to detect retransmits."""
    hasher = hashlib.sha256()
    size = 0
    for block in uploaded_file.chunks(_READ_BLOCK_SIZE):
        hasher.update(block)
        size += len(block)
    return hasher.hexdigest(), size


def store_chunk(
    session_id: str,
    index: int,
    uploaded_file: UploadedFile,
) -> tuple[str, int]:
    """
    Stream a chunk into the staging directory.

    The payload is first written next to the target and atomically
    ``os.replace``d into place, so an interrupted request can never leave
    a half-written chunk behind.
    """
    target = chunk_path(session_id, index)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(f'.{target.name}.{os.getpid()}.tmp')
    hasher = hashlib.sha256()
    size = 0
    with Path(temporary).open('wb') as destination:
        for block in uploaded_file.chunks(_READ_BLOCK_SIZE):
            hasher.update(block)
            size += len(block)
            destination.write(block)
    Path(temporary).replace(target)
    return hasher.hexdigest(), size


def assemble(session_id: str, total_chunks: int) -> tuple[Path, str, int]:
    """
    Concatenate received chunks in index order.

    Chunks may have arrived in any order, they are always read back
    sorted by index. The assembled file stays in staging until its
    checksum is verified by the service layer.
    """
    assembled = staging_dir(session_id) / '_assembled.tmp'
    hasher = hashlib.sha256()
    size = 0
    with Path(assembled).open('wb') as destination:
        for index in range(total_chunks):
            with Path(chunk_path(session_id, index)).open('rb') as source:
                while True:
                    block = source.read(_READ_BLOCK_SIZE)
                    if not block:
                        break
                    hasher.update(block)
                    size += len(block)
                    destination.write(block)
    return assembled, hasher.hexdigest(), size


def publish(
    session_id: str,
    assembled_path: Path,
    filename: str,
) -> str:
    """Move a verified assembled file into the published storage."""
    storage = FileSystemStorage(location=root(), base_url=settings.MEDIA_URL)
    target_name = storage.get_available_name(
        f'{_PUBLISHED_DIR}/{session_id}/{storage.get_valid_name(filename)}',
    )
    with Path(assembled_path).open('rb') as source:
        return storage.save(target_name, DjangoFile(source))


def published_url(relative_name: str) -> str:
    """Return the public URL of a published video."""
    storage = FileSystemStorage(location=root(), base_url=settings.MEDIA_URL)
    return storage.url(relative_name)


def delete_published(relative_name: str) -> None:
    """Remove a published file, used when publishing cannot be finalized."""
    storage = FileSystemStorage(location=root(), base_url=settings.MEDIA_URL)
    storage.delete(relative_name)


def discard_file(path: Path) -> None:
    """Delete a single file, ignoring a missing one."""
    path.unlink(missing_ok=True)


def discard_staging(session_id: str) -> None:
    """Remove every staging artifact of a session."""
    shutil.rmtree(staging_dir(session_id), ignore_errors=True)
