"""
Response field selection (a.k.a. sparse fieldsets) for user profiles.

Why
---

The mobile profile list used to fetch the whole profile document for
every user in one request: a single record is tens of kilobytes and
always carried heavy avatar CDN addresses and internal operator notes,
which drove the mobile traffic bill up.

Clients may send a ``fields`` query parameter naming exactly the
fields they need (``?fields=id,name,contact.email``). The rules:

1. Without ``fields`` the complete public object is returned, so old
   clients keep their original contract.
2. Fields are comma-separated and nested fields use dot paths.
3. Selections from different branches merge back into one nested
   object, keeping only the containers necessary to reach each leaf.
4. Unknown paths are rejected with a ``400`` error that names the
   FIRST missing segment of the offending path (e.g. ``role.title``
   reports ``title``, not the deeper ``...name`` segment).
5. Malformed paths (empty segments like ``id,,name``) produce a
   distinguishable ``invalid_field_path`` error.
6. Sensitive fields (``internal_note``, ``avatar.storage_key``) are
   never part of any response. Naming them explicitly is accepted but
   silently excludes them: no serializer stage ever holds the value.

Field selection only rearranges the response payload. It is computed
from freshly dumped dictionaries, so the database rows, the ORM
objects and any later request are never affected.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from types import MappingProxyType, NoneType, UnionType
from typing import Any, TypeAlias, get_args, get_origin

import pydantic

from server.apps.profiles.serializers import (
    AvatarSchema,
    UserProfileSchema,
)


class _Kind(StrEnum):
    """Kinds of selectable response fields."""

    SCALAR = 'scalar'
    OBJECT = 'object'
    LIST = 'list'
    OPAQUE = 'opaque'


class _ErrorCode(StrEnum):
    """Machine-readable types of field-selection error details."""

    UNKNOWN = 'unknown_field'
    INVALID = 'invalid_field_path'


@dataclass(frozen=True, slots=True)
class FieldDescriptor:
    """Static description of one selectable response field."""

    kind: _Kind
    sensitive: bool = False
    children: Mapping[str, 'FieldDescriptor'] | None = None
    element: 'FieldDescriptor | None' = None


@dataclass(frozen=True, slots=True)
class FieldPathError:
    """One problem located at ``location`` inside a field path."""

    location: tuple[str, ...]
    message: str
    code: str

    def to_detail(self) -> dict[str, Any]:
        """Render the problem using DMR's standard error document."""
        return {
            'msg': self.message,
            'loc': list(self.location),
            'type': self.code,
        }


class FieldSelectionError(Exception):
    """At least one requested field path is invalid.

    Carries every problem found in the request, each one located by
    the first missing segment of its path.
    """

    code = 'field_selection_failed'

    def __init__(self, errors: Sequence[FieldPathError]) -> None:
        self.errors = list(errors)
        paths = ', '.join('.'.join(error.location) for error in errors)
        super().__init__(f'Invalid field selection: {paths}')

    def to_error_model(self) -> dict[str, Any]:
        """Render the problems using DMR's standard error document."""
        return {
            'detail': [error.to_detail() for error in self.errors],
        }


_SensitiveRegistry: TypeAlias = Mapping[
    type[pydantic.BaseModel],
    frozenset[str],
]
_ParsedSelectors: TypeAlias = tuple[
    list[list[str]] | None,
    list[FieldPathError],
]

#: Sensitive fields exist in storage but are NEVER declared on a
#: response schema. They are registered as known fields, so that naming
#: them silently excludes the selection instead of erroring as unknown.
_sensitive_registry: _SensitiveRegistry = MappingProxyType(
    {
        AvatarSchema: frozenset(('storage_key',)),
        UserProfileSchema: frozenset(('internal_note',)),
    },
)


def _unwrap_optional(annotation: Any) -> Any:
    """Return the single non-``None`` member of ``T | None``."""
    origin = get_origin(annotation)
    if origin not in {UnionType, get_origin(int | None)}:
        return annotation
    members = [
        member for member in get_args(annotation) if member is not NoneType
    ]
    return members[0] if len(members) == 1 else annotation


