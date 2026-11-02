from typing import final

from django.db import models


@final
class UserProfile(models.Model):
    """
    Full user profile document consumed by mobile list clients.

    The document is intentionally heavy: avatar CDN addresses, nested
    contact/role structures and free-form attributes are all stored on
    one row, so a single record can span tens of kilobytes.

    Two pieces are strictly internal and must never leave the server:

    - ``internal_note`` - operator-only remark on the profile itself;
    - ``avatar.storage_key`` - internal storage locator kept inside the
      raw avatar document.

    They are not part of any response schema (see ``serializers.py``)
    and the field selector refuses to expose them even when a client
    asks for them by name (see ``services.py``).
    """

    name = models.CharField(max_length=100)
    email = models.EmailField(unique=True)

    #: Avatar document, e.g.
    #: ``{"url": "https://cdn.example.test/avatars/7.webp",
    #:    "thumbnail_url": "https://cdn.example.test/avatars/7-64.webp",
    #:    "storage_key": "private/avatars/7-source.tiff"}``.
    avatar = models.JSONField(default=dict)
    #: Contact document: ``{"email": ..., "phone": ...}``.
    contact = models.JSONField(default=dict)
    #: Role document ``{"id": 1, "name": "member"}`` or ``None``.
    role = models.JSONField(null=True, default=dict)
    #: Tag documents, can be an empty list.
    tags = models.JSONField(default=list)
    #: Free-form profile attributes, can be an empty object.
    attributes = models.JSONField(default=dict)

    #: Internal-only operator remark, never serialized.
    internal_note = models.TextField(blank=True, default='')

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
