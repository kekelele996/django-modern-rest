from dmr.routing import Router, path
from server.apps.batch_operations import views

router = Router(
    'batch-operations/',
    [
        path(
            'shelf/',
            views.ShelfBatchController.as_view(),
            name='shelf_batch',
        ),
    ],
    tags=['batch_operations'],
)
