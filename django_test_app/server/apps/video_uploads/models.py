import uuid

from django.db import models


class VideoUploadSession(models.Model):
    """
    Resumable upload session for a single course video.

    The session stays in ``uploading`` status until every chunk has arrived
    and the whole-file checksum has been verified. Only then it becomes
    ``completed`` and a :class:`Video` is published.
    """

    class Status(models.TextChoices):
        uploading = 'uploading', 'Uploading'
        completed = 'completed', 'Completed'

    id = models.UUIDField(
        primary_key=True,
        default=uuid.uuid4,
        editable=False,
    )
    filename = models.CharField(max_length=255)
    total_size = models.PositiveBigIntegerField()
    chunk_size = models.PositiveIntegerField()
    total_chunks = models.PositiveIntegerField()
    # SHA-256 hexdigest of the complete file, promised by the client:
    checksum = models.CharField(max_length=64)
    status = models.CharField(
        max_length=16,
        choices=Status.choices,
        default=Status.uploading,
        db_index=True,
    )
    # Last verification failure, kept so the client knows retry is possible:
    last_error = models.CharField(max_length=255, blank=True, default='')
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)


class UploadedChunk(models.Model):
    """
    A single received chunk of an upload session.

    The ``(session, index)`` uniqueness is what makes retransmitting the
    same chunk idempotent: there is always at most one record per index.
    """

    session = models.ForeignKey(
        VideoUploadSession,
        related_name='chunks',
        on_delete=models.CASCADE,
    )
    index = models.PositiveIntegerField()
    # SHA-256 hexdigest of the chunk payload:
    checksum = models.CharField(max_length=64)
    size = models.PositiveBigIntegerField()
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ('index',)
        constraints = (
            models.UniqueConstraint(
                fields=('session', 'index'),
                name='uq_video_chunk_session_index',
            ),
        )


class Video(models.Model):
    """A fully assembled and verified course video, ready to be published."""

    session = models.OneToOneField(
        VideoUploadSession,
        related_name='video',
        on_delete=models.PROTECT,
    )
    filename = models.CharField(max_length=255)
    file = models.FileField(upload_to='videos/')
    size = models.PositiveBigIntegerField()
    checksum = models.CharField(max_length=64, db_index=True)
    created_at = models.DateTimeField(auto_now_add=True)
