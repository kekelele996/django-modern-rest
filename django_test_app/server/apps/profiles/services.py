"""
Data access and mapping for the mobile user profile list.

Sensitive data is mapped OUT by hand: ``internal_note`` is never read
and the avatar document's internal ``storage_key`` is never copied,
so no sensitive key can reach a response schema or the field projector
(see :mod:`server.apps.profiles.fields`).
"""

from typing import Any

from django.db.models import QuerySet

from server.apps.profiles.models import UserProfile
from server.apps.profiles.serializers import (
    AvatarSchema,
    ContactSchema,
    RoleSchema,
    TagSchema,
    UserProfileSchema,
)


def profile_list_service() -> QuerySet[UserProfile]:
    """Return all user profiles, oldest first."""
    return UserProfile.objects.order_by('id')


def serialize_profile(profile: UserProfile) -> dict[str, Any]:
    """Serialize one profile into its complete PUBLIC dict."""
    avatar = profile.avatar or {}
    role_data = profile.role
    schema = UserProfileSchema(
        id=profile.pk,
        name=profile.name,
        avatar=AvatarSchema(
            url=avatar.get('url'),
            thumbnail_url=avatar.get('thumbnail_url'),
        ),
        contact=ContactSchema(**profile.contact),
        role=RoleSchema(**role_data) if role_data else None,
        tags=[TagSchema(**tag) for tag in profile.tags],
        attributes=profile.attributes,
    )
    return schema.model_dump()
