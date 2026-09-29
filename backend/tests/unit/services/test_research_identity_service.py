"""Focused guards for the GOO-299 identity service (PostgreSQL proof lives in
``tests/integration/test_report_identity_postgres.py``)."""

from collections.abc import AsyncIterator
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import pytest
from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from src.models.base import Base
from src.models.research_project_role import ResearchProjectRole
from src.models.research_protocol import ResearchProtocol
from src.schemas.research_engine import StudyLinkRequest
from src.services.research_engine import identity_service
from src.services.research_engine.project_access import ProjectContext


@pytest.mark.asyncio
@pytest.mark.parametrize("adjudicated", ["confirmed", "disputed"])
async def test_reviewer_proposal_cannot_overwrite_an_adjudicated_link(
    monkeypatch: pytest.MonkeyPatch, adjudicated: str
) -> None:
    collection_id, report_id, study_id = uuid4(), uuid4(), uuid4()
    report = SimpleNamespace(
        id=report_id,
        title_snapshot="Paper",
        study_id=study_id,
        study_link_status=adjudicated,
        study_link_actor_id=None,
        study_link_rationale="adjudicated",
    )

    async def live_reports(*_args: Any, **_kwargs: Any) -> dict[UUID, Any]:
        return {report_id: report}

    monkeypatch.setattr(identity_service, "_lock", AsyncMock())
    monkeypatch.setattr(
        identity_service, "_replayed_event", AsyncMock(return_value=None)
    )
    monkeypatch.setattr(identity_service, "_live_reports", live_reports)
    append = AsyncMock()
    monkeypatch.setattr(identity_service, "_append", append)
    context = cast(
        ProjectContext,
        SimpleNamespace(
            collection=SimpleNamespace(id=collection_id),
            effective_roles=frozenset({ResearchProjectRole.REVIEWER}),
        ),
    )

    with pytest.raises(HTTPException) as blocked:
        await identity_service.link_study(
            cast(AsyncSession, AsyncMock()),
            context,
            report_id,
            uuid4(),
            StudyLinkRequest(
                study_id=uuid4(),
                status="proposed",
                rationale="re-point",
                idempotency_key="k",
            ),
        )

    assert blocked.value.status_code == 409
    assert (report.study_id, report.study_link_status) == (study_id, adjudicated)
    append.assert_not_awaited()


@pytest.fixture
async def protocol_db() -> AsyncIterator[AsyncSession]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(
            lambda sync: Base.metadata.create_all(
                sync, tables=[ResearchProtocol.__table__]
            )
        )
    async with async_sessionmaker(engine, expire_on_commit=False)() as session:
        yield session
    await engine.dispose()


@pytest.mark.asyncio
async def test_governing_protocol_version_is_an_approved_one(
    protocol_db: AsyncSession,
) -> None:
    collection_id, approved = uuid4(), uuid4()
    older = datetime(2026, 9, 1, tzinfo=timezone.utc)
    protocol_db.add_all(
        [
            ResearchProtocol(
                collection_id=collection_id,
                name="draft only",
                created_at=older,
                updated_at=older,
            ),
            ResearchProtocol(
                collection_id=collection_id,
                name="approved",
                current_approved_version_id=approved,
                created_at=older + timedelta(days=1),
                updated_at=older + timedelta(days=1),
            ),
        ]
    )
    await protocol_db.flush()

    assert await identity_service._protocol_version_id(
        protocol_db, collection_id
    ) == str(approved)
    assert await identity_service._protocol_version_id(protocol_db, uuid4()) is None
