"""Real PostgreSQL proof for GOO-299 report/study identity guards.

The four identity tables are created by revision ``c9d2e4f6a8b1`` itself, not by
``Base.metadata``, so the migration's constraints are what these tests exercise.

Mutation verification (docs/engineering/testing.md), full record in
``docs/testing/agent-orchestration-mutation-checks.md`` (GOO-299 section).
Each mutant was restored from a byte-for-byte copy (``cmp``) and rerun green:

- ``identity_service.observe_sources`` project lock (``await _lock(...)``,
  identity_service.py:275) removed -> ``-k idempotent`` fails with IntegrityError
  ``uq_research_report_identifier_value`` (two importers created one DOI twice).
- ``identity_service.observe_sources`` already-observed guard
  (``if source.id in observed: continue``, :310) disabled -> ``-k idempotent``
  fails ``assert 3 == 2`` (re-import minted an orphan report for the
  identifier-less source; ``on_conflict_do_nothing`` never reaches it).
- ``identity_service.merge_reports`` project lock (:547) replaced by a plain
  stream read -> ``-k concurrent_merges`` fails ``DID NOT RAISE HTTPException``
  (the second merge re-points r2 to r3 instead of returning 409).
"""

import asyncio
import importlib.util
import os
from pathlib import Path
from typing import Any, AsyncIterator, Callable, cast
from uuid import UUID, uuid4

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from fastapi import HTTPException
from sqlalchemy import func, select, text
from sqlalchemy.engine import Connection, make_url
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from src.models import Base
from src.models.collection import Collection
from src.models.research_blueprint import ResearchBlueprint
from src.models.research_project import ResearchProject
from src.models.research_project_role import (
    ResearchProjectRole,
    ResearchProjectRoleAssignment,
)
from src.models.research_report import (
    ResearchReport,
    ResearchReportIdentifier,
    ResearchReportObservation,
)
from src.models.research_run import ResearchRun
from src.models.research_source import ResearchSource
from src.models.user import User
from src.models.workspace import Workspace, WorkspaceMember, WorkspaceRole
from src.schemas.research_engine import ReportMergeRequest, StudyLinkRequest
from src.services.research_engine import identity_service
from src.services.research_engine.project_access import (
    ProjectContext,
    ResearchAction,
    resolve_project,
)

pytestmark = pytest.mark.integration

VERSIONS = Path(__file__).parents[2] / "alembic" / "versions"
_IDENTITY_TABLES = (
    # GOO-301/302 screening tables reference research_reports: drop them first,
    # resolutions before the tables they reference.
    "screening_resolutions",
    "screening_suggestions",
    "screening_observations",
    "screening_assignments",
    "screening_queues",
    "research_import_records",
    "research_import_receipts",
    "research_report_observations",
    "research_report_identifiers",
    "research_reports",
    "research_studies",
)


def _upgrade(connection: Connection) -> None:
    # GOO-300's import tables reference research_reports, so they are rebuilt
    # by their own revision on top of the identity tables.
    for filename in (
        "c9d2e4f6a8b1_create_report_identities.py",
        "d4e6f8a0b2c3_create_search_imports.py",
    ):
        spec = importlib.util.spec_from_file_location(
            filename[:-3], VERSIONS / filename
        )
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        setattr(module, "op", Operations(MigrationContext.configure(connection)))
        cast(Callable[[], None], module.upgrade)()


@pytest.fixture
async def identity_factory(
    request: pytest.FixtureRequest,
) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    configured = os.getenv("RESEARCH_DECISION_DATABASE_URL")
    url = make_url(
        configured or str(request.getfixturevalue("postgres_container")["url"])
    ).set(drivername="postgresql+asyncpg")
    schema = f"test_report_identity_{uuid4().hex}"
    admin = create_async_engine(url)
    async with admin.begin() as connection:
        await connection.exec_driver_sql(f'CREATE SCHEMA "{schema}"')
    engine = create_async_engine(
        url, connect_args={"server_settings": {"search_path": schema}}
    )
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
        for table in _IDENTITY_TABLES:
            await connection.exec_driver_sql(f'DROP TABLE "{table}"')
        await connection.run_sync(_upgrade)
    try:
        yield async_sessionmaker(engine, expire_on_commit=False)
    finally:
        await engine.dispose()
        async with admin.begin() as connection:
            await connection.exec_driver_sql(f'DROP SCHEMA "{schema}" CASCADE')
        await admin.dispose()


