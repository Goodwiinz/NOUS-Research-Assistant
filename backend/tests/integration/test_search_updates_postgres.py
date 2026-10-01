"""Real PostgreSQL proof for GOO-319 scheduled search updates.

Runs on GOO-301's ``screening_factory`` schema, whose migration chain now
ends at ``c2f4b6d8e0a1``: the four insert-only tables, their unique keys and
CHECKs, the triggers and the widened import receipt kind come from the
migration. Providers are fake connectors the test controls (real
``SourceDocument``/``SearchTrace`` shapes through the real
``discovery.search_sources``); Crossref notices come from the fake too. The
live providers, a real beat across a worker restart and the deployed
journey are NOT RUN here.

Seeds: GOO-299's owner O (made supervisor), reviewer R, adjudicator A,
foreign F; an approved protocol; a completed run whose manifest carries a
real ``_search_receipts_v1`` strategy (plus a tampered and a superseded
one); a baseline corpus A, B, C (PMID only), D, and E1/E2 sharing a title.

Mutation verification (docs/engineering/testing.md), full record in
``docs/testing/agent-orchestration-mutation-checks.md`` (GOO-319 section):
``insert_fire`` as a plain INSERT with the unique key dropped (step 3: two
executions), ``due_fire`` returning every missed fire (step 5: missed 0),
``not_returned`` classified ``corrected_retracted`` (step 6: D retracted), a
failed notice check read as no notices (step 7: unchanged after outage),
the owner recheck skipped (step 10: archived project runs), the protocol
recheck skipped (step 10: amended protocol runs the old plan), a random
``dedup_key`` (step 8: second receipt).

Command (from ``backend/``): ``RESEARCH_DECISION_DATABASE_URL=... pytest -q
tests/integration/test_search_updates_postgres.py``.
"""

import asyncio
import copy
import hashlib
import importlib.util
import json
import os
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any, Awaitable, Callable, TypeAlias, TypeVar, cast
from uuid import UUID, uuid4

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from fastapi import HTTPException
from sqlalchemy import text
from sqlalchemy.engine import Connection, make_url
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from src.models.base import Base
from src.models.research_import import ResearchImportRecord
from src.models.research_project_role import (
    ResearchProjectRole,
    ResearchProjectRoleAssignment,
)
from src.models.research_step import ResearchStep
from src.schemas.research_engine import (
    SearchScheduleCreate,
    SearchScheduleVersionCreate,
)
from src.services.research_decisions import replay_decisions
from src.services.research_engine import corpus_service
from src.services.research_engine import search_update_rules as rules
from src.services.research_engine import search_update_service as svc
from src.services.research_engine.connectors.base import (
    SearchTrace,
    SourceConnector,
    SourceDocument,
)
from src.services.research_engine.corpus_export import _canonical
from src.services.research_engine.project_access import (
    ProjectContext,
    ResearchAction,
    resolve_project,
)
from tests.integration.research_engine_postgres_support import (
    seed_approved_protocol_binding,
)
from tests.integration.test_report_identity_postgres import _seed
from tests.integration.test_screening_queue_postgres import (  # noqa: F401
    _REBUILT_TABLES,
    VERSIONS,
    _upgrade,
    screening_factory,
)

pytestmark = [pytest.mark.integration, pytest.mark.requires_postgres]

Factory: TypeAlias = async_sessionmaker[AsyncSession]
T = TypeVar("T")
VIEW, SUPERVISE = ResearchAction.VIEW, ResearchAction.SUPERVISE
CRON, TZ = "0 6 * * 1", "Europe/London"
TABLES = (
    "research_search_schedules",
    "research_search_executions",
    "research_search_execution_attempts",
    "research_search_execution_results",
)
DOI = {k: f"10.5555/{k}" for k in ("a", "b", "d", "e1", "e2", "f", "x")}
NOTICE = {
    "notice_doi": "10.5555/a.retraction",
    "type": "retraction",
    "date": "2026-09-01T00:00:00Z",
    "asserted_by": "publisher",
}


