from dmr.routing import Router, path
from server.apps.video_uploads import views

router = Router(
    'video-uploads/',
    [
        path(
            '',
            views.VideoUploadCollectionController.as_view(),
            name='session_collection',
        ),
        path(
            '<uuid:session_id>/',
            views.VideoUploadDetailController.as_view(),
            name='session_detail',
        ),
        path(
            '<uuid:session_id>/chunks/<int:index>/',
            views.VideoChunkController.as_view(),
            name='session_chunk',
        ),
        path(
            '<uuid:session_id>/complete/',
            views.VideoUploadCompletionController.as_view(),
            name='session_complete',
        ),
    ],
    tags=['video_uploads'],
)