async def _seed(factory: async_sessionmaker[AsyncSession]) -> dict[str, UUID]:
    """Owner O; reviewer R; adjudicator A; role-less viewer V; foreign-org F."""
    ids = {k: uuid4() for k in ("org", "foreign_org", "O", "R", "A", "V", "F")}
    ids.update({k: uuid4() for k in ("workspace", "collection", "run")})
    async with factory() as db:
        for org in ("org", "foreign_org"):
            await db.execute(
                text("""INSERT INTO organizations
                    (id,name,storage_tier,storage_used_bytes,storage_limit_bytes,
                     is_active,created_at,updated_at,is_deleted)
                    VALUES (:id,:name,'FREE',0,1,true,now(),now(),false)"""),
                {"id": ids[org], "name": f"identity-{ids[org]}"},
            )
        for key in ("O", "R", "A", "V", "F"):
            db.add(
                User(
                    id=ids[key],
                    email=f"{ids[key]}@test.invalid",
                    password_hash="unused",
                    first_name="Test",
                    last_name=key,
                    organization_id=ids["foreign_org" if key == "F" else "org"],
                )
            )
        await db.flush()
        db.add(
            Workspace(
                id=ids["workspace"],
                name="identity",
                owner_id=ids["O"],
                organization_id=ids["org"],
            )
        )
        await db.flush()
        db.add(
            Collection(
                id=ids["collection"], workspace_id=ids["workspace"], name="identity"
            )
        )
        for key in ("R", "A", "V"):
            db.add(
                WorkspaceMember(
                    workspace_id=ids["workspace"],
                    user_id=ids[key],
                    role=WorkspaceRole.VIEWER,
                )
            )
        await db.flush()
        for key, role in (
            ("R", ResearchProjectRole.REVIEWER),
            ("A", ResearchProjectRole.ADJUDICATOR),
        ):
            db.add(
                ResearchProjectRoleAssignment(
                    collection_id=ids["collection"],
                    user_id=ids[key],
                    role=role,
                    assigned_by_id=ids["O"],
                )
            )
        engine_project = ResearchProject(
            name="identity", owner_id=ids["O"], collection_id=ids["collection"]
        )
        db.add(engine_project)
        await db.flush()
        blueprint = ResearchBlueprint(project_id=engine_project.id, name="identity")
        db.add(blueprint)
        await db.flush()
        db.add(
            ResearchRun(id=ids["run"], blueprint_id=blueprint.id, blueprint_version=1)
        )
        await db.commit()
    return ids


async def _sources(
    factory: async_sessionmaker[AsyncSession],
    run_id: UUID,
    *identifiers: dict[str, str],
) -> list[ResearchSource]:
    async with factory() as db:
        rows = [
            ResearchSource(
                run_id=run_id,
                connector_type="openalex",
                title=f"Paper {index}",
                metadata_={"identifiers": ids, "publication_date": "2020-01-01"},
            )
            for index, ids in enumerate(identifiers)
        ]
        db.add_all(rows)
        await db.commit()
        return rows


async def _observe(
    factory: async_sessionmaker[AsyncSession],
    collection_id: UUID,
    sources: list[ResearchSource],
) -> None:
    async with factory() as db:
        await identity_service.observe_sources(
            db, collection_id=collection_id, sources=sources
        )
        await db.commit()


async def _count(db: AsyncSession, model: Any, collection_id: UUID) -> int:
    return cast(
        int,
        (
            await db.execute(
                select(func.count())
                .select_from(model)
                .where(model.collection_id == collection_id)
            )
        ).scalar_one(),
    )