def _describe(annotation: Any, *, sensitive: bool = False) -> FieldDescriptor:
    annotation = _unwrap_optional(annotation)
    if isinstance(annotation, type) and issubclass(
        annotation,
        pydantic.BaseModel,
    ):
        return FieldDescriptor(
            kind=_Kind.OBJECT,
            sensitive=sensitive,
            children=_model_children(annotation),
        )
    origin = get_origin(annotation)
    if origin is list:
        return FieldDescriptor(
            kind=_Kind.LIST,
            sensitive=sensitive,
            element=_describe(get_args(annotation)[0]),
        )
    if origin is dict:
        # Free-form objects are opaque leaves: their keys are not part
        # of the declared contract.
        return FieldDescriptor(kind=_Kind.OPAQUE, sensitive=sensitive)
    return FieldDescriptor(kind=_Kind.SCALAR, sensitive=sensitive)


def _model_children(
    model: type[pydantic.BaseModel],
) -> Mapping[str, FieldDescriptor]:
    children = {
        name: _describe(field_info.annotation)
        for name, field_info in model.model_fields.items()
    }
    for name in _sensitive_registry.get(model, frozenset()):
        children[name] = FieldDescriptor(kind=_Kind.SCALAR, sensitive=True)
    return children


def _join_path(container: str, segment: str) -> str:
    return f'{container}.{segment}' if container else segment


def _split_part(part: str) -> list[str]:
    return [segment.strip() for segment in part.split('.')]


def _unknown_error(
    segments: Sequence[str],
    position: int,
    container: str,
) -> FieldPathError:
    segment = segments[position]
    dotted = '.'.join(segments[: position + 1])
    where = f"object '{container}'" if container else 'the response object'
    return FieldPathError(
        location=tuple(segments[: position + 1]),
        message=(
            f'Unknown field path {dotted!r}: '
            f'segment {segment!r} does not exist on {where}.'
        ),
        code=_ErrorCode.UNKNOWN.value,
    )


def _invalid_path_error(
    segments: Sequence[str],
    position: int,
) -> FieldPathError:
    dotted = '.'.join(segments)
    return FieldPathError(
        location=tuple(segments),
        message=(
            f'Invalid field path {dotted!r}: '
            f'empty segment at position {position}.'
        ),
        code=_ErrorCode.INVALID.value,
    )


def _empty_selector_error(
    parts: Sequence[str],
    index: int,
    non_empty: Sequence[int],
) -> FieldPathError:
    previous = next(
        (candidate for candidate in reversed(non_empty) if candidate < index),
        None,
    )
    following = next(candidate for candidate in non_empty if candidate > index)
    if previous is None:
        previous_segments: list[str] = []
    else:
        previous_segments = _split_part(parts[previous])
    location = [*previous_segments, '', *_split_part(parts[following])]
    dotted = '.'.join(location)
    return FieldPathError(
        location=tuple(location),
        message=(
            f'Invalid field selection {dotted!r}: empty field name '
            f'at position {len(previous_segments)}.'
        ),
        code=_ErrorCode.INVALID.value,
    )


def _classify_segment(
    segments: Sequence[str],
    position: int,
    descriptor: FieldDescriptor,
    container: str,
) -> tuple[FieldPathError | None, FieldDescriptor | None]:
    """
    Classify one path segment.

    ``(None, child)`` is a regular field, ``(None, None)`` is a
    sensitive field (accepted, but excluded from the response),
    anything else is an error paired with a ``None`` child.
    """
    segment = segments[position]
    if not segment:
        return _invalid_path_error(segments, position), None
    children = descriptor.children or {}
    if segment not in children:
        return _unknown_error(segments, position, container), None
    child = children[segment]
    if child.sensitive:
        return None, None
    last_index = len(segments) - 1
    if position < last_index and child.kind in {
        _Kind.SCALAR,
        _Kind.OPAQUE,
    }:
        return (
            _unknown_error(
                segments,
                position + 1,
                _join_path(container, segment),
            ),
            None,
        )
    return None, child


def _advance(
    child: FieldDescriptor,
    container: str,
    segment: str,
) -> tuple[FieldDescriptor, str]:
    """Move the walk to the next object layer."""
    name = _join_path(container, segment)
    if child.kind != _Kind.LIST:
        return child, name
    # The next segment addresses fields of the list elements.
    assert child.element is not None  # noqa: S101
    return child.element, name


def _validate_selector(segments: Sequence[str]) -> FieldPathError | None:
    """Validate a single path against the static response shape."""
    descriptor = FieldDescriptor(
        kind=_Kind.OBJECT,
        children=_model_children(UserProfileSchema),
    )
    container = ''
    for position, segment in enumerate(segments):
        classified = _classify_segment(
            segments,
            position,
            descriptor,
            container,
        )
        if classified[0] is not None:
            return classified[0]
        if classified[1] is None:
            # Sensitive fields are accepted but excluded entirely.
            return None
        if position < len(segments) - 1:
            descriptor, container = _advance(
                classified[1],
                container,
                segment,
            )
    return None