class Crash(BaseException):
    """A worker dying mid-run: escapes every ``except Exception``."""


class FakeProvider(SourceConnector):
    """A provider whose results, completion and notices the test sets."""

    def __init__(self, name: str) -> None:
        self.name = name
        self.endpoint = f"https://{name}.invalid/works"
        self.docs: list[SourceDocument] = []
        self.has_more = False
        self.crash = False
        self.searches = 0
        self.notices: dict[str, list[dict[str, Any]]] = {}
        self.notice_fail = False
        self.notice_crash = False
        self.notice_calls: list[list[str]] = []

    async def search(
        self,
        query: str,
        max_results: int = 50,
        *,
        search_trace: SearchTrace | None = None,
        **kwargs: Any,
    ) -> list[SourceDocument]:
        self.searches += 1
        if self.crash:
            raise Crash()
        docs = copy.deepcopy(self.docs)
        if search_trace is not None:
            await search_trace.record_page(
                endpoint=self.endpoint,
                params={"query": query, "rows": max_results},
                documents=docs,
                has_more=self.has_more,
                provider_count=len(docs),
            )
        return docs

    async def update_notices(self, dois: list[str]) -> dict[str, list[dict]]:
        self.notice_calls.append(list(dois))
        if self.notice_crash:
            raise Crash()
        if self.notice_fail:
            raise RuntimeError("crossref outage")
        return {doi: list(self.notices.get(doi, [])) for doi in dois}


def _crossref(key: str, title: str, updated: str = "2026-01-01T00:00:00Z") -> Any:
    return SourceDocument(
        connector_type="crossref",
        external_id=DOI[key],
        title=title,
        authors=["Ada Lovelace"],
        metadata={
            "doi": DOI[key],
            "journal": "J",
            "published": [[2021]],
            "provider_updated": updated,
        },
    )


def _pubmed(pmid: str, title: str) -> Any:
    return SourceDocument(
        connector_type="pubmed",
        external_id=pmid,
        title=title,
        authors=["Grace Hopper"],
        metadata={"pmid": pmid},
    )


def _strategy(protocol_version_id: str, step_id: str = "search") -> dict[str, Any]:
    strategy: dict[str, Any] = {
        "schema_version": "nous.academic.search-strategy.v1",
        "project_id": None,
        "protocol_version_id": protocol_version_id,
        "effective_plan_hash": "e" * 64,
        "blueprint_id": None,
        "blueprint_version": 1,
        "step_id": step_id,
        "intended": {
            "selected_providers": ["crossref", "pubmed"],
            "parameters": {"query_template": "$query"},
        },
        "route_limits": {
            "max_providers": 6,
            "max_results_per_provider": 50,
            "requested_results_per_provider": 10,
            "request_timeout_seconds": 90.0,
        },
    }
    strategy["strategy_version"] = rules.strategy_hash(strategy)
    return strategy


async def _as(
    w: Any,
    user: str,
    action: ResearchAction,
    call: Callable[[AsyncSession, ProjectContext], Awaitable[T]],
) -> T:
    """One route-shaped request: resolve, then the service (which commits)."""
    async with w.factory() as db:
        context = await resolve_project(db, w.p1, w.ids[user], action)
        return await call(db, context)


async def _refused(awaitable: Awaitable[Any], status: int, detail: str = "") -> None:
    with pytest.raises(HTTPException) as error:
        await awaitable
    assert error.value.status_code == status, error.value.detail
    assert detail in str(error.value.detail)


async def _scalar(factory: Factory, sql: str, **params: Any) -> Any:
    async with factory() as db:
        return (await db.execute(text(sql), params)).scalar_one()


async def _sql(factory: Factory, sql: str, **params: Any) -> None:
    async with factory() as db:
        await db.execute(text(sql), params)
        await db.commit()


