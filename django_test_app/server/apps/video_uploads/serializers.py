import datetime as dt
import uuid
from typing import Annotated, Literal, TypeAlias

import pydantic

_SHA256_HEX: TypeAlias = Annotated[
    str,
    pydantic.Field(
        pattern=r'^[0-9a-fA-F]{64}$',
        examples=['a' * 64],
    ),
]


class SessionInitSchema(pydantic.BaseModel):
    """Payload for starting a new resumable upload session."""

    filename: str = pydantic.Field(min_length=1, max_length=255)
    total_size: int = pydantic.Field(gt=0)
    chunk_size: int = pydantic.Field(gt=0)
    # SHA-256 hexdigest promised for the fully assembled file:
    checksum: _SHA256_HEX

    @pydantic.field_validator('checksum')
    @classmethod
    def normalize_checksum(cls, value: str) -> str:
        return value.lower()

    @pydantic.model_validator(mode='after')
    def chunk_size_must_fit(self) -> 'SessionInitSchema':
        if self.chunk_size > self.total_size:
            raise ValueError(
                '`chunk_size` must not be greater than `total_size`',
            )
        return self


class SessionPathSchema(pydantic.BaseModel):
    """Path parameters identifying an upload session."""

    session_id: uuid.UUID


class ChunkPathSchema(SessionPathSchema):
    """Path parameters identifying a single chunk of a session."""

    index: int = pydantic.Field(ge=0)


class SessionStatusSchema(pydantic.BaseModel):
    """Current state of an upload session."""

    id: uuid.UUID
    filename: str
    total_size: int
    chunk_size: int
    total_chunks: int
    checksum: str
    status: Literal['uploading', 'completed']
    received_chunks: list[int]
    missing_chunks: list[int]
    received_size: int
    is_complete: bool
    last_error: str
    created_at: dt.datetime
    updated_at: dt.datetime


class ChunkFileMetadata(pydantic.BaseModel):
    """Metadata of the uploaded file part, validated from ``request.FILES``."""

    name: str
    size: int = pydantic.Field(gt=0)


class ChunkUploadFiles(pydantic.BaseModel):
    """Multipart payload of a chunk upload request."""

    chunk: ChunkFileMetadata


class ChunkAckSchema(pydantic.BaseModel):
    """Result of a chunk upload request."""

    session_id: uuid.UUID
    index: int
    checksum: str
    size: int
    # `stored` for a new chunk, `duplicate` for an idempotent retransmit
    # (nothing was written twice), `replaced` for a corrected retransmit:
    state: Literal['stored', 'duplicate', 'replaced']


class VideoSchema(pydantic.BaseModel):
    """A published course video."""

    id: int
    session_id: uuid.UUID
    filename: str
    size: int
    checksum: str
    url: str
    created_at: dt.datetime
