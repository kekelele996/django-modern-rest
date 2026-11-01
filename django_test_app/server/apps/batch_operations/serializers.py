from typing import Annotated, Final, Literal, TypeAlias, final

import pydantic

DatabaseId: TypeAlias = Annotated[int, pydantic.Field(gt=0)]

#: Put a product on the shelf (Chinese: ``上架``).
LIST_ACTION: Final = 'list'
#: Take a product off the shelf (Chinese: ``下架``).
DELIST_ACTION: Final = 'delist'
SUPPORTED_ACTIONS: Final[frozenset[str]] = frozenset(
    (LIST_ACTION, DELIST_ACTION),
)

#: Shelf status values as they appear over the wire.
ShelfStatusValue: TypeAlias = Literal['on', 'off']

#: Operations backends submit "dozens" of items at once,
#: reject obviously unbounded payloads.
MAX_BATCH_OPERATIONS: Final = 100


@final
class ShelfOperationSchema(pydantic.BaseModel):
    """A single shelf change requested as part of a batch."""

    product_id: DatabaseId
    # NOTE: this is intentionally a plain ``str`` and not a ``Literal``
    # or an enum. An unrecognized operation type must only fail this
    # particular item, it must not reject the whole batch with a ``400``
    # at request-validation time.
    action: str


@final
class BatchShelfOperationsSchema(pydantic.BaseModel):
    """Batch of shelf operations submitted in a single request."""

    operations: Annotated[
        list[ShelfOperationSchema],
        pydantic.Field(
            min_length=1,
            max_length=MAX_BATCH_OPERATIONS,
        ),
    ]


@final
class OperationErrorSchema(pydantic.BaseModel):
    """Machine- and human-readable reason of a per-item failure."""

    code: str
    message: str


@final
class ShelfOperationResultSchema(pydantic.BaseModel):
    """Outcome of one operation, reported in submission order."""

    #: 1-based position of this item in the submitted batch.
    index: Annotated[int, pydantic.Field(ge=1)]
    product_id: DatabaseId
    action: str
    success: bool
    #: Resulting shelf status, ``None`` when the item failed.
    status: ShelfStatusValue | None = None
    #: Failure reason, ``None`` when the item succeeded.
    error: OperationErrorSchema | None = None


@final
class BatchShelfResultSchema(pydantic.BaseModel):
    """Per-item results with summary counters for the whole batch."""

    total: int
    succeeded: int
    failed: int
    results: list[ShelfOperationResultSchema]
