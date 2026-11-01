from http import HTTPStatus
from typing import final

from django.http import HttpResponse

from dmr import Body, Controller, ResponseSpec, validate
from dmr.plugins.pydantic import PydanticSerializer
from server.apps.batch_operations.serializers import (
    BatchShelfOperationsSchema,
    BatchShelfResultSchema,
)
from server.apps.batch_operations.services import batch_shelf_service


@final
class ShelfBatchController(Controller[PydanticSerializer]):
    """Operations backend endpoint for batch listing and delisting."""

    @validate(
        ResponseSpec(
            return_type=BatchShelfResultSchema,
            status_code=HTTPStatus.OK,
            description='Every operation in the batch succeeded.',
        ),
        ResponseSpec(
            return_type=BatchShelfResultSchema,
            status_code=HTTPStatus.MULTI_STATUS,
            description='At least one operation failed, see per-item results.',
        ),
    )
    def post(
        self,
        parsed_body: Body[BatchShelfOperationsSchema],
    ) -> HttpResponse:
        """
        Submit dozens of shelf operations in one batch request.

        Operations are processed independently and in submission order.
        A single failure never aborts the batch and each response item
        carries its 1-based position, its success flag and, on failure,
        a machine-readable code with a human-readable reason.
        Each successful item is committed atomically (status change and
        audit log); a failed item leaves no trace in the database.
        """
        result = batch_shelf_service(parsed_body.operations)
        status_code = (
            HTTPStatus.OK if result.failed == 0 else HTTPStatus.MULTI_STATUS
        )
        return self.to_response(
            result.model_dump(mode='json'),
            status_code=status_code,
        )
