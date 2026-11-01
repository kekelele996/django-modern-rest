"""
Partial save of an activity config document.

Why a naive implementation is dangerous
---------------------------------------

Replacing ``activity.config`` with the request body wholesale turns
every PATCH into a blind box: editing a single ticket price wipes the
whole registration block (sign-up switch, capacity limit), because the
form only sent the fields it actually manages.

The partial-save rules implemented here:

1. Only fields present in the request are touched; omitted fields keep
   their stored values.
2. Nested objects are merged layer by layer - a sent object only
   overrides the keys it carries.
3. A field explicitly sent as ``null`` is deleted from the document.
4. An empty object ``{}`` is a no-op: the stored subtree is kept as is.
5. Unknown fields are rejected with their FULL path, reported in the
   standard ``loc`` of a DMR error detail (e.g.
   ``["registration", "max_guests"]``).
6. The merged candidate is validated as a whole BEFORE anything is
   written. A validation failure leaves the stored config untouched:
   merging happens on a deep copy and the database write is the last
   step, so half-applied configs can never survive a failed request.
"""

import copy
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, ClassVar, Final, TypeAlias

import pydantic
from django.db import transaction

from server.apps.activities.models import Activity
from server.apps.activities.serializers import (
    ActivityConfig,
    RegistrationSettings,
    TicketSettings,
)

#: Stable machine-readable type of an "unknown field" error detail.
UNKNOWN_FIELD_TYPE: Final = 'unknown_field'

#: Maps a config layer model to its object-valued fields' models.
#: Keys missing from this map (and from the section models) are leaf
#: values whose type and constraints are checked by pydantic.
_ObjectLayers: TypeAlias = Mapping[
    type[pydantic.BaseModel],
    Mapping[str, type[pydantic.BaseModel]],
]
_OBJECT_SECTIONS: Final[_ObjectLayers] = {
    ActivityConfig: {
        'tickets': TicketSettings,
        'registration': RegistrationSettings,
    },
    TicketSettings: {},
    RegistrationSettings: {},
}


@dataclass(frozen=True, slots=True)
class ConfigError:
    """One problem located at ``location`` inside the config document."""

    #: Full path from the document root, e.g.
    #: ``("registration", "capacity")``.
    location: tuple[str, ...]
    #: Human-readable explanation.
    message: str
    #: Machine-readable error type (``unknown_field`` or a pydantic one).
    code: str

    def to_detail(self) -> dict[str, Any]:
        detail: dict[str, Any] = {
            'msg': self.message,
            'loc': list(self.location),
            'type': self.code,
        }
        return detail


class ActivityConfigValidationError(Exception):
    """The merged config candidate failed validation.

    Carries every problem found in the request, each one located by its
    full path. Raising this exception means the stored config was not
    modified in any way.
    """

    #: Stable machine-readable code identifying this failure group.
    code: ClassVar[str] = 'config_validation_failed'

    def __init__(self, errors: Sequence[ConfigError]) -> None:
        self.errors = list(errors)
        paths = ', '.join(
            '.'.join(error.location) or '<root>' for error in self.errors
        )
        super().__init__(f'Invalid activity config: {paths}')

    def to_error_model(self) -> dict[str, Any]:
        """Render the problems using DMR's standard error document."""
        return {
            'detail': [error.to_detail() for error in self.errors],
        }


def _dotted_path(location: tuple[Any, ...]) -> str:
    return '.'.join(str(part) for part in location)


def _unknown_error(location: tuple[str, ...]) -> ConfigError:
    return ConfigError(
        location=location,
        message=f'Unknown field: {_dotted_path(location)}',
        code=UNKNOWN_FIELD_TYPE,
    )


def _merge_section(
    current: dict[str, Any],
    key: str,
    sent_value: Any,
    nested_model: type[pydantic.BaseModel],
    field_path: tuple[str, ...],
    errors: list[ConfigError],
) -> None:
    """Merge one object-valued field, if an object was actually sent."""
    if not isinstance(sent_value, Mapping):
        # Lists and scalars for an object field are kept in the
        # candidate so the full-document validation reports them at
        # the same path with a precise type error.
        current[key] = sent_value
        return
    if not sent_value:
        # An empty object keeps the whole stored subtree untouched.
        return
    section = current.get(key)
    if not isinstance(section, dict):
        # A malformed stored value is replaced by a fresh object
        # instead of crashing the merge.
        section = {}
        current[key] = section
    _merge_patch(section, sent_value, nested_model, field_path, errors)


def _merge_patch(
    current: dict[str, Any],
    patch: Mapping[str, Any],
    layer: type[pydantic.BaseModel],
    location: tuple[str, ...],
    errors: list[ConfigError],
) -> None:
    """
    Merge one ``patch`` layer into ``current`` in place.

    ``current`` always belongs to a deep copy of the stored config,
    so mutating it can never damage the original document.
    """
    sections = _OBJECT_SECTIONS[layer]
    for key, sent_value in patch.items():
        field_path = (*location, key)

        if key not in layer.model_fields:
            # Unknown fields never enter the candidate document,
            # and the error says exactly where they were seen.
            errors.append(_unknown_error(field_path))
        elif sent_value is None:
            # An explicit `null` means "delete this item".
            current.pop(key, None)
        elif key in sections:
            _merge_section(
                current,
                key,
                sent_value,
                sections[key],
                field_path,
                errors,
            )
        else:
            # Leaf value: store whatever was sent as-is and let the
            # strict full-document validation judge type and rules.
            current[key] = sent_value


def _validate_candidate(
    candidate: Mapping[str, Any],
) -> list[ConfigError]:
    try:
        ActivityConfig.model_validate(candidate, strict=True)
    except pydantic.ValidationError as exc:
        return [
            ConfigError(
                location=tuple(str(part) for part in error['loc']),
                message=error['msg'],
                code=error['type'],
            )
            for error in exc.errors()
        ]
    return []


def partial_save_activity_config(
    activity: Activity,
    patch: Mapping[str, Any],
) -> dict[str, Any]:
    """
    Apply a partial config ``patch`` to ``activity`` and persist it.

    Returns the resulting effective config document. Raises
    :class:`ActivityConfigValidationError` (without any write) when the
    merged result is invalid or contains unknown fields.
    """
    # 1. Build the candidate on a deep copy: the stored document and the
    #    model instance cannot be mutated by merging or validation.
    candidate: dict[str, Any] = copy.deepcopy(activity.config or {})

    # 2. Layer-by-layer merge, collecting unknown-field errors with
    #    full paths instead of failing on the first one.
    errors: list[ConfigError] = []
    _merge_patch(candidate, patch, ActivityConfig, (), errors)

    # 3. Validate the WHOLE candidate: a bad leaf in one section must
    #    not be saved just because another section merged cleanly.
    errors.extend(_validate_candidate(candidate))

    if errors:
        # Nothing has been written yet: the original config survives
        # the failed request without a single partial change.
        raise ActivityConfigValidationError(errors)

    # 4. An effective no-op (`{}` or deletes of already missing keys)
    #    must not even bump `updated_at`.
    if candidate != (activity.config or {}):
        with transaction.atomic():
            activity.config = candidate
            activity.save(update_fields=['config', 'updated_at'])

    return candidate
