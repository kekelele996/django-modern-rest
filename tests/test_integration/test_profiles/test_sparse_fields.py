"""
Sparse fieldsets for the mobile user profile list.

A complete profile is a heavy document and carries avatar CDN
addresses plus internal-only data. Clients can ask for exactly the
fields they need with the ``fields`` query parameter:

- ``?fields=id,name`` keeps only the two top-level fields;
- nested fields use dot paths (``contact.email``);
- branches from different selectors merge into one object;
- unknown paths fail with a ``400`` naming the first missing segment;
- sensitive fields are never returned, even when named explicitly;
- empty containers keep their ``[]`` / ``{}`` / ``null`` shape;
- requests without ``fields`` keep the complete public object.
"""

from http import HTTPStatus
from typing import Any

import pytest
from django.urls import reverse

from dmr.test import DMRClient
from server.apps.profiles.models import UserProfile  # type: ignore[import-not-found]

pytestmark = pytest.mark.django_db

PROFILE_LIST_URL = reverse('api:profiles:profile_list')

#: Internal-only values that must never appear in any response.
SECRET_NOTE = 'VIP; refund requests are escalated personally'  # noqa: S105
SECRET_STORAGE_KEY = 'private/avatars/ada-source.tiff'  # noqa: S105


@pytest.fixture
def profiles() -> tuple[UserProfile, UserProfile]:
    """Create one heavy profile and one profile with empty containers."""
    rich = UserProfile.objects.create(
        name='Ada Lovelace',
        email='ada@example.com',
        avatar={
            'url': 'https://cdn.example.com/avatars/ada.webp',
            'thumbnail_url': ('https://cdn.example.com/avatars/ada-64.webp'),
            'storage_key': SECRET_STORAGE_KEY,
        },
        contact={'email': 'ada@example.com', 'phone': '+1000'},
        role={'id': 1, 'name': 'admin'},
        tags=[
            {'id': 1, 'name': 'vip'},
            {'id': 2, 'name': 'early-adopter'},
        ],
        attributes={'theme': 'dark', 'newsletter': True},
        internal_note=SECRET_NOTE,
    )
    minimal = UserProfile.objects.create(
        name='Bo Nilsson',
        email='bo@example.com',
        avatar={},
        contact={'email': 'bo@example.com'},
        role=None,
        tags=[],
        attributes={},
        internal_note='do not rehire',
    )
    return rich, minimal


def _full_rich(rich: UserProfile) -> dict[str, Any]:
    return {
        'id': rich.pk,
        'name': 'Ada Lovelace',
        'avatar': {
            'url': 'https://cdn.example.com/avatars/ada.webp',
            'thumbnail_url': ('https://cdn.example.com/avatars/ada-64.webp'),
        },
        'contact': {'email': 'ada@example.com', 'phone': '+1000'},
        'role': {'id': 1, 'name': 'admin'},
        'tags': [
            {'id': 1, 'name': 'vip'},
            {'id': 2, 'name': 'early-adopter'},
        ],
        'attributes': {'theme': 'dark', 'newsletter': True},
    }


def _full_minimal(minimal: UserProfile) -> dict[str, Any]:
    return {
        'id': minimal.pk,
        'name': 'Bo Nilsson',
        'avatar': {'url': None, 'thumbnail_url': None},
        'contact': {'email': 'bo@example.com', 'phone': None},
        'role': None,
        'tags': [],
        'attributes': {},
    }


def _get(client: DMRClient, fields: str | None = None):
    """Request the profile list, optionally with a ``fields`` value."""
    if fields is None:
        return client.get(PROFILE_LIST_URL)
    return client.get(f'{PROFILE_LIST_URL}?fields={fields}')


def test_old_clients_get_the_complete_object(
    dmr_client: DMRClient,
    profiles: tuple[UserProfile, UserProfile],
) -> None:
    """R7: no ``fields`` parameter keeps the original full payload."""
    rich, minimal = profiles

    response = _get(dmr_client)

    assert response.status_code == HTTPStatus.OK, response.content
    assert response.headers['Content-Type'] == 'application/json'
    assert response.json() == [_full_rich(rich), _full_minimal(minimal)]


def test_empty_fields_returns_the_full_object(
    dmr_client: DMRClient,
    profiles: tuple[UserProfile, UserProfile],
) -> None:
    """An empty ``fields=`` value does not strip everything away."""
    rich, minimal = profiles

    response = dmr_client.get(f'{PROFILE_LIST_URL}?fields=')

    assert response.status_code == HTTPStatus.OK, response.content
    assert response.json() == [_full_rich(rich), _full_minimal(minimal)]


def test_two_top_level_fields_return_only_them(
    dmr_client: DMRClient,
    profiles: tuple[UserProfile, UserProfile],
) -> None:
    """R1: a top-level selection shrinks every item to the same keys."""
    rich, minimal = profiles

    response = _get(dmr_client, 'id,name')

    assert response.status_code == HTTPStatus.OK, response.content
    assert response.json() == [
        {'id': rich.pk, 'name': 'Ada Lovelace'},
        {'id': minimal.pk, 'name': 'Bo Nilsson'},
    ]