async def _seed_world(factory: Factory) -> Any:
    ids = await _seed(factory)
    async with factory() as db:
        db.add(
            ResearchProjectRoleAssignment(
                collection_id=ids["collection"],
                user_id=ids["O"],
                role=ResearchProjectRole.SUPERVISOR,
                assigned_by_id=ids["O"],
            )
        )
        blueprint_id = (
            await db.execute(
                text("SELECT blueprint_id FROM research_runs WHERE id = :r"),
                {"r": ids["run"]},
            )
        ).scalar_one()
        binding = await seed_approved_protocol_binding(
            db,
            blueprint_id=blueprint_id,
            collection_id=ids["collection"],
            author_id=ids["O"],
            steps=[{"id": "search", "type": "search"}],
            parameters={},
        )
        version = str(binding.protocol_version_id)
        good = _strategy(version)
        tampered = {**_strategy(version, "search-2"), "route_limits": {}}
        superseded = _strategy(str(uuid4()), "search-3")
        journal: dict[str, Any] = {
            "schema_version": 1,
            "strategies": {
                good["strategy_version"]: good,
                _strategy(version, "search-2")["strategy_version"]: tampered,
                superseded["strategy_version"]: superseded,
            },
            "executions": {},
        }
        await db.execute(
            text("""UPDATE research_runs SET status = 'completed',
                    protocol_version_id = :v,
                    reproducibility_manifest = CAST(:m AS jsonb)
                    WHERE id = :r"""),
            {
                "v": binding.protocol_version_id,
                "m": json.dumps({"_search_receipts_v1": journal}),
                "r": ids["run"],
            },
        )
        for index, version_key in enumerate(journal["strategies"]):
            db.add(
                ResearchStep(
                    run_id=ids["run"],
                    step_index=index,
                    step_type="search",
                    output={
                        "query": "membrane imaging",
                        "coverage": {"strategy_version": version_key},
                    },
                )
            )
        await db.commit()
    return SimpleNamespace(
        factory=factory,
        ids=ids,
        p1=ids["collection"],
        protocol=binding.protocol_version_id,
        good=good,
        tampered_version=_strategy(version, "search-2")["strategy_version"],
        superseded=superseded,
    )


async def _seed_corpus(w: Any) -> dict[str, str]:
    """Baseline reports through one GOO-300 file-import receipt."""
    parsed = {
        "a": {"title": "Alpha", "identifiers": {"doi": DOI["a"]}},
        "b": {"title": "Beta", "identifiers": {"doi": DOI["b"]}},
        "c": {"title": "Gamma", "identifiers": {"pmid": "111"}},
        "d": {"title": "Delta", "identifiers": {"doi": DOI["d"]}},
        "e1": {"title": "Echo", "identifiers": {"doi": DOI["e1"]}},
        "e2": {"title": "Echo", "identifiers": {"doi": DOI["e2"]}},
    }
    async with w.factory() as db:
        await resolve_project(db, w.p1, w.ids["O"], ResearchAction.EDIT)
        records = [
            ResearchImportRecord(
                id=uuid4(),
                collection_id=w.p1,
                record_index=index,
                status="accepted",
                raw=json.dumps(fields),
                parsed={
                    **fields,
                    "authors": ["Ada Lovelace"],
                    "year": "2021",
                    "venue": "J",
                },
            )
            for index, fields in enumerate(parsed.values())
        ]
        await corpus_service.insert_receipt(
            db,
            collection_id=w.p1,
            actor_user_id=w.ids["O"],
            kind="file_import",
            dedup_key="file:baseline",
            lineage_key="b" * 64,
            declared={"database": "fixture"},
            observed={},
            records=records,
        )
        reports = {k: str(r.report_id) for k, r in zip(parsed, records)}
        await db.commit()
    return reports


