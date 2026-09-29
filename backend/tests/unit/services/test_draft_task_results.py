"""GOO-297: retained draft task terminal results.

aiosqlite, table subset only (pattern ``test_kpi_tenant_isolation.py``). The
concurrent real-PostgreSQL versions live in
``backend/tests/integration/test_draft_task_results_postgres.py``.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from uuid import uuid4

import pytest
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, create_async_engine

from src.models.draft_task_result import DraftTaskResult

pytestmark = pytest.mark.unit


@pytest.fixture
async def engine() -> AsyncIterator[AsyncEngine]:
    eng = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with eng.begin() as conn:
        await conn.run_sync(DraftTaskResult.__table__.create)
    yield eng
    await eng.dispose()


async def test_completed_requires_exact_artifact(engine: AsyncEngine) -> None:
    async with AsyncSession(engine) as db:
        db.add(
            DraftTaskResult(
                task_id="t1",
                collection_id=uuid4(),
                actor_user_id=uuid4(),
                state="completed",
                request_fingerprint="a" * 64,
            )
        )
        with pytest.raises(IntegrityError):
            await db.commit()
