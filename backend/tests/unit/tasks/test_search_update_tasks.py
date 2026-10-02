"""GOO-319 beat tick: a no-op until SEARCH_UPDATES_ENABLED."""

import pytest

from src.core.config import settings
from src.tasks import search_update_tasks


async def test_tick_self_skips_when_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "SEARCH_UPDATES_ENABLED", False)

    def explode() -> None:
        raise AssertionError("no session may open while disabled")

    monkeypatch.setattr("src.core.database.AsyncSessionLocal", explode)
    assert await search_update_tasks._tick() == 0
