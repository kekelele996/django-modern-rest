from dmr.routing import Router, path
from server.apps.quotes import views

router = Router(
    'quotes/',
    [
        path(
            'stream/',
            views.QuoteStreamController.as_view(),
            name='quotes_stream',
        ),
        path(
            'publish/',
            views.QuotePublishController.as_view(),
            name='quotes_publish',
        ),
    ],
    tags=['quotes'],
)