async def _context(
    factory: async_sessionmaker[AsyncSession],
    collection_id: UUID,
    user_id: UUID,
    action: ResearchAction,
) -> ProjectContext:
    """Resolve and release the Collection lock so only the identity lock remains."""
    async with factory() as db:
        context = await resolve_project(db, collection_id, user_id, action)
        await db.commit()
        return context


@pytest.mark.asyncio
async def test_constraints(
    identity_factory: async_sessionmaker[AsyncSession],
) -> None:
    ids = await _seed(identity_factory)
    [source] = await _sources(identity_factory, ids["run"], {"doi": "10.1000/one"})
    await _observe(identity_factory, ids["collection"], [source])
    async with identity_factory() as db:
        report_id = (
            await db.execute(select(ResearchReportObservation.report_id))
        ).scalar_one()
        with pytest.raises(IntegrityError, match="uq_research_report_identifier"):
            async with db.begin_nested():
                db.add(
                    ResearchReportIdentifier(
                        collection_id=ids["collection"],
                        report_id=report_id,
                        kind="doi",
                        value="10.1000/one",
                    )
                )
                await db.flush()
        with pytest.raises(IntegrityError):
            async with db.begin_nested():
                await db.execute(
                    text("DELETE FROM research_sources WHERE id=:id"),
                    {"id": source.id},
                )
        with pytest.raises(IntegrityError):
            async with db.begin_nested():
                await db.execute(
                    text("DELETE FROM research_reports WHERE id=:id"),
                    {"id": report_id},
                )