def _flatten_parts(raw_fields: Sequence[str]) -> list[str]:
    return [
        part.strip()
        for raw_value in raw_fields
        for part in raw_value.split(',')
    ]


def _selectors_with_errors(parts: Sequence[str]) -> _ParsedSelectors:
    """Turn non-empty raw parts into selectors and syntax errors."""
    non_empty = [index for index, part in enumerate(parts) if part]
    selectors = [_split_part(parts[index]) for index in non_empty]
    errors = [
        _empty_selector_error(parts, index, non_empty)
        for index in range(non_empty[-1])
        if not parts[index]
    ]
    return selectors, errors


def _parse(raw_fields: list[str] | None) -> _ParsedSelectors:
    """
    Flatten the raw query value into selectors and syntax errors.

    ``None`` selectors mean "no selection" (full object), including an
    empty ``fields=`` parameter. Empty slots before the last selector
    (``id,,name``) are syntax errors; trailing slots are ignored.
    """
    if raw_fields is None:
        return None, []
    parts = _flatten_parts(raw_fields)
    if not any(parts):
        return None, []
    return _selectors_with_errors(parts)


def _open_subtree(
    current: dict[str, Any],
    segment: str,
) -> dict[str, Any] | None:
    """
    Get or create the children dict for *segment*.

    Returns ``None`` when the whole subtree was already selected
    (``None`` value): deeper paths cannot narrow it back.
    """
    existing = current.get(segment)
    if existing is None and segment in current:
        return None
    if not isinstance(existing, dict):
        existing = {}
        current[segment] = existing
    return existing


def _insert(tree: dict[str, Any], segments: Sequence[str]) -> None:
    """Insert one validated path into the selection tree."""
    descriptor = FieldDescriptor(
        kind=_Kind.OBJECT,
        children=_model_children(UserProfileSchema),
    )
    current = tree
    for segment in segments[:-1]:
        child = (descriptor.children or {})[segment]
        if child.sensitive:
            return
        subtree = _open_subtree(current, segment)
        if subtree is None:
            return
        current = subtree
        if child.kind == _Kind.LIST:
            assert child.element is not None  # noqa: S101
            descriptor = child.element
        else:
            descriptor = child
    if not (descriptor.children or {})[segments[-1]].sensitive:
        # Whole subtree absorbs narrower child selectors:
        current[segments[-1]] = None


def _pruned(tree: dict[str, Any]) -> dict[str, Any]:
    """
    Rebuild *tree* without branches that select nothing.

    Empty branches only appear when a path reached a sensitive leaf
    (e.g. ``avatar.storage_key``): the sensitive leaf is never
    inserted, so its container stays empty and must not render as
    ``{}``.
    """
    pruned: dict[str, Any] = {}
    for segment, child in tree.items():
        if child is None:
            pruned[segment] = None
            continue
        subtree = _pruned(child)
        if subtree:
            pruned[segment] = subtree
    return pruned


def select_fields(raw_fields: list[str] | None) -> dict[str, Any] | None:
    """
    Parse and validate the ``fields`` query value into a tree.

    Raises :class:`FieldSelectionError` for unknown or malformed paths.
    Returns ``None`` when nothing was selected (old clients). A tree
    maps field names to sub-trees; ``None`` means "take the subtree".
    """
    selectors, errors = _parse(raw_fields)
    if selectors is None:
        return None
    for selector in selectors:
        error = _validate_selector(selector)
        if error is not None:
            errors.append(error)
    if errors:
        raise FieldSelectionError(errors)
    tree: dict[str, Any] = {}
    for selector in selectors:
        _insert(tree, selector)
    return _pruned(tree)


def project_fields(
    document: Any,
    tree: dict[str, Any] | None,
) -> Any:
    """
    Return the part of a serialized *document* described by *tree*.

    Builds new containers instead of mutating *document*, so the
    source data and any later read stay untouched. Empty objects
    ``{}``, empty lists ``[]`` and ``None`` keep their shape.
    """
    if tree is None:
        return document
    if isinstance(document, dict):
        return {
            key: project_fields(document[key], child)
            for key, child in tree.items()
            if key in document
        }
    if isinstance(document, list):
        # The same element-level tree applies to every list item.
        return [project_fields(entry, tree) for entry in document]
    return document
