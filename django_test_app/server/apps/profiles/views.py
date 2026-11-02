from http import HTTPStatus
from typing import Any, final

from django.http import HttpResponse
from typing_extensions import override

from dmr import Controller, Query
from dmr.endpoint import Endpoint
from dmr.plugins.pydantic import PydanticSerializer
from server.apps.profiles.fields import (
    FieldSelectionError,
    project_fields,
    select_fields,
)
from server.apps.profiles.serializers import ProfileListQuery
from server.apps.profiles.services import (
    profile_list_service,
    serialize_profile,
)


@final
class UserProfileListController(Controller[PydanticSerializer]):
    """
    Mobile user profile list with optional sparse fieldsets.

    ``?fields=id,contact.email`` returns only the selected branches,
    while a request without ``fields`` keeps returning the complete
    public profile document for old clients.
    """

    def get(
        self,
        parsed_query: Query[ProfileListQuery],
    ) -> list[dict[str, Any]]:
        """List user profiles, optionally projected to chosen fields."""
        # Validate the selection before touching the database:
        field_tree = select_fields(parsed_query.fields)
        return [
            project_fields(serialize_profile(profile), field_tree)
            for profile in profile_list_service()
        ]

    @override
    def handle_error(
        self,
        endpoint: Endpoint,
        controller: Controller[PydanticSerializer],
        exc: Exception,
    ) -> HttpResponse:
        # Unknown or malformed `fields` paths use the standard 400 model:
        if isinstance(exc, FieldSelectionError):
            return self.to_error(
                exc.to_error_model(),
                status_code=HTTPStatus.BAD_REQUEST,
            )
        return super().handle_error(endpoint, controller, exc)
