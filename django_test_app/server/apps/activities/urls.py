from dmr.routing import Router, path
from server.apps.activities import views

router = Router(
    'activities/',
    [
        path(
            '<int:activity_id>/config/',
            views.ActivityConfigController.as_view(),
            name='activity_config',
        ),
    ],
    tags=['activities'],
)
