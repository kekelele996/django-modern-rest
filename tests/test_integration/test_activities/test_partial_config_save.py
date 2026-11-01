"""
Partial save of an activity config document.

The config under test::

    {
        'tickets': {'price': 99.0},
        'registration': {'enabled': true, 'capacity': 100},
    }

Operations staff edit small pieces (e.g. only the ticket price).
Such an edit must never wipe the pieces the form did not send.
"""

import datetime as dt
from http import HTTPStatus
from typing import Any

import pytest
from django.urls import reverse
from django.utils import timezone

from dmr.test import DMRClient
from server.apps.activities.models import Activity

pytestmark = pytest.mark.django_db

#: A fully configured activity used by every test.
FULL_CONFIG: dict[str, Any] = {
    'tickets': {'price': 99.0},
    'registration': {'enabled': True, 'capacity': 100},
}


@pytest.fixture
def activity() -> Activity:
    """Create an activity with both config sections filled in."""
    return Activity.objects.create(name='Spring meetup', config=FULL_CONFIG)


def _patch_config(
    dmr_client: DMRClient,
    activity: Activity,
    payload: Any,
):
    return dmr_client.patch(
        reverse(
            'api:activities:activity_config',
            kwargs={'activity_id': activity.pk},
        ),
        data=payload,
    )


def _assert_unchanged(activity: Activity) -> None:
    activity.refresh_from_db()
    assert activity.config == FULL_CONFIG


def _error_locs(response) -> set[tuple[str, ...]]:
    return {tuple(detail['loc']) for detail in response.json()['detail']}


def test_changing_ticket_price_keeps_the_registration_block(
    dmr_client: DMRClient,
    activity: Activity,
) -> None:
    """The headline regression: price edit must not erase registration."""
    response = _patch_config(dmr_client, activity, {'tickets': {'price': 88.0}})

    assert response.status_code == HTTPStatus.OK, response.content
    assert response.headers['Content-Type'] == 'application/json'
    assert response.json() == {
        'config': {
            'tickets': {'price': 88.0},
            # The whole block the request did not send is still there:
            'registration': {'enabled': True, 'capacity': 100},
        },
    }
    activity.refresh_from_db()
    assert activity.config == {
        'tickets': {'price': 88.0},
        'registration': {'enabled': True, 'capacity': 100},
    }


def test_changing_one_registration_field_keeps_the_other(
    dmr_client: DMRClient,
    activity: Activity,
) -> None:
    """Raising the capacity must not silently turn the sign-up switch off."""
    response = _patch_config(
        dmr_client,
        activity,
        {'registration': {'capacity': 200}},
    )

    assert response.status_code == HTTPStatus.OK, response.content
    activity.refresh_from_db()
    assert activity.config['registration'] == {
        'enabled': True,
        'capacity': 200,
    }
    # Unrelated sections stay byte-for-byte the same.
    assert activity.config['tickets'] == {'price': 99.0}


def test_each_nested_layer_only_changes_what_it_sends(
    dmr_client: DMRClient,
    activity: Activity,
) -> None:
    """Deep merge: sent leaves change, sibling leaves survive."""
    response = _patch_config(
        dmr_client,
        activity,
        {
            'tickets': {'price': 1.0},
            'registration': {'enabled': False},
        },
    )

    assert response.status_code == HTTPStatus.OK, response.content
    activity.refresh_from_db()
    assert activity.config == {
        'tickets': {'price': 1.0},
        'registration': {'enabled': False, 'capacity': 100},
    }


def test_null_value_deletes_the_item(
    dmr_client: DMRClient,
    activity: Activity,
) -> None:
    """An explicit ``null`` removes the whole optional section."""
    response = _patch_config(dmr_client, activity, {'tickets': None})

    assert response.status_code == HTTPStatus.OK, response.content
    assert response.json() == {
        'config': {
            'registration': {'enabled': True, 'capacity': 100},
        },
    }
    activity.refresh_from_db()
    assert 'tickets' not in activity.config
    # Deleting one section does not touch the other one.
    assert activity.config['registration'] == FULL_CONFIG['registration']


def test_null_on_required_field_fails_without_changing_anything(
    dmr_client: DMRClient,
    activity: Activity,
) -> None:
    """Deleting a required leaf makes the merged document invalid."""
    response = _patch_config(
        dmr_client,
        activity,
        {'registration': {'capacity': None}},
    )

    assert response.status_code == HTTPStatus.BAD_REQUEST, response.content
    assert ('registration', 'capacity') in _error_locs(response)
    _assert_unchanged(activity)


@pytest.mark.parametrize(
    'payload',
    [
        pytest.param({}, id='empty_root'),
        pytest.param({'tickets': {}}, id='empty_tickets_section'),
        pytest.param({'registration': {}}, id='empty_registration_section'),
    ],
)
def test_empty_object_keeps_the_original_value(
    dmr_client: DMRClient,
    activity: Activity,
    payload: dict[str, Any],
) -> None:
    """``{}`` at any layer is a no-op, including the document root."""
    response = _patch_config(dmr_client, activity, payload)

    assert response.status_code == HTTPStatus.OK, response.content
    assert response.json() == {'config': FULL_CONFIG}
    _assert_unchanged(activity)


