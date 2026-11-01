import pytest
from django.core.cache import cache


@pytest.fixture(autouse=True)
def _clean_idempotency_cache() -> None:
    """Each test starts with an empty idempotency cache."""
    cache.clear()
