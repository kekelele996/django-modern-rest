from collections.abc import Callable
from http import HTTPStatus
from typing import ClassVar, final

from django.http import HttpResponse
from typing_extensions import override

from dmr import Body, Controller, FileMetadata, Path, modify
from dmr.endpoint import Endpoint
from dmr.errors import ErrorType
from dmr.metadata import ResponseSpec
from dmr.parsers import MultiPartParser
from dmr.plugins.pydantic import PydanticSerializer
from server.apps.video_uploads import services
from server.apps.video_uploads.serializers import (
    ChunkAckSchema,
    ChunkPathSchema,
    ChunkUploadFiles,
    SessionInitSchema,
    SessionPathSchema,
    SessionStatusSchema,
    VideoSchema,
)

_ErrorHandler = Callable[[Exception], HttpResponse]


class UploadErrorController(Controller[PydanticSerializer]):
    """Shared mapping of upload domain errors onto HTTP responses."""

    error_handlers: ClassVar[tuple[tuple[type[Exception], str], ...]] = (
        (services.SessionNotFoundError, '_session_not_found_error'),
        (services.SessionCompletedError, '_session_completed_error'),
        (services.ChunkIndexOutOfRangeError, '_chunk_index_error'),
        (services.ChunkSizeMismatchError, '_chunk_size_error'),
        (services.MissingChunksError, '_missing_chunks_error'),
        (services.ChecksumMismatchError, '_checksum_mismatch_error'),
    )

    @override
    def handle_error(
        self,
        endpoint: Endpoint,
        controller: Controller[PydanticSerializer],
        exc: Exception,
    ) -> HttpResponse:
        for error_type, handler_name in self.error_handlers:
            if isinstance(exc, error_type):
                handler: _ErrorHandler = getattr(self, handler_name)
                return handler(exc)
        return super().handle_error(endpoint, controller, exc)

    def _session_not_found_error(self, exc: Exception) -> HttpResponse:
        return self.to_error(
            self.format_error(
                str(exc),
                loc=['session_id'],
                error_type=ErrorType.not_found,
            ),
            status_code=HTTPStatus.NOT_FOUND,
        )

    def _session_completed_error(self, exc: Exception) -> HttpResponse:
        return self.to_error(
            self.format_error(
                'Upload session is already completed',
                error_type=ErrorType.user_msg,
            ),
            status_code=HTTPStatus.CONFLICT,
        )

    def _chunk_index_error(self, exc: Exception) -> HttpResponse:
        index = exc.index
        return self.to_error(
            self.format_error(
                str(exc),
                loc=['chunks', index],
                error_type=ErrorType.not_found,
            ),
            status_code=HTTPStatus.NOT_FOUND,
        )

    def _chunk_size_error(self, exc: Exception) -> HttpResponse:
        index = exc.index
        return self.to_error(
            self.format_error(
                str(exc),
                loc=['chunks', index],
                error_type=ErrorType.value_error,
            ),
            status_code=HTTPStatus.UNPROCESSABLE_ENTITY,
        )

    def _missing_chunks_error(self, exc: Exception) -> HttpResponse:
        missing = exc.missing
        return self.to_error(
            {
                'detail': [
                    {
                        'msg': f'Chunk {index} has not been received',
                        'type': 'missing_chunk',
                        'loc': ['chunks', index],
                    }
                    for index in missing
                ],
            },
            status_code=HTTPStatus.CONFLICT,
        )

    def _checksum_mismatch_error(self, exc: Exception) -> HttpResponse:
        return self.to_error(
            self.format_error(
                str(exc),
                error_type='checksum_mismatch',
            ),
            status_code=HTTPStatus.UNPROCESSABLE_ENTITY,
        )


@final
class VideoUploadCollectionController(UploadErrorController):
    """Start a new resumable upload session."""

    @modify(status_code=HTTPStatus.CREATED)
    def post(
        self,
        parsed_body: Body[SessionInitSchema],
    ) -> SessionStatusSchema:
        """Initialize a course video upload session."""
        session = services.init_session(parsed_body)
        return services.session_status(session)


@final
class VideoUploadDetailController(UploadErrorController):
    """Inspect and abort a single upload session."""

    def get(
        self,
        parsed_path: Path[SessionPathSchema],
    ) -> SessionStatusSchema:
        """Report which chunks have already been received."""
        session = services.get_session(parsed_path.session_id)
        return services.session_status(session)

    @modify(
        status_code=HTTPStatus.NO_CONTENT,
        extra_responses=[
            ResponseSpec(
                Controller.error_model,
                status_code=HTTPStatus.CONFLICT,
            ),
        ],
    )
    def delete(self, parsed_path: Path[SessionPathSchema]) -> None:
        """Abort an unfinished session and drop its staging chunks."""
        session = services.get_session(parsed_path.session_id)
        services.abort_session(session)


@final
class VideoChunkController(UploadErrorController):
    """Upload one chunk of an upload session."""

    parsers = (MultiPartParser(),)

    @modify(
        extra_responses=[
            ResponseSpec(
                Controller.error_model,
                status_code=HTTPStatus.CONFLICT,
            ),
            ResponseSpec(
                Controller.error_model,
                status_code=HTTPStatus.UNPROCESSABLE_ENTITY,
            ),
        ],
    )
    def put(
        self,
        parsed_path: Path[ChunkPathSchema],
        parsed_file_metadata: FileMetadata[ChunkUploadFiles],
    ) -> ChunkAckSchema:
        """
        Store a chunk.

        Retransmitting an identical chunk is a no-op (``state=duplicate``),
        chunks may arrive in any order since they are named by index.
        """
        session = services.get_session(parsed_path.session_id)
        chunk, state = services.receive_chunk(
            session,
            parsed_path.index,
            self.request.FILES['chunk'],
        )
        return ChunkAckSchema(
            session_id=session.id,
            index=chunk.index,
            checksum=chunk.checksum,
            size=chunk.size,
            state=state,
        )


@final
class VideoUploadCompletionController(UploadErrorController):
    """Finalize an upload session."""

    @modify(
        extra_responses=[
            ResponseSpec(VideoSchema, status_code=HTTPStatus.OK),
            ResponseSpec(
                Controller.error_model,
                status_code=HTTPStatus.CONFLICT,
            ),
            ResponseSpec(
                Controller.error_model,
                status_code=HTTPStatus.UNPROCESSABLE_ENTITY,
            ),
        ],
    )
    def post(
        self,
        parsed_path: Path[SessionPathSchema],
    ) -> VideoSchema:
        """
        Assemble chunks, verify the total checksum and publish the video.

        Completing an already completed session is idempotent.
        """
        session = services.get_session(parsed_path.session_id)
        video, created = services.complete_session(session)
        result = services.video_schema(video)
        if created:
            return result
        return self.to_response(result, status_code=HTTPStatus.OK)