def test_nested_leaf_keeps_parent_container(
    dmr_client: DMRClient,
    profiles: tuple[UserProfile, UserProfile],
) -> None:
    """R2: ``contact.email`` keeps one nested object with one leaf."""
    rich, minimal = profiles

    response = _get(dmr_client, 'id,contact.email')

    assert response.status_code == HTTPStatus.OK, response.content
    assert response.json() == [
        {'id': rich.pk, 'contact': {'email': 'ada@example.com'}},
        {'id': minimal.pk, 'contact': {'email': 'bo@example.com'}},
    ]


def test_branches_merge_into_one_nested_object(
    dmr_client: DMRClient,
    profiles: tuple[UserProfile, UserProfile],
) -> None:
    """R3: several dot paths rebuild the matching nested structure."""
    response = _get(dmr_client, 'name,contact.phone,role.name,avatar.url')

    assert response.status_code == HTTPStatus.OK, response.content
    assert response.json() == [
        {
            'name': 'Ada Lovelace',
            'contact': {'phone': '+1000'},
            'role': {'name': 'admin'},
            'avatar': {
                'url': 'https://cdn.example.com/avatars/ada.webp',
            },
        },
        {
            'name': 'Bo Nilsson',
            'contact': {'phone': None},
            'role': None,
            'avatar': {'url': None},
        },
    ]


def test_list_elements_are_projected_per_item(
    dmr_client: DMRClient,
    profiles: tuple[UserProfile, UserProfile],
) -> None:
    """A dot path below a list selects inside every list item."""
    rich, minimal = profiles

    response = _get(dmr_client, 'id,tags.name')

    assert response.status_code == HTTPStatus.OK, response.content
    assert response.json() == [
        {
            'id': rich.pk,
            'tags': [{'name': 'vip'}, {'name': 'early-adopter'}],
        },
        {'id': minimal.pk, 'tags': []},
    ]


def test_whole_branch_absorbs_narrower_selectors(
    dmr_client: DMRClient,
    profiles: tuple[UserProfile, UserProfile],
) -> None:
    """``role`` plus ``role.name`` returns the whole role object."""
    rich, minimal = profiles

    response = _get(dmr_client, 'id,role,role.name')

    assert response.status_code == HTTPStatus.OK, response.content
    assert response.json() == [
        {'id': rich.pk, 'role': {'id': 1, 'name': 'admin'}},
        {'id': minimal.pk, 'role': None},
    ]


@pytest.mark.parametrize(
    ('selector', 'expected_loc', 'missing_segment'),
    [
        pytest.param(
            'id,role.title',
            ['role', 'title'],
            'title',
            id='unknown_object_field',
        ),
        pytest.param(
            'contact.address.zip',
            ['contact', 'address'],
            'address',
            id='first_missing_segment_wins',
        ),
        pytest.param(
            'name.first',
            ['name', 'first'],
            'first',
            id='cannot_descend_into_scalar',
        ),
        pytest.param(
            'tags.name.title',
            ['tags', 'name', 'title'],
            'title',
            id='below_list_element_scalar',
        ),
    ],
)
def test_unknown_path_names_missing_segment(
    dmr_client: DMRClient,
    profiles: tuple[UserProfile, UserProfile],
    selector: str,
    expected_loc: list[str],
    missing_segment: str,
) -> None:
    """R4: bad requests are 400s that name the missing segment."""
    response = _get(dmr_client, selector)

    assert response.status_code == HTTPStatus.BAD_REQUEST, response.content
    details = response.json()['detail']
    assert len(details) == 1
    detail = details[0]
    assert detail['loc'] == expected_loc
    assert detail['type'] == 'unknown_field'
    assert missing_segment in detail['msg']
    assert '.'.join(expected_loc) in detail['msg']


@pytest.mark.parametrize(
    'selector',
    [
        pytest.param('id,,name', id='double_comma'),
        pytest.param(',id', id='leading_comma'),
        pytest.param('contact.', id='trailing_dot'),
        pytest.param('.name', id='leading_dot'),
    ],
)
def test_malformed_paths_get_distinct_error(
    dmr_client: DMRClient,
    profiles: tuple[UserProfile, UserProfile],
    selector: str,
) -> None:
    """Empty path segments are syntax errors, not unknown fields."""
    response = _get(dmr_client, selector)

    assert response.status_code == HTTPStatus.BAD_REQUEST, response.content
    detail = response.json()['detail'][0]
    assert detail['type'] == 'invalid_field_path'
    assert any(not part for part in detail['loc'])


