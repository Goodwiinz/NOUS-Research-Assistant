"""GOO-299 backfill: dry-run by default, identifier-only, rerun is a no-op."""

from collections.abc import AsyncIterator
from datetime import datetime, timedelta, timezone
from typing import Any, cast
from uuid import UUID, uuid4

import pytest
from sqlalchemy import event, func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from scripts.backfill_report_identities import backfill
from src.models.base import Base
from src.models.research_blueprint import ResearchBlueprint
from src.models.research_decision import ResearchDecisionEvent, ResearchDecisionStream
from src.models.research_project import ResearchProject
from src.models.research_report import (
    ResearchReport,
    ResearchReportIdentifier,
    ResearchReportObservation,
    ResearchStudy,
)
from src.models.research_run import ResearchRun
from src.models.research_source import ResearchSource

T0 = datetime(2026, 9, 1, tzinfo=timezone.utc)


@pytest.fixture
async def factory() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")

    @event.listens_for(engine.sync_engine, "connect")
    def _now(connection: Any, _record: Any) -> None:
        # Identity and ledger tables use PostgreSQL's now() server default.
        connection.create_function(
            "now", 0, lambda: datetime.now(timezone.utc).isoformat(" ")
        )

    tables = [
        ResearchProject.__table__,
        ResearchBlueprint.__table__,
        ResearchRun.__table__,
        ResearchSource.__table__,
        ResearchDecisionStream.__table__,
        ResearchDecisionEvent.__table__,
        ResearchStudy.__table__,
        ResearchReport.__table__,
        ResearchReportIdentifier.__table__,
        ResearchReportObservation.__table__,
    ]
    async with engine.begin() as connection:
        await connection.run_sync(
            lambda sync: Base.metadata.create_all(sync, tables=tables)
        )
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


async def _run(db: AsyncSession, owner_id: UUID, collection_id: UUID | None) -> UUID:
    project = ResearchProject(name="p", owner_id=owner_id, collection_id=collection_id)
    db.add(project)
    await db.flush()
    blueprint = ResearchBlueprint(project_id=project.id, name="b")
    db.add(blueprint)
    await db.flush()
    run = ResearchRun(blueprint_id=blueprint.id, blueprint_version=1)
    db.add(run)
    await db.flush()
    return cast(UUID, run.id)


async def _seed(factory: async_sessionmaker[AsyncSession]) -> UUID:
    collection_id = uuid4()
    async with factory() as db:
        owner_id = uuid4()  # SQLite leaves FKs unenforced; no encrypted user row
        mapped = await _run(db, owner_id, collection_id)
        legacy = await _run(db, owner_id, None)
        rows = [
            (mapped, "crossref", "A trial", {"doi": "10.1000/a"}),
            (mapped, "openalex", "A trial.", {"doi": "10.1000/A", "openalex": "W1"}),
            # Same title, different DOI: must stay a separate report.
            (mapped, "crossref", "A trial", {"doi": "10.1000/b"}),
            (mapped, "rag_store", "Uploaded", {"rag_store": "doc", "doi": "10.1000/a"}),
            (legacy, "crossref", "Legacy", {"doi": "10.1000/legacy"}),
        ]
        for index, (run_id, connector, title, ids) in enumerate(rows):
            db.add(
                ResearchSource(
                    run_id=run_id,
                    connector_type=connector,
                    title=title,
                    metadata_={"identifiers": ids},
                    created_at=T0 + timedelta(minutes=index),
                )
            )
        await db.commit()
    return collection_id


async def _count(factory: async_sessionmaker[AsyncSession], model: object) -> int:
    async with factory() as db:
        return int(
            (await db.execute(select(func.count()).select_from(model))).scalar_one()  # type: ignore[arg-type]
        )


@pytest.mark.asyncio
async def test_dry_run_writes_nothing_and_apply_is_idempotent(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    await _seed(factory)
    expected = {
        "collections": 1,
        "sources": 5,
        "would_create_reports": 2,
        "would_create_observations": 3,
        "skipped_rag_store": 1,
        "mapping_required": 1,
    }

    assert await backfill(factory, apply=False) == expected
    assert await _count(factory, ResearchReport) == 0
    assert await _count(factory, ResearchReportObservation) == 0

    assert await backfill(factory, apply=True) == expected
    assert await _count(factory, ResearchReport) == 2
    assert await _count(factory, ResearchReportIdentifier) == 3
    assert await _count(factory, ResearchReportObservation) == 3
    assert await _count(factory, ResearchSource) == 5  # originals untouched

    rerun = await backfill(factory, apply=True)
    assert rerun["would_create_reports"] == 0
    assert rerun["would_create_observations"] == 0
    assert await _count(factory, ResearchReport) == 2
