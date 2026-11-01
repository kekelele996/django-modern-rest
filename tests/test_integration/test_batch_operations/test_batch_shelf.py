from collections.abc import Callable
from http import HTTPStatus
from typing import Any

import pytest
from django.db import IntegrityError
from django.urls import reverse
from faker import Faker

from dmr.test import DMRClient
from server.apps.batch_operations.models import (
    Product,
    ShelfOperationLog,
    ShelfStatus,
)

pytestmark = pytest.mark.django_db


@pytest.fixture
def product_factory(
    faker: Faker,
) -> Callable[..., Product]:
    """Create products with unique SKUs."""
    counter = 0

    def factory(
        *,
        status: str = ShelfStatus.OFF_SHELF,
    ) -> Product:
        nonlocal counter
        counter += 1
        return Product.objects.create(
            sku=f'{faker.slug()}-{counter}',
            name=faker.word(),
            status=status,
        )

    return factory


def test_batch_shelf_all_items_succeed(
    dmr_client: DMRClient,
    product_factory: Callable[..., Product],
) -> None:
    """All items succeed: ``200`` with one success result per item."""
    first = product_factory()
    second = product_factory(status=ShelfStatus.ON_SHELF)
    payload = {
        'operations': [
            {'product_id': first.pk, 'action': 'list'},
            {'product_id': second.pk, 'action': 'delist'},
        ],
    }

    response = dmr_client.post(
        reverse('api:batch_operations:shelf_batch'),
        data=payload,
    )

    assert response.status_code == HTTPStatus.OK, response.content
    assert response.headers['Content-Type'] == 'application/json'
    assert response.json() == {
        'total': 2,
        'succeeded': 2,
        'failed': 0,
        'results': [
            {
                'index': 1,
                'product_id': first.pk,
                'action': 'list',
                'success': True,
                'status': 'on',
                'error': None,
            },
            {
                'index': 2,
                'product_id': second.pk,
                'action': 'delist',
                'success': True,
                'status': 'off',
                'error': None,
            },
        ],
    }
    first.refresh_from_db()
    second.refresh_from_db()
    assert first.status == ShelfStatus.ON_SHELF
    assert second.status == ShelfStatus.OFF_SHELF
    # Each successful item wrote its audit log.
    assert list(
        ShelfOperationLog.objects.values_list('action', flat=True),
    ) == ['list', 'delist']


def test_batch_shelf_partial_failures_are_indexed_and_isolated(
    dmr_client: DMRClient,
    product_factory: Callable[..., Product],
) -> None:
    """Unknown action and missing product fail only their own items."""
    first = product_factory()
    third = product_factory()
    payload = {
        'operations': [
            {'product_id': first.pk, 'action': 'list'},
            {'product_id': third.pk + 999, 'action': 'explode'},
            {'product_id': third.pk, 'action': 'list'},
            {'product_id': third.pk + 999, 'action': 'delist'},
        ],
    }

    response = dmr_client.post(
        reverse('api:batch_operations:shelf_batch'),
        data=payload,
    )

    # Mixed outcome is reported as `207 Multi-Status`.
    assert response.status_code == HTTPStatus.MULTI_STATUS, response.content
    data = response.json()
    assert data['total'] == 4
    assert data['succeeded'] == 2
    assert data['failed'] == 2

    results = data['results']
    assert [item['index'] for item in results] == [1, 2, 3, 4]
    assert results[0]['success'] is True
    assert results[0]['status'] == 'on'
    assert results[2]['success'] is True
    assert results[2]['status'] == 'on'

    # The on-duty colleague must see WHICH item failed and why.
    assert results[1] == {
        'index': 2,
        'product_id': third.pk + 999,
        'action': 'explode',
        'success': False,
        'status': None,
        'error': {
            'code': 'unknown_action',
            'message': (
                "Unsupported operation type 'explode', "
                'expected one of: delist, list'
            ),
        },
    }
    assert results[3] == {
        'index': 4,
        'product_id': third.pk + 999,
        'action': 'delist',
        'success': False,
        'status': None,
        'error': {
            'code': 'product_not_found',
            'message': f'Product {third.pk + 999} does not exist',
        },
    }

    # Items around the failures still took effect.
    first.refresh_from_db()
    third.refresh_from_db()
    assert first.status == ShelfStatus.ON_SHELF
    assert third.status == ShelfStatus.ON_SHELF
    # Failed items left no audit log behind.
    assert ShelfOperationLog.objects.count() == 2