def test_named_sensitive_fields_stay_hidden(
    dmr_client: DMRClient,
    profiles: tuple[UserProfile, UserProfile],
) -> None:
    """R5: naming sensitive fields silently drops them, status stays 200."""
    rich, minimal = profiles

    response = _get(
        dmr_client,
        'id,internal_note,avatar,avatar.storage_key',
    )

    assert response.status_code == HTTPStatus.OK, response.content
    assert response.json() == [
        # `avatar` stays, but its sensitive `storage_key` never does:
        {
            'id': rich.pk,
            'avatar': {
                'url': 'https://cdn.example.com/avatars/ada.webp',
                'thumbnail_url': (
                    'https://cdn.example.com/avatars/ada-64.webp'
                ),
            },
        },
        {
            'id': minimal.pk,
            'avatar': {'url': None, 'thumbnail_url': None},
        },
    ]
    body = response.content.decode()
    assert 'internal_note' not in body
    assert SECRET_NOTE not in body
    assert SECRET_STORAGE_KEY not in body
    assert 'storage_key' not in body


def test_sensitive_only_selection_is_empty(
    dmr_client: DMRClient,
    profiles: tuple[UserProfile, UserProfile],
) -> None:
    """A sensitive-only selection never falls back to the full object."""
    response = _get(dmr_client, 'internal_note,avatar.storage_key')

    assert response.status_code == HTTPStatus.OK, response.content
    assert response.json() == [{}, {}]


def test_below_sensitive_paths_are_dropped(
    dmr_client: DMRClient,
    profiles: tuple[UserProfile, UserProfile],
) -> None:
    """Digging below a sensitive leaf stays a 200 and leaks nothing."""
    rich, minimal = profiles

    response = _get(dmr_client, 'id,avatar.storage_key.path.below')

    assert response.status_code == HTTPStatus.OK, response.content
    assert response.json() == [{'id': rich.pk}, {'id': minimal.pk}]
    assert SECRET_STORAGE_KEY not in response.content.decode()


def test_empty_containers_keep_their_shape(
    dmr_client: DMRClient,
    profiles: tuple[UserProfile, UserProfile],
) -> None:
    """R6: ``[]``, ``{}`` and ``None`` survive parent projection."""
    _rich, minimal = profiles

    response = _get(dmr_client, 'id,tags,attributes,role,avatar.thumbnail_url')

    assert response.status_code == HTTPStatus.OK, response.content
    assert response.json()[1] == {
        'id': minimal.pk,
        'tags': [],
        'attributes': {},
        'role': None,
        'avatar': {'thumbnail_url': None},
    }


def test_repeated_params_equal_comma_lists(
    dmr_client: DMRClient,
    profiles: tuple[UserProfile, UserProfile],
) -> None:
    """Both list conventions produce the same projection."""
    rich, _minimal = profiles

    comma = _get(dmr_client, 'id,contact.email')
    repeated = dmr_client.get(
        f'{PROFILE_LIST_URL}?fields=id,contact.email&fields=contact.phone',
    )
    mixed = dmr_client.get(
        f'{PROFILE_LIST_URL}?fields=id,contact&fields=contact.email',
    )

    assert comma.json()[0] == {
        'id': rich.pk,
        'contact': {'email': 'ada@example.com'},
    }
    # Two leaves of the same parent merge into one contact object:
    assert repeated.json()[0] == {
        'id': rich.pk,
        'contact': {
            'email': 'ada@example.com',
            'phone': '+1000',
        },
    }
    # `contact` (whole) absorbs the narrower `contact.email`:
    assert mixed.json()[0]['contact'] == {
        'email': 'ada@example.com',
        'phone': '+1000',
    }


def test_selection_does_not_change_stored_data(
    dmr_client: DMRClient,
    profiles: tuple[UserProfile, UserProfile],
) -> None:
    """R8: projection is response-only: rows and later reads are intact."""
    rich, _minimal = profiles

    sparse = _get(dmr_client, 'id,name,avatar.url')
    assert sparse.status_code == HTTPStatus.OK, sparse.content

    # The database row still holds the complete document, including
    # internal-only pieces:
    rich.refresh_from_db()
    assert rich.internal_note == SECRET_NOTE
    assert rich.avatar['storage_key'] == SECRET_STORAGE_KEY
    assert rich.tags == [
        {'id': 1, 'name': 'vip'},
        {'id': 2, 'name': 'early-adopter'},
    ]

    # A later request without `fields` returns the full object again:
    full = dmr_client.get(PROFILE_LIST_URL)
    assert full.status_code == HTTPStatus.OK, full.content
    assert full.json()[0] == _full_rich(rich)

    # Repeating the sparse request yields the same projected payload:
    sparse_again = _get(dmr_client, 'id,name,avatar.url')
    assert sparse_again.json() == sparse.json()


def test_other_endpoints_ignore_fields_param(
    dmr_client: DMRClient,
) -> None:
    """Existing endpoints keep ignoring unknown query parameters."""
    url = reverse('api:model_simple:user_minimalistic')
    created = dmr_client.post(
        url,
        data={
            'email': 'legacy@example.com',
            'customer_service_uid': '616d51f2-2c31-4b04-8034-2f59eae042db',
        },
    )
    assert created.status_code == HTTPStatus.CREATED, created.content

    response = dmr_client.get(f'{url}?fields=id,name')

    assert response.status_code == HTTPStatus.OK, response.content
    assert response.json() == [created.json()]
