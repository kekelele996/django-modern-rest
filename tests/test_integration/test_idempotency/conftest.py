import pytest
from django.core.cache import cache

from server.apps.middlewares import views  # type: ignore[import-not-found]


@pytest.fixture(autouse=True)
def _reset_idempotency_state() -> None:
    """Empty the cache and the charge ledger before every test."""
    cache.clear()
    views.reset_charges()


def pytest_collection_modifyitems(
    session: pytest.Session,
    config: pytest.Config,
    items: list[pytest.Item],  # noqa: WPS110
) -> None:
    """Run all idempotency tests on a single worker.

    The locmem cache and the in-process charge ledger are not shared
    between xdist workers, parallel runs would observe state created
    by a different worker.
    """
    for test_item in items:
        test_item.add_marker(pytest.mark.xdist_group('idempotency'))
