from typing import Annotated, Any, ClassVar, TypeAlias, final

import pydantic

DatabaseId: TypeAlias = Annotated[int, pydantic.Field(gt=0)]


class _StrictSchema(pydantic.BaseModel):
    """Shared base that rejects fields not declared on the model."""

    model_config = pydantic.ConfigDict(extra='forbid')


@final
class AvatarSchema(_StrictSchema):
    """Public avatar addresses.

    The raw avatar document additionally carries an internal
    ``storage_key``; it is never mapped onto this schema, so no
    serializer can ever produce it.
    """

    url: str | None = None
    thumbnail_url: str | None = None


@final
class ContactSchema(_StrictSchema):
    """Public contact details of a user profile."""

    email: pydantic.EmailStr
    phone: str | None = None


@final
class RoleSchema(_StrictSchema):
    """Role of a user inside a project."""

    id: int
    name: str


@final
class TagSchema(_StrictSchema):
    """Tag attached to a user profile."""

    id: int
    name: str


@final
class UserProfileSchema(_StrictSchema):
    """
    Full public representation of a user profile.

    Sensitive data (the top-level ``internal_note`` and the avatar's
    ``storage_key``) is deliberately not declared here: the response
    pipeline never holds such keys in the first place.
    """

    id: DatabaseId
    name: str
    avatar: AvatarSchema
    contact: ContactSchema
    role: RoleSchema | None
    tags: list[TagSchema]
    attributes: dict[str, Any] = {}


@final
class ProfileListQuery(_StrictSchema):
    """Query parameters accepted by the profile list endpoint."""

    # Both repeated params (`?fields=id&fields=name`) and single
    # comma-separated params (`?fields=id,name`) are turned into a list:
    __dmr_force_list__: ClassVar[frozenset[str]] = frozenset(('fields',))

    fields: list[str] | None = pydantic.Field(
        default=None,
        description=(
            'Comma-separated selection of response fields. '
            'Nested fields use dot paths, for example: '
            '`id,contact.email,role.name`. '
            'When omitted, the complete public object is returned. '
            'Sensitive fields cannot be selected and are always hidden.'
        ),
    )