def test_noop_patch_does_not_bump_updated_at(
    dmr_client: DMRClient,
    activity: Activity,
) -> None:
    """Keeping everything must not perform a write at all."""
    frozen = timezone.now() - dt.timedelta(days=2)
    Activity.objects.filter(pk=activity.pk).update(updated_at=frozen)

    response = _patch_config(dmr_client, activity, {})

    assert response.status_code == HTTPStatus.OK, response.content
    activity.refresh_from_db()
    assert activity.updated_at == frozen


def test_unknown_fields_report_their_full_dotted_paths(
    dmr_client: DMRClient,
    activity: Activity,
) -> None:
    """Unknown keys at every depth are named, all at once."""
    response = _patch_config(
        dmr_client,
        activity,
        {
            'venue': 'main hall',
            'registration': {
                'max_guests': 5,
                'tier': {'name': 'vip'},
            },
        },
    )

    assert response.status_code == HTTPStatus.BAD_REQUEST, response.content
    errors = response.json()['detail']
    assert {tuple(error['loc']) for error in errors} == {
        ('venue',),
        ('registration', 'max_guests'),
        ('registration', 'tier'),
    }
    # Every error is an unknown-field error and names its full path.
    assert all(error['type'] == 'unknown_field' for error in errors)
    assert all('.'.join(error['loc']) in error['msg'] for error in errors)
    _assert_unchanged(activity)


def test_unknown_fields_and_invalid_values_are_collected_together(
    dmr_client: DMRClient,
    activity: Activity,
) -> None:
    """One request reports every problem instead of the first one only."""
    response = _patch_config(
        dmr_client,
        activity,
        {
            'tickets': {'price': -10},
            'registration': {'bogus': 1},
        },
    )

    assert response.status_code == HTTPStatus.BAD_REQUEST, response.content
    errors = response.json()['detail']
    assert {tuple(error['loc']) for error in errors} == {
        ('tickets', 'price'),
        ('registration', 'bogus'),
    }
    assert {error['type'] for error in errors} == {
        'unknown_field',
        'greater_than',
    }
    _assert_unchanged(activity)


@pytest.mark.parametrize(
    'payload',
    [
        pytest.param({'tickets': {'price': 0}}, id='zero_price'),
        pytest.param({'tickets': {'price': 'free'}}, id='non_numeric_price'),
        pytest.param(
            {'registration': {'enabled': 'yes'}},
            id='non_boolean_switch',
        ),
        pytest.param(
            {'registration': {'capacity': 3.5}},
            id='fractional_capacity',
        ),
        pytest.param({'registration': 5}, id='section_as_scalar'),
        pytest.param({'tickets': []}, id='section_as_list'),
    ],
)
def test_invalid_merged_config_is_rejected_atomically(
    dmr_client: DMRClient,
    activity: Activity,
    payload: dict[str, Any],
) -> None:
    """No invalid value may survive: failure leaves the config pristine."""
    response = _patch_config(dmr_client, activity, payload)

    assert response.status_code == HTTPStatus.BAD_REQUEST, response.content
    errors = response.json()['detail']
    assert errors, 'at least one problem must be reported'
    assert all(error['loc'] for error in errors)
    _assert_unchanged(activity)


def test_failed_merge_does_not_mutate_already_sent_sibling_values(
    dmr_client: DMRClient,
    activity: Activity,
) -> None:
    """A valid price in the same request as a bad value is NOT saved."""
    response = _patch_config(
        dmr_client,
        activity,
        {
            'tickets': {'price': 66.0},
            'registration': {'capacity': -1},
        },
    )

    assert response.status_code == HTTPStatus.BAD_REQUEST, response.content
    assert _error_locs(response) == {('registration', 'capacity')}
    _assert_unchanged(activity)


def test_repeated_partial_saves_accumulate(
    dmr_client: DMRClient,
    activity: Activity,
) -> None:
    """Consecutive edits accumulate instead of resetting each other."""
    first = _patch_config(dmr_client, activity, {'tickets': {'price': 70.0}})
    second = _patch_config(
        dmr_client,
        activity,
        {'registration': {'enabled': False}},
    )

    assert first.status_code == HTTPStatus.OK
    assert second.status_code == HTTPStatus.OK
    activity.refresh_from_db()
    assert activity.config == {
        'tickets': {'price': 70.0},
        'registration': {'enabled': False, 'capacity': 100},
    }


def test_unknown_activity_returns_404(
    dmr_client: DMRClient,
    activity: Activity,
) -> None:
    """Editing a config of a missing activity answers ``404``."""
    response = dmr_client.patch(
        reverse(
            'api:activities:activity_config',
            kwargs={'activity_id': activity.pk + 999},
        ),
        data={},
    )

    assert response.status_code == HTTPStatus.NOT_FOUND


@pytest.mark.parametrize(
    'payload',
    [
        pytest.param([], id='array_body'),
        pytest.param('price=88', id='string_body'),
    ],
)
def test_non_object_body_is_rejected(
    dmr_client: DMRClient,
    activity: Activity,
    payload: Any,
) -> None:
    """Structural request errors reject the whole request as ``400``."""
    response = _patch_config(dmr_client, activity, payload)

    assert response.status_code == HTTPStatus.BAD_REQUEST
    _assert_unchanged(activity)