def _create(w: Any, user: str, version: str, key: str, step: str = "search") -> Any:
    data = SearchScheduleCreate(
        source_run_id=w.ids["run"],
        step_id=step,
        strategy_version=version,
        cron=CRON,
        timezone=TZ,
        idempotency_key=key,
    )
    return _as(
        w,
        user,
        SUPERVISE,
        lambda db, ctx: svc.create_schedule(db, ctx, w.ids[user], data),
    )


async def _claim(w: Any, now: datetime) -> list[UUID]:
    async with w.factory() as db:
        return await svc.claim_due(db, now)


async def _run(w: Any, execution: UUID, now: datetime | None = None) -> str:
    async with w.factory() as db:
        return await svc.run_execution(
            db, execution, lambda _org: w.connectors, now=now, worker="test"
        )


async def _attempts(w: Any, execution: UUID) -> list[tuple[str, str | None]]:
    async with w.factory() as db:
        rows = (
            await db.execute(
                text("""SELECT outcome, reason FROM research_search_execution_attempts
                        WHERE execution_id = :e ORDER BY created_at, id"""),
                {"e": execution},
            )
        ).all()
    return [(r[0], r[1]) for r in rows]


async def _delta(w: Any, execution: UUID) -> dict[str, dict[str, Any]]:
    async with w.factory() as db:
        delta = await svc.accepted_delta(db, w.p1, execution)
    return {i.report_id: i.model_dump(mode="json", by_alias=True) for i in delta.items}


async def _downgrade_on_fresh_schema() -> None:
    """Step 12 on an empty schema: only the four tables drop and the kind
    CHECK is the two-value one again."""
    configured = os.environ["RESEARCH_DECISION_DATABASE_URL"]
    url = make_url(configured).set(drivername="postgresql+asyncpg")
    schema = f"test_search_down_{uuid4().hex}"
    admin = create_async_engine(url)
    async with admin.begin() as connection:
        await connection.exec_driver_sql(f'CREATE SCHEMA "{schema}"')
    engine = create_async_engine(
        url, connect_args={"server_settings": {"search_path": schema}}
    )

    def downgrade(connection: Connection) -> None:
        spec = importlib.util.spec_from_file_location(
            "search_migration", VERSIONS / "c2f4b6d8e0a1_create_search_schedules.py"
        )
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        setattr(module, "op", Operations(MigrationContext.configure(connection)))
        module.downgrade()

    try:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
            for table in _REBUILT_TABLES:
                await connection.exec_driver_sql(f'DROP TABLE "{table}"')
            await connection.run_sync(_upgrade)
            await connection.run_sync(downgrade)
            tables = {
                row[0]
                for row in (
                    await connection.exec_driver_sql(
                        "SELECT tablename FROM pg_tables WHERE schemaname = "
                        f"'{schema}'"
                    )
                ).all()
            }
            assert not set(TABLES) & tables
            assert {"research_import_receipts", "archive_deposit_attempts"} <= tables
            check = await connection.exec_driver_sql(
                "SELECT pg_get_constraintdef(c.oid) FROM pg_constraint c "
                "JOIN pg_namespace n ON n.oid = c.connamespace "
                f"WHERE n.nspname = '{schema}' "
                "AND c.conname = 'ck_research_import_receipt_kind'"
            )
            definition = check.scalar_one()
            assert "citation_chase" in definition
            assert "scheduled_search" not in definition
    finally:
        await engine.dispose()
        async with admin.begin() as connection:
            await connection.exec_driver_sql(f'DROP SCHEMA "{schema}" CASCADE')
        await admin.dispose()