@pytest.mark.asyncio
async def test_repeated_and_concurrent_imports_are_idempotent(
    identity_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ids = await _seed(identity_factory)
    collection_id = ids["collection"]
    sources = await _sources(
        identity_factory,
        ids["run"],
        {"doi": "10.1000/shared"},
        {"doi": "10.1000/shared", "pmid": "123"},
        {},  # no identifiers: its own report, and never a second one on re-import
    )
    for _attempt in range(2):
        await _observe(identity_factory, collection_id, sources)
        async with identity_factory() as db:
            assert await _count(db, ResearchReport, collection_id) == 2
            assert await _count(db, ResearchReportIdentifier, collection_id) == 2
            assert await _count(db, ResearchReportObservation, collection_id) == 3

    first, second = await _sources(
        identity_factory,
        ids["run"],
        {"doi": "10.1000/concurrent"},
        {"doi": "10.1000/concurrent", "openalex": "W1"},
    )
    # Force overlap: importer ``one`` pauses at its first flush, i.e. after it
    # loaded the identifier index and staged a new report but before writing,
    # until importer ``two`` has either finished or is seen waiting on a lock.
    paused, release = asyncio.Event(), asyncio.Event()
    original_flush = AsyncSession.flush

    async def import_one(db: AsyncSession, source: ResearchSource) -> None:
        await identity_service.observe_sources(
            db, collection_id=collection_id, sources=[source]
        )
        await db.commit()

    one, two, observer = identity_factory(), identity_factory(), identity_factory()

    async def flush_after_release(self: AsyncSession, *args: Any, **kw: Any) -> None:
        if self is one and not paused.is_set():
            paused.set()
            await release.wait()
        await original_flush(self, *args, **kw)

    monkeypatch.setattr(AsyncSession, "flush", flush_after_release)
    async with one, two, observer:
        pid = cast(
            int, (await two.execute(text("SELECT pg_backend_pid()"))).scalar_one()
        )
        winner = asyncio.create_task(import_one(one, first))
        await asyncio.wait_for(paused.wait(), 5)
        loser = asyncio.create_task(import_one(two, second))
        blocked = asyncio.create_task(_wait_until_blocked(observer, pid))
        await asyncio.wait({loser, blocked}, return_when=asyncio.FIRST_COMPLETED)
        release.set()
        try:
            # Without the lock the paused importer inserts the same DOI after the
            # other one committed: IntegrityError uq_research_report_identifier_value.
            await asyncio.gather(winner, loser)
        finally:
            for task in (winner, loser, blocked):
                if not task.done():
                    task.cancel()
            await asyncio.gather(winner, loser, blocked, return_exceptions=True)
        assert blocked.done() and blocked.exception() is None, "no lock wait seen"
    async with identity_factory() as db:
        report_ids = set(
            (
                await db.execute(
                    select(ResearchReportObservation.report_id).where(
                        ResearchReportObservation.source_id.in_([first.id, second.id])
                    )
                )
            )
            .scalars()
            .all()
        )
        assert len(report_ids) == 1
        assert await _count(db, ResearchReport, collection_id) == 3


@pytest.mark.asyncio
async def test_link_reload_history(
    identity_factory: async_sessionmaker[AsyncSession],
) -> None:
    ids = await _seed(identity_factory)
    collection_id = ids["collection"]
    [source] = await _sources(identity_factory, ids["run"], {"doi": "10.1000/link"})
    await _observe(identity_factory, collection_id, [source])
    async with identity_factory() as db:
        [report] = await identity_service.list_reports(db, collection_id=collection_id)

    async with identity_factory() as db:
        context = await resolve_project(
            db, collection_id, ids["R"], ResearchAction.REVIEW
        )
        proposed = await identity_service.link_study(
            db,
            context,
            report.id,
            ids["R"],
            StudyLinkRequest(
                study_id=None,
                status="proposed",
                rationale="same trial",
                idempotency_key="propose-1",
            ),
        )
        await db.commit()
    confirm = StudyLinkRequest(
        study_id=None,
        status="confirmed",
        rationale="checked registry",
        idempotency_key="confirm-1",
    )
    async with identity_factory() as db:
        context = await resolve_project(
            db, collection_id, ids["A"], ResearchAction.ADJUDICATE
        )
        await identity_service.link_study(db, context, report.id, ids["A"], confirm)
        await db.commit()

    async with identity_factory() as db:
        [reloaded] = await identity_service.list_reports(
            db, collection_id=collection_id
        )
        history = await identity_service.history(db, collection_id=collection_id)
    assert reloaded.study_link_status == "confirmed"
    assert reloaded.study_id == proposed.study_id
    assert [event.actor_role for event in history] == ["reviewer", "adjudicator"]
    assert history[0].payload["prior_study_id"] is None
    assert history[1].payload["prior_study_id"] == str(proposed.study_id)
    assert history[1].payload["prior_status"] == "proposed"
    assert "protocol_version_id" in history[1].payload

    # Same key + same body replays; same key + different body conflicts.
    async with identity_factory() as db:
        context = await resolve_project(
            db, collection_id, ids["A"], ResearchAction.ADJUDICATE
        )
        again = await identity_service.link_study(
            db, context, report.id, ids["A"], confirm
        )
        assert again.study_link_status == "confirmed"
        with pytest.raises(HTTPException) as conflict:
            await identity_service.link_study(
                db,
                context,
                report.id,
                ids["A"],
                confirm.model_copy(update={"rationale": "changed"}),
            )
        assert conflict.value.status_code == 409
        await db.rollback()
    async with identity_factory() as db:
        assert len(await identity_service.history(db, collection_id=collection_id)) == 2

    for user, status in (("V", 403), ("F", 404)):
        async with identity_factory() as db:
            with pytest.raises(HTTPException) as denied:
                await resolve_project(
                    db, collection_id, ids[user], ResearchAction.ADJUDICATE
                )
            assert denied.value.status_code == status
    async with identity_factory() as db:
        await db.execute(
            text("UPDATE collections SET research_status='archived' WHERE id=:id"),
            {"id": collection_id},
        )
        await db.commit()
    async with identity_factory() as db:
        with pytest.raises(HTTPException) as archived:
            await resolve_project(
                db, collection_id, ids["A"], ResearchAction.ADJUDICATE
            )
        assert archived.value.status_code == 409


async def _wait_until_blocked(observer: AsyncSession, pid: int) -> None:
    """Observe an actual PostgreSQL lock wait, never infer overlap from a sleep."""
    async with asyncio.timeout(5):
        while not (
            await observer.execute(
                text("SELECT cardinality(pg_blocking_pids(:pid)) > 0"), {"pid": pid}
            )
        ).scalar_one():
            await asyncio.sleep(0.01)


@pytest.mark.asyncio
async def test_concurrent_merges_serialize_without_partial_rewiring(
    identity_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Contexts are resolved up front, so only the identity stream lock serializes."""
    ids = await _seed(identity_factory)
    collection_id = ids["collection"]
    sources = await _sources(
        identity_factory,
        ids["run"],
        {"doi": "10.1000/r1"},
        {"doi": "10.1000/r2", "pmid": "2"},
        {"doi": "10.1000/r3"},
    )
    await _observe(identity_factory, collection_id, sources)
    async with identity_factory() as db:
        report_of: dict[Any, UUID] = dict(
            (
                await db.execute(
                    select(
                        ResearchReportObservation.source_id,
                        ResearchReportObservation.report_id,
                    )
                )
            )
            .tuples()
            .all()
        )
    r1, r2, r3 = (report_of[source.id] for source in sources)
    context = await _context(
        identity_factory, collection_id, ids["A"], ResearchAction.ADJUDICATE
    )

    paused, release = asyncio.Event(), asyncio.Event()
    original_append = identity_service.append_decision

    async def paused_append(*args: Any, **kwargs: Any) -> Any:
        if not paused.is_set():
            paused.set()
            await release.wait()
        return await original_append(*args, **kwargs)

    monkeypatch.setattr(identity_service, "append_decision", paused_append)

    async def merge(db: AsyncSession, surviving: UUID, key: str) -> None:
        await identity_service.merge_reports(
            db,
            context,
            ids["A"],
            ReportMergeRequest(
                surviving_report_id=surviving,
                merged_report_ids=[r2],
                rationale="duplicate",
                idempotency_key=key,
            ),
        )
        await db.commit()

    first, second, observer = identity_factory(), identity_factory(), identity_factory()
    async with first, second, observer:
        pid = cast(
            int, (await second.execute(text("SELECT pg_backend_pid()"))).scalar_one()
        )
        winner = asyncio.create_task(merge(first, r1, "merge-a"))
        await asyncio.wait_for(paused.wait(), 5)
        loser = asyncio.create_task(merge(second, r3, "merge-b"))
        try:
            await _wait_until_blocked(observer, pid)
            release.set()
            await winner
            with pytest.raises(HTTPException) as already:
                await loser
            assert already.value.status_code == 409
            assert already.value.detail == "Report already merged"
        finally:
            release.set()
            for task in (winner, loser):
                if not task.done():
                    task.cancel()
            await asyncio.gather(winner, loser, return_exceptions=True)
            await second.rollback()

    async with identity_factory() as db:
        owners: dict[Any, UUID] = dict(
            (
                await db.execute(
                    select(
                        ResearchReportObservation.source_id,
                        ResearchReportObservation.report_id,
                    )
                )
            )
            .tuples()
            .all()
        )
        identifier_owners = set(
            (
                await db.execute(
                    select(ResearchReportIdentifier.report_id).where(
                        ResearchReportIdentifier.value.in_(["10.1000/r2", "2"])
                    )
                )
            )
            .scalars()
            .all()
        )
        merged_into = (
            await db.execute(
                select(ResearchReport.merged_into_report_id).where(
                    ResearchReport.id == r2
                )
            )
        ).scalar_one()
    assert owners[sources[1].id] == r1
    r3_observations = [s for s, r in owners.items() if r == r3]
    assert r3_observations == [sources[2].id]
    assert identifier_owners == {r1}
    assert merged_into == r1