@pytest.mark.parametrize(
    'failing_index',
    [1, 2],
)
def test_failed_item_rolls_back_without_a_trace(
    dmr_client: DMRClient,
    monkeypatch: pytest.MonkeyPatch,
    product_factory: Callable[..., Product],
    *,
    failing_index: int,
) -> None:
    """A crash after the status write rolls the whole item back."""
    victim = product_factory()
    survivor = product_factory()
    ordered = [victim, survivor]
    if failing_index == 2:
        ordered.reverse()
    operations = [
        {'product_id': product.pk, 'action': 'list'} for product in ordered
    ]
    original_create = ShelfOperationLog.objects.create

    def flaky_create(**kwargs: Any) -> ShelfOperationLog:
        if kwargs['product'] == victim:
            raise IntegrityError('simulated failure mid-operation')
        return original_create(**kwargs)

    monkeypatch.setattr(ShelfOperationLog.objects, 'create', flaky_create)

    response = dmr_client.post(
        reverse('api:batch_operations:shelf_batch'),
        data={'operations': operations},
    )

    assert response.status_code == HTTPStatus.MULTI_STATUS, response.content
    data = response.json()
    assert data['succeeded'] == 1
    assert data['failed'] == 1
    failed_item = data['results'][failing_index - 1]
    assert failed_item['index'] == failing_index
    assert failed_item['success'] is False
    assert failed_item['error'] == {
        'code': 'operation_failed',
        'message': 'Operation was rolled back due to a database error',
    }

    victim.refresh_from_db()
    survivor.refresh_from_db()
    # The victim's status change was rolled back together with its log.
    assert victim.status == ShelfStatus.OFF_SHELF
    assert not victim.shelf_logs.exists()
    # Processing continued past the failed item.
    assert survivor.status == ShelfStatus.ON_SHELF
    assert survivor.shelf_logs.count() == 1


def test_unknown_actions_only_affect_their_own_items(
    dmr_client: DMRClient,
    product_factory: Callable[..., Product],
) -> None:
    """A batch of unknown actions reports every item individually."""
    product = product_factory()
    payload = {
        'operations': [
            {'product_id': product.pk, 'action': 'freeze'},
            {'product_id': product.pk, 'action': 'teleport'},
        ],
    }

    response = dmr_client.post(
        reverse('api:batch_operations:shelf_batch'),
        data=payload,
    )

    assert response.status_code == HTTPStatus.MULTI_STATUS, response.content
    data = response.json()
    assert data['succeeded'] == 0
    assert data['failed'] == 2
    assert [item['index'] for item in data['results']] == [1, 2]
    assert [item['error']['code'] for item in data['results']] == [
        'unknown_action',
        'unknown_action',
    ]
    product.refresh_from_db()
    assert product.status == ShelfStatus.OFF_SHELF
    assert ShelfOperationLog.objects.count() == 0


@pytest.mark.parametrize(
    'payload',
    [
        pytest.param({}, id='missing_operations'),
        pytest.param({'operations': []}, id='empty_batch'),
        pytest.param(
            {'operations': [{'product_id': 0, 'action': 'list'}]},
            id='invalid_product_id',
        ),
        pytest.param(
            {'operations': [{'product_id': 1}]},
            id='missing_action',
        ),
    ],
)
def test_malformed_batch_is_rejected_as_a_whole(
    dmr_client: DMRClient,
    payload: dict[str, object],
) -> None:
    """Structural request errors still reject the whole request."""
    response = dmr_client.post(
        reverse('api:batch_operations:shelf_batch'),
        data=payload,
    )

    assert response.status_code == HTTPStatus.BAD_REQUEST, response.content
