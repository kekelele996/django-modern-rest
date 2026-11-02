from dmr.routing import Router, path
from server.apps.profiles import views

router = Router(
    'profiles/',
    [
        path(
            '',
            views.UserProfileListController.as_view(),
            name='profile_list',
        ),
    ],
    tags=['profiles'],
)
