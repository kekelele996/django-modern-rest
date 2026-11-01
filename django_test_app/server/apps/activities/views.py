from http import HTTPStatus
from typing import Annotated, Any, final

import pydantic
from django.http import HttpResponse
from django.shortcuts import get_object_or_404

from dmr import Body, Controller, Path, ResponseSpec, validate
from dmr.plugins.pydantic import PydanticSerializer
from server.apps.activities.models import Activity
from server.apps.activities.serializers import ActivityConfigResponseSchema
from server.apps.activities.services import (
    ActivityConfigValidationError,
    partial_save_activity_config,
)


@final
class _ActivityPath(pydantic.BaseModel):
    activity_id: Annotated[int, pydantic.Field(ge=1)]


@final
class ActivityConfigController(Controller[PydanticSerializer]):
    """Partial-save endpoint for a single activity's JSON config."""

    @validate(
        ResponseSpec(
            return_type=ActivityConfigResponseSchema,
            status_code=HTTPStatus.OK,
            description='Config updated; the effective document is returned.',
        ),
    )
    def patch(
        self,
        parsed_path: Path[_ActivityPath],
        parsed_body: Body[dict[str, Any]],
    ) -> HttpResponse:
        """
        Save only the config fields actually sent in the request.

        Nested objects are merged layer by layer, ``null`` deletes a
        field and an empty object leaves the stored subtree untouched.
        Every problem (unknown fields at any depth, invalid values) is
        reported with its full path, and a failed request never changes
        the stored configuration.
        """
        activity = get_object_or_404(Activity, pk=parsed_path.activity_id)
        try:
            config = partial_save_activity_config(activity, parsed_body)
        except ActivityConfigValidationError as exc:
            # The error document uses the framework error model, so the
            # usual 400 response schema covers it.
            return self.to_error(
                exc.to_error_model(),
                status_code=HTTPStatus.BAD_REQUEST,
            )
        return self.to_response(
            {'config': config},
            status_code=HTTPStatus.OK,
        )
