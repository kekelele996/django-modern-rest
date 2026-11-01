from collections.abc import Sequence
from typing import ClassVar, Final

from django.db import DatabaseError, transaction

from server.apps.batch_operations.models import Product, ShelfOperationLog
from server.apps.batch_operations.serializers import (
    DELIST_ACTION,
    LIST_ACTION,
    SUPPORTED_ACTIONS,
    BatchShelfResultSchema,
    OperationErrorSchema,
    ShelfOperationResultSchema,
    ShelfOperationSchema,
    ShelfStatusValue,
)

#: Domain action wire values mapped to product shelf statuses.
_ACTION_TO_STATUS: Final[dict[str, ShelfStatusValue]] = {
    LIST_ACTION: 'on',
    DELIST_ACTION: 'off',
}


class ShelfOperationError(Exception):
    """Base class for failures of a single batch item."""

    #: Stable machine-readable code reported for this failure.
    code: ClassVar[str] = 'operation_failed'


class UnknownShelfActionError(ShelfOperationError):
    """Operation type is not one of the supported shelf actions."""

    code: ClassVar[str] = 'unknown_action'


class ProductNotFoundError(ShelfOperationError):
    """Product referenced by the operation does not exist."""

    code: ClassVar[str] = 'product_not_found'


def _write_shelf_change(
    operation: ShelfOperationSchema,
    target_status: ShelfStatusValue,
) -> None:
    """Write the status update and its audit log in one transaction."""
    with transaction.atomic():
        try:
            product = Product.objects.get(pk=operation.product_id)
        except Product.DoesNotExist:
            # Exiting the atomic block rolls the savepoint back and lets
            # the batch loop report this item as failed.
            raise ProductNotFoundError(
                f'Product {operation.product_id} does not exist',
            ) from None
        product.status = target_status
        product.save(update_fields=['status', 'updated_at'])
        ShelfOperationLog.objects.create(
            product=product,
            action=operation.action,
        )


def _apply_shelf_operation(
    operation: ShelfOperationSchema,
) -> ShelfStatusValue:
    """
    Apply one shelf operation.

    The status update and its audit log are committed together, so the
    item either fully lands or is rolled back without leaving a trace.
    """
    target_status = _ACTION_TO_STATUS.get(operation.action)
    if target_status is None:
        # Checked before opening a transaction: no writes are attempted
        # for an operation type we do not understand.
        raise UnknownShelfActionError(
            f'Unsupported operation type {operation.action!r}, '
            f'expected one of: {", ".join(sorted(SUPPORTED_ACTIONS))}',
        )
    try:
        _write_shelf_change(operation, target_status)
    except DatabaseError:
        # The atomic block above has already rolled the item back:
        # report it instead of taking the whole batch down.
        raise ShelfOperationError(
            'Operation was rolled back due to a database error',
        ) from None
    return target_status


def _success_result(
    index: int,
    operation: ShelfOperationSchema,
    status: ShelfStatusValue,
) -> ShelfOperationResultSchema:
    return ShelfOperationResultSchema(
        index=index,
        product_id=operation.product_id,
        action=operation.action,
        success=True,
        status=status,
    )


def _failure_result(
    index: int,
    operation: ShelfOperationSchema,
    error: ShelfOperationError,
) -> ShelfOperationResultSchema:
    return ShelfOperationResultSchema(
        index=index,
        product_id=operation.product_id,
        action=operation.action,
        success=False,
        error=OperationErrorSchema(
            code=error.code,
            message=str(error),
        ),
    )


def batch_shelf_service(
    operations: Sequence[ShelfOperationSchema],
) -> BatchShelfResultSchema:
    """
    Apply shelf operations one by one, independently and in order.

    A failure of one item never aborts the batch: every item gets its
    own result entry carrying its 1-based position and failure reason.
    """
    results: list[ShelfOperationResultSchema] = []
    succeeded = 0
    for index, operation in enumerate(operations, start=1):
        try:
            status = _apply_shelf_operation(operation)
        except ShelfOperationError as error:
            results.append(_failure_result(index, operation, error))
        else:
            succeeded += 1
            results.append(_success_result(index, operation, status))
    total = len(results)
    return BatchShelfResultSchema(
        total=total,
        succeeded=succeeded,
        failed=total - succeeded,
        results=results,
    )