async def test_schedule_claims_once_classifies_and_survives_restart(
    screening_factory: Factory,
) -> None:
    factory = screening_factory
    w = await _seed_world(factory)
    crossref, pubmed = FakeProvider("crossref"), FakeProvider("pubmed")
    w.connectors = {"crossref": crossref, "pubmed": pubmed}

    # 1. Seed the baseline corpus.
    reports = await _seed_corpus(w)
    assert reports["e1"] != reports["e2"]  # one title, two DOIs: two reports

    # 2. Create: refusals first, then the pinned schedule with its baseline.
    await _refused(
        _create(w, "O", w.tampered_version, "tampered", "search-2"),
        422,
        svc.STRATEGY_MISMATCH,
    )
    await _refused(
        _create(w, "O", w.superseded["strategy_version"], "old", "search-3"),
        409,
        "superseded protocol",
    )
    await _refused(_create(w, "R", w.good["strategy_version"], "rev"), 403)
    await _refused(_create(w, "F", w.good["strategy_version"], "foreign"), 404)
    bad_cron = SearchScheduleCreate(
        source_run_id=w.ids["run"],
        step_id="search",
        strategy_version=w.good["strategy_version"],
        cron="*/15 * * * *",
        timezone=TZ,
        idempotency_key="sub-hourly",
    )
    await _refused(
        _as(
            w,
            "O",
            SUPERVISE,
            lambda db, c: svc.create_schedule(db, c, w.ids["O"], bad_cron),
        ),
        422,
        "at most hourly",
    )
    schedule, replayed = await _create(w, "O", w.good["strategy_version"], "create")
    assert not replayed and schedule.status == "scheduled"
    again, replayed = await _create(w, "O", w.good["strategy_version"], "create")
    assert replayed and again.schedule_id == schedule.schedule_id
    sid = schedule.schedule_id
    root = await _scalar(
        factory,
        "SELECT baseline_snapshot FROM research_search_schedules WHERE id = :s",
        s=sid,
    )
    assert set(root["reports"]) == set(reports.values())
    assert root["digest"] == schedule.tip.baseline_digest
    assert schedule.next_fire_local is not None
    first = datetime.strptime(schedule.next_fire_local, rules.LOCAL_FORMAT)
    zone = rules.parse_schedule(CRON, TZ)[1]

    def fire(weeks: int) -> datetime:
        return rules.to_utc(first + timedelta(weeks=weeks), zone) + timedelta(minutes=1)

    # 3. Concurrent claim: one execution for one fire, whatever races.
    async def claim_in_own_session() -> list[UUID]:
        return await _claim(w, fire(0))

    claims = await asyncio.gather(claim_in_own_session(), claim_in_own_session())
    assert sum(len(c) for c in claims) == 1
    (exec1,) = [e for c in claims for e in c]
    async with factory() as db:
        tip = (
            await db.execute(svc._tips().where(svc.S.schedule_id == sid))
        ).scalar_one()
        local_fire = (first, rules.to_utc(first, zone), 0)
        assert await svc.insert_fire(db, tip, local_fire) is None  # lost-lock race
        await db.commit()
    assert (
        await _scalar(factory, "SELECT count(*) FROM research_search_executions") == 1
    )

    # 4. Restart: the worker dies after its started attempt.
    crossref.crash = True
    with pytest.raises(Crash):
        await _run(w, exec1)
    assert await _attempts(w, exec1) == [("started", None)]
    assert await _claim(w, fire(0) + timedelta(minutes=1)) == []
    real = datetime.now(timezone.utc)
    assert await _run(w, exec1, real + timedelta(minutes=5)) == "running"
    assert await _attempts(w, exec1) == [("started", None)]

    # 6 (set up). Today's results: A unchanged + a retraction notice, B with
    # a new title, new F titled like E1 but another DOI, E1, no D; PubMed
    # capped without C.
    crossref.crash = False
    crossref.docs = [
        _crossref("a", "Alpha"),
        _crossref("b", "Beta (revised)"),
        _crossref("e1", "Echo"),
        _crossref("f", "Echo"),
    ]
    pubmed.docs = [_pubmed("900", "Unrelated one"), _pubmed("901", "Unrelated two")]
    pubmed.has_more = True
    crossref.notices = {DOI["a"]: [NOTICE]}

    def later() -> datetime:
        return datetime.now(timezone.utc) + timedelta(minutes=31)

    # The second attempt dies after its import committed: the third reuses
    # that receipt (dedup_key per execution) and never searches again.
    crossref.notice_crash = True
    with pytest.raises(Crash):
        await _run(w, exec1, later())
    searches = crossref.searches
    scheduled = (
        "SELECT count(*) FROM research_import_receipts WHERE kind = 'scheduled_search'"
    )
    assert await _scalar(factory, scheduled) == 1
    crossref.notice_crash = False
    assert await _run(w, exec1, later()) == "succeeded"
    assert crossref.searches == searches
    assert await _scalar(factory, scheduled) == 1
    attempts = await _attempts(w, exec1)
    assert [a[0] for a in attempts] == ["started", "started", "started", "succeeded"]

    # 6. Classified delta against the explicit baseline.
    delta = await _delta(w, exec1)
    by_key = {k: delta[r] for k, r in reports.items()}
    assert by_key["a"]["class"] == "corrected_retracted"
    assert by_key["a"]["evidence"]["notices"][0]["notice_doi"] == NOTICE["notice_doi"]
    assert by_key["b"]["class"] == "changed"
    assert by_key["b"]["evidence"]["fields"]["title"] == {
        "before": "Beta",
        "after": "Beta (revised)",
    }
    assert by_key["d"]["class"] == "unknown"
    assert by_key["d"]["reason"] == "not_returned"
    assert by_key["c"]["class"] == "unknown"
    assert by_key["c"]["reason"] == "provider_capped"
    assert by_key["e1"]["class"] == "unchanged"
    assert by_key["e2"]["reason"] == "not_returned"
    new = [r for r, item in delta.items() if item["class"] == "new"]
    assert len(new) == 3  # F and the two PubMed results
    f_report = await _scalar(
        factory,
        "SELECT report_id FROM research_report_identifiers WHERE value = :v",
        v=DOI["f"],
    )
    assert str(f_report) in new and str(f_report) not in reports.values()
    assert "deleted" not in json.dumps(delta)
    async with factory() as db:
        accepted = await svc.accepted_delta(db, w.p1, exec1)
    assert accepted.baseline_execution_id is None
    assert accepted.baseline_digest == schedule.tip.baseline_digest
    assert accepted.citation_chasing == {"required": False, "status": "not_required"}

    # 5. Missed ticks: three Mondays pass with no tick; one fire, missed 2.
    # 9 (set up). A workspace-level withdrawal marker on B (local metadata,
    # not a publisher notice) arrives before the next run.
    async with factory() as db:
        await resolve_project(db, w.p1, w.ids["O"], ResearchAction.EDIT)
        await corpus_service.insert_receipt(
            db,
            collection_id=w.p1,
            actor_user_id=w.ids["O"],
            kind="file_import",
            dedup_key="file:withdrawal",
            lineage_key="c" * 64,
            declared={"database": "workspace"},
            observed={},
            records=[
                ResearchImportRecord(
                    id=uuid4(),
                    collection_id=w.p1,
                    record_index=0,
                    status="accepted",
                    raw="{}",
                    parsed={
                        "title": "Beta (revised)",
                        "identifiers": {"doi": DOI["b"]},
                        "is_retracted": True,
                        "retraction_status": "retracted",
                    },
                )
            ],
        )
        await db.commit()
    (exec2,) = await _claim(w, fire(3))
    missed = await _scalar(
        factory,
        "SELECT missed_fires FROM research_search_executions WHERE id = :e",
        e=exec2,
    )
    assert missed == 2

    # 7. Outage: the Crossref notice check fails for every DOI.
    crossref.notice_fail = True
    assert await _run(w, exec2) == "succeeded"
    delta2 = await _delta(w, exec2)
    assert not [i for i in delta2.values() if i["class"] == "unchanged"]
    assert delta2[reports["a"]]["reason"] == "provider_failed"
    assert delta2[reports["a"]]["publication"]["check"] == "failed"
    # 9. The withdrawal marker never becomes a publication retraction.
    assert delta2[reports["b"]]["class"] != "corrected_retracted"
    async with factory() as db:
        second = await svc.accepted_delta(db, w.p1, exec2)
    assert second.baseline_execution_id == exec1
    crossref.notice_fail = False

    # 8. Idempotent import: rerunning a succeeded execution changes nothing.
    receipts = await _scalar(
        factory,
        "SELECT count(*) FROM research_import_receipts WHERE kind = 'scheduled_search'",
    )
    searches = crossref.searches
    assert await _run(w, exec1) == "succeeded"
    assert crossref.searches == searches
    assert (
        receipts == 2
        and await _scalar(
            factory,
            "SELECT count(*) FROM research_import_receipts WHERE kind = 'scheduled_search'",
        )
        == 2
    )
    assert (
        await _scalar(factory, "SELECT count(*) FROM research_search_execution_results")
        == 2
    )
    assert (
        await _scalar(
            factory,
            "SELECT count(*) FROM research_decision_events "
            "WHERE event_type = 'search_update.executed' "
            "AND payload->>'execution_id' = :e",
            e=str(exec1),
        )
        == 1
    )

    # Versions: disable (a stale tip is 409), nothing is claimed, re-enable.
    disable = SearchScheduleVersionCreate(
        expected_tip_id=sid, enabled=False, idempotency_key="off"
    )
    off, _ = await _as(
        w,
        "O",
        SUPERVISE,
        lambda db, c: svc.version_schedule(db, c, w.ids["O"], sid, disable),
    )
    assert off.status == "disabled" and len(off.versions) == 2
    stale = SearchScheduleVersionCreate(
        expected_tip_id=sid, enabled=True, idempotency_key="stale"
    )
    await _refused(
        _as(
            w,
            "O",
            SUPERVISE,
            lambda db, c: svc.version_schedule(db, c, w.ids["O"], sid, stale),
        ),
        409,
        svc.STALE_TIP,
    )
    assert await _claim(w, fire(4)) == []
    enable = SearchScheduleVersionCreate(
        expected_tip_id=off.tip.id, enabled=True, idempotency_key="on"
    )
    await _as(
        w,
        "O",
        SUPERVISE,
        lambda db, c: svc.version_schedule(db, c, w.ids["O"], sid, enable),
    )

    # 10. Revocation at execution: archived, role removed, protocol amended.
    receipts_before = await _scalar(
        factory, "SELECT count(*) FROM research_import_receipts"
    )
    await _sql(
        factory,
        "UPDATE workspaces SET is_archived = true WHERE id = :w",
        w=w.ids["workspace"],
    )
    (archived,) = await _claim(w, fire(5))
    assert await _run(w, archived) == "failed"
    assert (await _attempts(w, archived))[-1] == ("failed", svc.PROJECT_ARCHIVED)
    await _sql(
        factory,
        "UPDATE workspaces SET is_archived = false WHERE id = :w",
        w=w.ids["workspace"],
    )
    await _sql(
        factory,
        "UPDATE research_project_role_assignments SET is_deleted = true "
        "WHERE user_id = :u AND role = 'supervisor'",
        u=w.ids["O"],
    )
    (revoked,) = await _claim(w, fire(6))
    assert await _run(w, revoked) == "failed"
    assert (await _attempts(w, revoked))[-1] == ("failed", svc.OWNER_ROLE_REVOKED)
    await _sql(
        factory,
        "UPDATE research_project_role_assignments SET is_deleted = false "
        "WHERE user_id = :u AND role = 'supervisor'",
        u=w.ids["O"],
    )
    amended = uuid4()
    await _sql(
        factory,
        """INSERT INTO research_protocol_versions (
               id, protocol_id, version, parent_version_id, question_version_id,
               blueprint_id, execution_plan, snapshot, content_hash, status,
               change_kind, amendment_reason, author_user_id, approved_by_user_id,
               approved_at, superseded_at, created_at)
           SELECT :new, protocol_id, 2, id, question_version_id, blueprint_id,
               execution_plan, snapshot, repeat('f', 64), 'approved', 'amendment',
               'Fixture amendment', author_user_id, approved_by_user_id, now(),
               NULL, now()
           FROM research_protocol_versions WHERE id = :old""",
        new=amended,
        old=w.protocol,
    )
    await _sql(
        factory,
        "UPDATE research_protocols SET current_approved_version_id = :new "
        "WHERE current_approved_version_id = :old",
        new=amended,
        old=w.protocol,
    )
    (changed,) = await _claim(w, fire(7))
    searches = crossref.searches
    assert await _run(w, changed) == "failed"
    assert (await _attempts(w, changed))[-1] == ("failed", svc.PROTOCOL_CHANGED)
    assert crossref.searches == searches  # the old plan never ran
    assert (
        await _scalar(factory, "SELECT count(*) FROM research_import_receipts")
        == receipts_before
    )
    listing = await _as(w, "R", VIEW, lambda db, c: svc.list_schedules(db, c))
    (shown,) = listing.schedules
    assert shown.status == "blocked"
    assert shown.last_execution is not None and shown.last_execution.status == "failed"
    executions = await _as(w, "R", VIEW, lambda db, c: svc.list_executions(db, c, sid))
    assert len(executions.executions) == 5
    assert executions.executions[0].attempts[1].detail == {
        "retry_of": str(executions.executions[0].attempts[0].id)
    }

    # 11. The delta export reconstructs every count and evidence item.
    package = await _as(w, "R", VIEW, lambda db, c: svc.export_delta(db, c, exec1))
    assert package["schema"] == svc.DELTA_SCHEMA
    assert (
        package["body_sha256"]
        == hashlib.sha256(_canonical(package["body"])).hexdigest()
    )
    body = package["body"]
    rebuilt = {name: 0 for name in rules.DELTA_CLASSES}
    for item in body["items"]:
        rebuilt[item["class"]] += 1
        assert "evidence" in item and "publication" in item
    assert rebuilt == body["counts"] == accepted.counts
    assert body["baseline_digest"] == schedule.tip.baseline_digest
    filtered = await _as(
        w, "R", VIEW, lambda db, c: svc.export_delta(db, c, exec1, "unknown")
    )
    assert {i["class"] for i in filtered["body"]["items"]} == {"unknown"}
    await _refused(
        _as(w, "R", VIEW, lambda db, c: svc.export_delta(db, c, archived)), 404
    )

    # 12. Insert-only tables, ledger replay, guarded downgrade.
    for table in TABLES:
        for statement in (
            f"UPDATE {table} SET created_at = now()",
            f"DELETE FROM {table}",
        ):
            async with factory() as db:
                with pytest.raises(DBAPIError) as blocked:
                    await db.execute(text(statement))
                assert getattr(blocked.value.orig, "sqlstate", None) == "55000"
    async with factory() as db:
        events = await replay_decisions(
            db, collection_id=w.p1, aggregate_type=svc.AGGREGATE_TYPE, aggregate_id=w.p1
        )
    kinds = [str(e.event_type) for e in events]
    assert kinds.count("search_update.schedule_versioned") == 3
    assert kinds.count("search_update.executed") == 2
    async with factory() as db:
        connection = await db.connection()

        def refuse(sync: Connection) -> None:
            spec = importlib.util.spec_from_file_location(
                "search_migration_refuse",
                VERSIONS / "c2f4b6d8e0a1_create_search_schedules.py",
            )
            assert spec is not None and spec.loader is not None
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            setattr(module, "op", Operations(MigrationContext.configure(sync)))
            with pytest.raises(RuntimeError, match="refusing"):
                module.downgrade()

        await connection.run_sync(refuse)
        await db.rollback()
    if os.getenv("RESEARCH_DECISION_DATABASE_URL"):
        await _downgrade_on_fresh_schema()
    assert cast(Any, schedule).schedule_id == sid
