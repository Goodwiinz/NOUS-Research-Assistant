"""Real PostgreSQL proof for GOO-320 superseding review versions.

Runs on GOO-301's ``screening_factory`` schema, whose migration chain now
ends at ``d4a6c8e0f2b3``: both insert-only tables, their unique keys, the
partial root index, the CHECKs and the triggers come from the migration.
Deltas are real GOO-319 executions over fake providers the test controls
(``FakeProvider``); queues, observations, adjudication, acquisition, study
links, claims and assessments go through their own services. Live providers
and the deployed journey are NOT RUN here.

Seeds: GOO-299's owner O (made supervisor), reviewers R and B (dual
independent review), adjudicator A (J in the plan), role-less viewer V,
foreign F; reports R1-R6 by file import, where R2 and R3 are reports of one
confirmed study S; R6 is never screened.

Mutation verification (docs/engineering/testing.md), full record in
``docs/testing/agent-orchestration-mutation-checks.md`` (GOO-320 section):
carrying ``changed`` too (step 4: R4 carried), skipping the criteria-hash
check (step 8: decisions carried across changed criteria), carrying
``unknown`` (step 4: R3 carried), ``graph_part`` edges for every report
(step 7: R1's claims stale), ``UNIQUE(parent_review_version_id)`` dropped
from the migration (step 5: two successors), the superseded-release check
skipped (step 9: wrong supersession accepted).

Command (from ``backend/``): ``RESEARCH_DECISION_DATABASE_URL=... pytest -q
tests/integration/test_review_versions_postgres.py``.
"""

import asyncio
import hashlib
import importlib.util
import json
import os
from dataclasses import replace
from datetime import date, datetime, timedelta
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
from src.models.collection import CollectionDocument
from src.models.document import Document, DocumentType
from src.models.draft_release import DraftRelease
from src.models.generated_draft import GeneratedDraft
from src.models.manuscript_release import ManuscriptRelease
from src.models.research_import import ResearchImportRecord
from src.models.research_project_role import (
    ResearchProjectRole,
    ResearchProjectRoleAssignment,
)
from src.models.research_step import ResearchStep
from src.models.user import User
from src.models.workspace import WorkspaceMember, WorkspaceRole
from src.schemas.research_engine import (
    FulltextAttemptCreate,
    FulltextRequestCreate,
    ReviewReleaseLinkCreate,
    ReviewVersionCreate,
    ScreeningAdjudicateRequest,
    ScreeningAssignmentCreate,
    ScreeningObservationCreate,
    ScreeningQueueCreate,
    SearchScheduleCreate,
    SearchScheduleVersionCreate,
    StudyLinkRequest,
)
from src.services.research import claims_service as claims
from src.services.research import draft_release_service
from src.services.research import release_rules as rr
from src.services.research_decisions import replay_decisions
from src.services.research_engine import (
    acquisition_service,
    corpus_service,
    identity_service,
    prisma,
    prisma_service,
)
from src.services.research_engine import review_update_service as svc
from src.services.research_engine import screening_service
from src.services.research_engine import search_update_rules as search_rules
from src.services.research_engine import search_update_service as updates
from src.services.research_engine.corpus_export import _canonical
from src.services.research_engine.project_access import (
    ProjectContext,
    ResearchAction,
    resolve_project,
)
from src.shared.claim_schemas import ClaimAssessmentCreate, ClaimCreate, ClaimLinkCreate
from tests.integration.research_engine_postgres_support import (
    seed_approved_protocol_binding,
)
from tests.integration.test_report_identity_postgres import _seed
from tests.integration.test_screening_queue_postgres import (  # noqa: F401
    _REBUILT_TABLES,
    SNAPSHOT,
    VERSIONS,
    _upgrade,
    screening_factory,
)
from tests.integration.test_search_updates_postgres import FakeProvider, _strategy

pytestmark = [pytest.mark.integration, pytest.mark.requires_postgres]

Factory: TypeAlias = async_sessionmaker[AsyncSession]
T = TypeVar("T")
VIEW, EDIT, REVIEW, ADJUDICATE, SUPERVISE = (
    ResearchAction.VIEW,
    ResearchAction.EDIT,
    ResearchAction.REVIEW,
    ResearchAction.ADJUDICATE,
    ResearchAction.SUPERVISE,
)
CRON, TZ = "0 6 * * 1", "Europe/London"
TA, FT = "title_abstract", "full_text"
TITLES = {
    "r1": "One",
    "r2": "Two",
    "r3": "Three",
    "r4": "Four",
    "r5": "Five",
    "r6": "Six",
    "n1": "Nova",
    "n2": "Two",  # another report of study S, by another DOI
}
DOI = {k: f"10.7777/{k}" for k in TITLES}
NOTICE = {
    "notice_doi": "10.7777/r5.retraction",
    "type": "retraction",
    "date": "2026-09-01T00:00:00Z",
    "asserted_by": "publisher",
}
TABLES = ("research_review_versions", "research_review_release_links")
MIGRATION = "d4a6c8e0f2b3_create_review_versions.py"


def _sha(value: str | bytes) -> str:
    data = value.encode() if isinstance(value, str) else value
    return hashlib.sha256(data).hexdigest()


async def _as(
    w: Any,
    user: str,
    action: ResearchAction,
    call: Callable[[AsyncSession, ProjectContext], Awaitable[T]],
) -> T:
    """A route-shaped request whose service commits."""
    async with w.factory() as db:
        context = await resolve_project(db, w.p1, w.ids[user], action)
        return await call(db, context)


async def _act(
    w: Any,
    user: str,
    action: ResearchAction,
    call: Callable[[AsyncSession, ProjectContext], Awaitable[T]],
) -> T:
    """A route-shaped request whose route commits (screening, identity)."""
    async with w.factory() as db:
        context = await resolve_project(db, w.p1, w.ids[user], action)
        result = await call(db, context)
        await db.commit()
        return result


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


# --- world ---------------------------------------------------------------------


def _crossref_strategy(protocol_version_id: str, step_id: str) -> dict[str, Any]:
    strategy = _strategy(protocol_version_id, step_id)
    strategy["intended"]["selected_providers"] = ["crossref"]
    strategy.pop("strategy_version")
    strategy["strategy_version"] = search_rules.strategy_hash(strategy)
    return strategy


async def _journal(w: Any, strategies: list[dict[str, Any]]) -> None:
    """The completed run's GOO-298 journal and one search step per strategy."""
    async with w.factory() as db:
        await db.execute(
            text("""UPDATE research_runs SET status = 'completed',
                    protocol_version_id = :v,
                    reproducibility_manifest = CAST(:m AS jsonb)
                    WHERE id = :r"""),
            {
                "v": w.protocol,
                "m": json.dumps(
                    {
                        "_search_receipts_v1": {
                            "schema_version": 1,
                            "strategies": {
                                s["strategy_version"]: s for s in strategies
                            },
                            "executions": {},
                        }
                    }
                ),
                "r": w.ids["run"],
            },
        )
        strategy = strategies[-1]
        db.add(
            ResearchStep(
                run_id=w.ids["run"],
                step_index=len(strategies) - 1,
                step_type="search",
                output={
                    "query": "membrane imaging",
                    "coverage": {"strategy_version": strategy["strategy_version"]},
                },
            )
        )
        await db.commit()


async def _world(factory: Factory) -> Any:
    ids = await _seed(factory)
    ids["B"] = uuid4()
    async with factory() as db:
        db.add(
            User(
                id=ids["B"],
                email=f"{ids['B']}@test.invalid",
                password_hash="unused",
                first_name="Test",
                last_name="B",
                organization_id=ids["org"],
            )
        )
        await db.flush()
        db.add(
            WorkspaceMember(
                workspace_id=ids["workspace"],
                user_id=ids["B"],
                role=WorkspaceRole.VIEWER,
            )
        )
        for user, role in (
            ("O", ResearchProjectRole.SUPERVISOR),
            ("B", ResearchProjectRole.REVIEWER),
        ):
            db.add(
                ResearchProjectRoleAssignment(
                    collection_id=ids["collection"],
                    user_id=ids[user],
                    role=role,
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
            snapshot=SNAPSHOT,
        )
        await db.commit()
    w = SimpleNamespace(
        factory=factory,
        ids=ids,
        p1=ids["collection"],
        protocol=binding.protocol_version_id,
        crossref=FakeProvider("crossref"),
    )
    w.connectors = {"crossref": w.crossref}
    w.strategy = _crossref_strategy(str(w.protocol), "search")
    await _journal(w, [w.strategy])
    return w


async def _corpus(w: Any) -> dict[str, UUID]:
    """R1-R6 through one GOO-300 file-import receipt."""
    keys = ("r1", "r2", "r3", "r4", "r5", "r6")
    async with w.factory() as db:
        await resolve_project(db, w.p1, w.ids["O"], EDIT)
        records = [
            ResearchImportRecord(
                id=uuid4(),
                collection_id=w.p1,
                record_index=index,
                status="accepted",
                raw=key,
                parsed={
                    "title": TITLES[key],
                    "identifiers": {"doi": DOI[key]},
                    "authors": ["Ada Lovelace"],
                    "year": "2021",
                    "venue": "J",
                },
            )
            for index, key in enumerate(keys)
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
        reports = {k: cast(UUID, r.report_id) for k, r in zip(keys, records)}
        await db.commit()
    return reports


def _doc(key: str, title: str | None = None) -> Any:
    from src.services.research_engine.connectors.base import SourceDocument

    return SourceDocument(
        connector_type="crossref",
        external_id=DOI[key],
        title=title or TITLES[key],
        authors=["Ada Lovelace"],
        metadata={
            "doi": DOI[key],
            "journal": "J",
            "published": [[2021]],
            "provider_updated": "2026-09-01T00:00:00Z",
        },
    )


async def _retrieve(w: Any, report: UUID, body: str) -> UUID:
    """GOO-303: a hashed document recorded as the report's full text."""
    async with w.factory() as db:
        document = Document(
            title=f"full text {report}",
            filename="full.pdf",
            file_path="local:///full.pdf",
            file_size_bytes=1,
            mime_type="application/pdf",
            document_type=DocumentType.PDF,
            organization_id=w.ids["org"],
            checksum_sha256=_sha(body),
            content_text=body,
        )
        db.add(document)
        await db.flush()
        db.add(CollectionDocument(collection_id=w.p1, document_id=document.id))
        await db.commit()
        document_id = cast(UUID, document.id)
    state, _ = await _act(
        w,
        "O",
        EDIT,
        lambda db, ctx: acquisition_service.request_fulltext(
            db,
            ctx,
            w.ids["O"],
            FulltextRequestCreate(report_id=report, idempotency_key=f"rq-{report}"),
        ),
    )
    await _act(
        w,
        "O",
        EDIT,
        lambda db, ctx: acquisition_service.record_attempt(
            db,
            ctx,
            state.request_id,
            w.ids["O"],
            FulltextAttemptCreate(
                outcome="retrieved",
                attempted_on=date.today(),
                document_id=document_id,
                idempotency_key=f"rt-{report}",
            ),
        ),
    )
    return document_id


async def _queue(w: Any, key: str, stage: str, reports: list[UUID]) -> Any:
    body = ScreeningQueueCreate(
        protocol_version_id=w.protocol,
        stage=cast(Any, stage),
        report_ids=reports,
        idempotency_key=key,
    )
    queue = await _act(
        w,
        "O",
        SUPERVISE,
        lambda db, ctx: screening_service.create_queue(db, ctx, w.ids["O"], body),
    )
    for user in ("R", "B"):
        await _act(
            w,
            "O",
            SUPERVISE,
            lambda db, ctx: screening_service.assign(
                db,
                ctx,
                queue.id,
                w.ids["O"],
                ScreeningAssignmentCreate(
                    reviewer_user_id=w.ids[user], idempotency_key=f"{key}-{user}"
                ),
            ),
        )
    return queue


async def _screen(
    w: Any, queue_id: UUID, decisions: dict[UUID, tuple[str, str]]
) -> None:
    """Both reviewers screen: ``report -> (R's decision, B's decision)``."""
    async with w.factory() as db:
        queue = (
            await db.execute(
                text("SELECT stage, criteria_hash FROM screening_queues WHERE id = :q"),
                {"q": queue_id},
            )
        ).one()
        assignments = dict(
            (
                await db.execute(
                    text("""SELECT reviewer_id, id FROM screening_assignments
                            WHERE queue_id = :q AND revoked_at IS NULL"""),
                    {"q": queue_id},
                )
            )
            .tuples()
            .all()
        )
    for report, votes in decisions.items():
        for user, decision in zip(("R", "B"), votes):
            reason = (
                "wrong population" if queue[0] == FT and decision == "exclude" else None
            )
            body = ScreeningObservationCreate(
                report_id=report,
                assignment_id=assignments[w.ids[user]],
                criteria_hash=queue[1],
                decision=cast(Any, decision),
                exclusion_reason=reason,
                idempotency_key=f"obs-{queue_id}-{report}-{user}",
            )
            await _act(
                w,
                user,
                REVIEW,
                lambda db, ctx: screening_service.submit(
                    db, ctx, queue_id, w.ids[user], body
                ),
            )


async def _adjudicate(w: Any, queue_id: UUID, report: UUID, decision: str) -> None:
    async with w.factory() as db:
        tip = (
            await db.execute(
                text("""SELECT r.id, r.input_observation_ids, r.criteria_hash
                        FROM screening_resolutions r
                        WHERE r.queue_id = :q AND r.report_id = :r
                          AND NOT EXISTS (SELECT 1 FROM screening_resolutions n
                                          WHERE n.supersedes_resolution_id = r.id)"""),
                {"q": queue_id, "r": report},
            )
        ).one()
    body = ScreeningAdjudicateRequest(
        resolution_id=tip[0],
        input_observation_ids=[UUID(str(o)) for o in tip[1]],
        criteria_hash=tip[2],
        decision=cast(Any, decision),
        rationale="adjudicated on the abstract",
        idempotency_key=f"adj-{queue_id}-{report}",
    )
    await _act(
        w,
        "A",
        ADJUDICATE,
        lambda db, ctx: screening_service.adjudicate(
            db, ctx, queue_id, report, w.ids["A"], body
        ),
    )


async def _study(w: Any, report: UUID, study: UUID | None, key: str) -> UUID:
    response = await _act(
        w,
        "A",
        ADJUDICATE,
        lambda db, ctx: identity_service.link_study(
            db,
            ctx,
            report,
            w.ids["A"],
            StudyLinkRequest(
                study_id=study,
                status="confirmed",
                rationale="same trial",
                idempotency_key=key,
            ),
        ),
    )
    return cast(UUID, response.study_id)


async def _claim_on(
    w: Any, draft: Any, sentence: str, document: UUID, body: str
) -> Any:
    """A claim over ``sentence`` linked to the whole document body and
    assessed by A: ``(link_id, assessment_id)``."""
    start = draft.content.index(sentence)
    claim, _ = await _as(
        w,
        "O",
        EDIT,
        lambda db, ctx: claims.create_claim(
            db,
            ctx,
            w.ids["O"],
            ClaimCreate(
                draft_id=draft.id,
                start_char=start,
                end_char=start + len(sentence),
                text=sentence,
                idempotency_key=f"claim-{sentence}",
            ),
        ),
    )
    link, _ = await _as(
        w,
        "O",
        EDIT,
        lambda db, ctx: claims.link(
            db,
            ctx,
            claim.id,
            w.ids["O"],
            ClaimLinkCreate(
                claim_version_id=claim.version.id,
                kind="source_span",
                document_id=document,
                start_char=0,
                end_char=len(body),
                quote=body,
                idempotency_key=f"link-{sentence}",
            ),
        ),
    )
    assessment, _ = await _as(
        w,
        "A",
        ADJUDICATE,
        lambda db, ctx: claims.assess(
            db,
            ctx,
            claim.id,
            w.ids["A"],
            ClaimAssessmentCreate(
                claim_version_id=claim.version.id,
                stance="supporting",
                link_ids=[link.id],
                rationale="adjudicated",
                idempotency_key=f"assess-{sentence}",
            ),
        ),
    )
    return link.id, assessment.id


async def _release(w: Any, tag: str) -> UUID:
    """One verified GOO-315 release (candidate + verified rows)."""
    async with w.factory() as db:
        draft = GeneratedDraft(
            project_id=w.p1, version=1, title=f"Release {tag}", content=tag
        )
        db.add(draft)
        await db.flush()
        draft_release = DraftRelease(
            collection_id=w.p1,
            draft_id=draft.id,
            draft_version=1,
            content_hash=_sha(tag),
            claim_version_ids=[],
            assessment_ids=[],
            interpretation_claim_version_ids=[],
            policy_version=1,
            promoted_by_id=w.ids["A"],
            actor_role="adjudicator",
        )
        db.add(draft_release)
        await db.flush()
        rows: list[ManuscriptRelease] = []
        for stage in ("candidate", "verified"):
            row = ManuscriptRelease(
                id=uuid4(),
                collection_id=w.p1,
                draft_id=draft.id,
                draft_version=1,
                content_hash=_sha(tag),
                stage=stage,
                candidate_release_id=None if not rows else rows[0].id,
                draft_release_id=None if not rows else draft_release.id,
                snapshot={"tag": tag},
                snapshot_hash=_sha(f"snapshot-{tag}"),
                checks={},
                checks_hash=_sha(f"checks-{tag}"),
                package_files=[],
                package_sha256=_sha(f"package-{tag}"),
                package_storage_key=f"fixture/{tag}",
                created_by_id=w.ids["O" if stage == "candidate" else "A"],
                actor_role="editor" if stage == "candidate" else "adjudicator",
            )
            db.add(row)
            await db.flush()
            rows.append(row)
        await db.commit()
    return cast(UUID, rows[1].id)


def _create(w: Any, user: str, key: str, **fields: Any) -> Awaitable[Any]:
    data = ReviewVersionCreate(rationale="review update", idempotency_key=key, **fields)
    return _as(
        w,
        user,
        SUPERVISE,
        lambda db, ctx: svc.create_version(db, ctx, w.ids[user], data),
    )


def _successor(
    w: Any, key: str, parent: UUID, delta: Any, **fields: Any
) -> Awaitable[Any]:
    return _create(
        w,
        "O",
        key,
        parent_review_version_id=parent,
        execution_id=delta.execution_id,
        delta_hash=delta.delta_hash,
        reviewer_user_ids=[w.ids["R"], w.ids["B"]],
        **fields,
    )


async def _versions(w: Any, user: str = "O") -> dict[str, Any]:
    listed = await _as(w, user, VIEW, lambda db, ctx: svc.list_versions(db, ctx))
    return {str(v.id): v for v in listed.versions}


async def _accounting(w: Any, version: UUID) -> Any:
    return await _as(w, "O", VIEW, lambda db, ctx: svc.accounting(db, ctx, version))


async def _delta(w: Any, execution: UUID) -> Any:
    async with w.factory() as db:
        return await updates.accepted_delta(db, w.p1, execution)


async def _work(w: Any, version: UUID) -> Any:
    return await _as(
        w, "O", SUPERVISE, lambda db, ctx: svc.ensure_work(db, ctx, w.ids["O"], version)
    )


def _status(version: Any, stage: str) -> Any:
    return next(s for s in version.work if s.stage == stage)


async def _xmins(factory: Factory) -> dict[str, str]:
    async with factory() as db:
        rows = await db.execute(text("""
            SELECT 'assessment:' || id, xmin::text FROM research_claim_assessments
            UNION ALL SELECT 'link:' || id, xmin::text
                FROM research_claim_evidence_links
            UNION ALL SELECT 'resolution:' || id, xmin::text FROM screening_resolutions
            UNION ALL SELECT 'release:' || id, xmin::text FROM manuscript_releases"""))
        return dict(rows.tuples().all())


def _migration(sync: Connection, direction: str) -> None:
    spec = importlib.util.spec_from_file_location(MIGRATION[:-3], VERSIONS / MIGRATION)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    setattr(module, "op", Operations(MigrationContext.configure(sync)))
    getattr(module, direction)()


async def _downgrade_on_fresh_schema() -> None:
    """Step 11 on an empty schema: only the two tables drop."""
    url = make_url(os.environ["RESEARCH_DECISION_DATABASE_URL"]).set(
        drivername="postgresql+asyncpg"
    )
    schema = f"test_review_down_{uuid4().hex}"
    admin = create_async_engine(url)
    async with admin.begin() as connection:
        await connection.exec_driver_sql(f'CREATE SCHEMA "{schema}"')
    engine = create_async_engine(
        url, connect_args={"server_settings": {"search_path": schema}}
    )
    try:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
            for table in _REBUILT_TABLES:
                await connection.exec_driver_sql(f'DROP TABLE "{table}"')
            await connection.run_sync(_upgrade)
            before = {
                row[0]
                for row in (
                    await connection.exec_driver_sql(
                        f"SELECT tablename FROM pg_tables WHERE schemaname = '{schema}'"
                    )
                ).all()
            }
            await connection.run_sync(lambda sync: _migration(sync, "downgrade"))
            after = {
                row[0]
                for row in (
                    await connection.exec_driver_sql(
                        f"SELECT tablename FROM pg_tables WHERE schemaname = '{schema}'"
                    )
                ).all()
            }
            assert before - after == set(TABLES)
    finally:
        await engine.dispose()
        async with admin.begin() as connection:
            await connection.exec_driver_sql(f'DROP SCHEMA "{schema}" CASCADE')
        await admin.dispose()


async def test_superseding_review_reconciles_carries_forward_and_targets_staleness(
    screening_factory: Factory,
) -> None:
    factory = screening_factory
    w = await _world(factory)

    # 1. Seed the parent: dual title/abstract and full-text review of R1-R5
    # (R4 adjudicated by A), R2 and R3 one confirmed study S, R6 never
    # screened; claims on R1's, R4's and R5's documents; verified release P.
    r = await _corpus(w)
    ta = await _queue(w, "ta", TA, [r[k] for k in ("r1", "r2", "r3", "r4", "r5", "r6")])
    await _screen(
        w,
        ta.id,
        {
            r["r1"]: ("include", "include"),
            r["r2"]: ("include", "include"),
            r["r3"]: ("include", "include"),
            r["r4"]: ("include", "exclude"),
            r["r5"]: ("include", "include"),
        },
    )
    await _adjudicate(w, ta.id, r["r4"], "include")
    texts = {k: f"Report {k} full text. Finding {k} holds." for k in r}
    docs = {
        k: await _retrieve(w, r[k], texts[k]) for k in ("r1", "r2", "r3", "r4", "r5")
    }
    ft = await _queue(w, "ft", FT, [r[k] for k in ("r1", "r2", "r3", "r4", "r5")])
    await _screen(
        w, ft.id, {r[k]: ("include", "include") for k in ("r1", "r2", "r3", "r4", "r5")}
    )
    study = await _study(w, r["r2"], None, "s-r2")
    await _study(w, r["r3"], study, "s-r3")
    draft = await _as(w, "O", VIEW, lambda db, ctx: _draft(db, w))
    claim = {
        k: await _claim_on(w, draft, f"Claim {k} holds.", docs[k], texts[k])
        for k in ("r1", "r4", "r5")
    }
    release_p = await _release(w, "P")

    # 2. Root: frozen tips, R6 labelled, PRISMA hash from retained rows.
    await _refused(_create(w, "R", "root-r"), 403)
    await _refused(_create(w, "F", "root-f"), 404)
    root, replayed = await _create(w, "O", "root")
    assert not replayed and root.version_number == 1
    assert [(m.report_id, m.stage, m.kind) for m in root.missing_history] == [
        (r["r6"], TA, "decision_missing")
    ]
    assert {(c.report_id, c.stage) for c in root.carried} == {
        (r[k], stage) for k in ("r1", "r2", "r3", "r4", "r5") for stage in (TA, FT)
    }
    async with factory() as db:
        context = await resolve_project(db, w.p1, w.ids["O"])
        inputs = await prisma_service.load_inputs(db, context)
    heads = {
        k: v
        for k, v in inputs.versions["stream_heads"].items()
        if not k.startswith(svc.AGGREGATE_TYPE)
    }
    body = prisma.derive_prisma_flow(
        replace(inputs, versions={**inputs.versions, "stream_heads": heads})
    )
    assert prisma.package(body)["body_sha256"] == root.prisma_body_hash
    assert body["counts"]["included_studies"] == 4  # R1, S, R4, R5
    assert (await _accounting(w, root.id)).flow_matches_frozen_hash

    # 3. Delta: two schedules fire once each over the same results. R1, R2
    # and R6 unchanged, R4 retitled, R5 retracted (Crossref), new N1 and N2,
    # R3 not returned.
    schedules = []
    for key in ("sa", "sb"):
        schedule, _ = await _as(
            w,
            "O",
            SUPERVISE,
            lambda db, ctx: updates.create_schedule(
                db,
                ctx,
                w.ids["O"],
                SearchScheduleCreate(
                    source_run_id=w.ids["run"],
                    step_id="search",
                    strategy_version=w.strategy["strategy_version"],
                    cron=CRON,
                    timezone=TZ,
                    idempotency_key=key,
                ),
            ),
        )
        schedules.append(schedule)
    first = datetime.strptime(
        cast(str, schedules[0].next_fire_local), search_rules.LOCAL_FORMAT
    )
    zone = search_rules.parse_schedule(CRON, TZ)[1]

    def fire(weeks: int) -> datetime:
        return search_rules.to_utc(first + timedelta(weeks=weeks), zone) + timedelta(
            minutes=1
        )

    w.crossref.docs = [
        _doc("r1"),
        _doc("r2"),
        _doc("r4", "Four (revised)"),
        _doc("r5"),
        _doc("r6"),
        _doc("n1"),
        _doc("n2"),
    ]
    w.crossref.notices = {DOI["r5"]: [NOTICE]}
    async with factory() as db:
        executions = await updates.claim_due(db, fire(0))
    assert len(executions) == 2
    for execution in executions:
        async with factory() as db:
            outcome = await updates.run_execution(
                db, execution, lambda _org: w.connectors, worker="test"
            )
        assert outcome == "succeeded"
    deltas = [await _delta(w, e) for e in executions]
    classes = {i.report_id: (i.delta_class, i.reason) for i in deltas[0].items}
    assert classes[str(r["r1"])] == ("unchanged", None)
    assert classes[str(r["r4"])][0] == "changed"
    assert classes[str(r["r5"])][0] == "corrected_retracted"
    assert classes[str(r["r3"])] == ("unknown", "not_returned")
    new = sorted(rid for rid, (c, _) in classes.items() if c == "new")
    async with factory() as db:
        by_doi = dict(
            (await db.execute(text("""SELECT value, report_id::text
                            FROM research_report_identifiers WHERE kind = 'doi'""")))
            .tuples()
            .all()
        )
    r["n1"], r["n2"] = UUID(by_doi[DOI["n1"]]), UUID(by_doi[DOI["n2"]])
    assert new == sorted([str(r["n1"]), str(r["n2"])])
    xmin_before = await _xmins(factory)

    # 4 + 5. Two concurrent successors of the root, each accepting a
    # first-fire delta: one wins, the other is stale (UNIQUE(parent)).
    results = await asyncio.gather(
        _successor(w, "s1-a", root.id, deltas[0]),
        _successor(w, "s1-b", root.id, deltas[1]),
        return_exceptions=True,
    )
    won = [x for x in results if not isinstance(x, BaseException)]
    lost = [x for x in results if isinstance(x, BaseException)]
    assert len(won) == 1 and len(lost) == 1, results
    assert isinstance(lost[0], HTTPException) and lost[0].status_code == 409
    assert lost[0].detail == svc.STALE_VERSION
    s1, replayed = won[0]
    assert not replayed and s1.version_number == 2
    winner = 0 if s1.accepted_execution_id == deltas[0].execution_id else 1
    delta = deltas[winner]
    assert (
        await _scalar(
            factory,
            "SELECT count(*) FROM research_review_versions WHERE "
            "parent_review_version_id = :p",
            p=root.id,
        )
        == 1
    )
    again, replayed = await _successor(w, ["s1-a", "s1-b"][winner], root.id, delta)
    assert replayed and again.id == s1.id
    await _refused(
        _create(
            w,
            "O",
            "s1-again",
            parent_review_version_id=root.id,
            execution_id=delta.execution_id,
            delta_hash="0" * 64,
        ),
        409,
        svc.DELTA_CHANGED,
    )

    # 4. Carried by reference, targeted work, needs-attention, predecessor.
    root_refs = {(c.report_id, c.stage): c.resolution_id for c in root.carried}
    assert {(c.report_id, c.stage): c.resolution_id for c in s1.carried} == {
        (r[k], stage): root_refs[(r[k], stage)]
        for k in ("r1", "r2")
        for stage in (TA, FT)
    }
    assert set(s1.required_work[TA]) == {r[k] for k in ("r4", "r5", "n1", "n2")}
    assert s1.required_work[FT] == []
    assert [(a.report_id, a.delta_class, a.reason) for a in s1.needs_attention] == [
        (r["r3"], "unknown", "not_returned")
    ]
    assert [(m.report_id, m.kind) for m in s1.missing_history] == [
        (r["r6"], "decision_missing")
    ]
    ta_work, ft_work = _status(s1, TA), _status(s1, FT)
    assert ta_work.status == "queued" and ta_work.unresolved_count == 4
    assert set(ta_work.required_report_ids) == set(s1.required_work[TA])
    assert set(ta_work.assigned_reviewer_ids) == {w.ids["R"], w.ids["B"]}
    assert ft_work.status == "waiting_on_title_abstract"
    r4_parent = root_refs[(r["r4"], TA)]
    assert (
        await _scalar(
            factory,
            "SELECT count(*) FROM screening_resolutions "
            "WHERE supersedes_resolution_id = :r",
            r=r4_parent,
        )
        == 0
    )
    queues = await _scalar(factory, "SELECT count(*) FROM screening_queues")
    await _work(w, s1.id)
    assert await _scalar(factory, "SELECT count(*) FROM screening_queues") == queues
    await _refused(_successor(w, "s2-early", s1.id, delta), 409, svc.DELTA_OUT_OF_ORDER)

    # 6. Screen the new work; accounting reconciles from retained rows.
    await _screen(
        w,
        ta_work.queue_id,
        {
            r["r4"]: ("include", "include"),
            r["r5"]: ("exclude", "exclude"),
            r["n1"]: ("include", "include"),
            r["n2"]: ("include", "include"),
        },
    )
    assert (await _accounting(w, s1.id)).error == "Review work is unresolved"
    s1 = await _work(w, s1.id)
    ft_work = _status(s1, FT)
    assert ft_work.status == "queued"
    assert set(ft_work.required_report_ids) == {r[k] for k in ("r4", "n1", "n2")}
    for k in ("n1", "n2"):
        docs[k] = await _retrieve(w, r[k], texts.get(k, f"Report {k} full text."))
    await _screen(
        w, ft_work.queue_id, {r[k]: ("include", "include") for k in ("r4", "n1", "n2")}
    )
    await _study(w, r["n2"], study, "s-n2")
    accounting = await _accounting(w, s1.id)
    boxes = accounting.boxes
    assert boxes is not None, accounting.error
    assert boxes["studies_in_previous_version"] == 4
    assert boxes["new_studies_included"] == 1  # N1; N2 is a report of S
    assert boxes["amended_out"] == 1 and boxes["amended_in"] == 0
    assert boxes["total_studies_included"] == 4
    assert boxes["amended_inclusion"] == [
        {"report_id": str(r["r5"]), "from": "include", "to": "not_included"}
    ]
    assert boxes["withheld_reports"] == [str(r["r3"])]
    assert boxes["new_records_identified"] == 7
    assert boxes["duplicates_removed"] == 5
    assert boxes["corrected_retracted"] == 1 and boxes["changed_sources"] == 1
    assert all(boxes["checks"].values())
    receipts = await _scalar(factory, "SELECT count(*) FROM research_import_receipts")
    async with factory() as db:
        rerun = await updates.run_execution(
            db, delta.execution_id, lambda _org: w.connectors, worker="test"
        )
    assert rerun == "succeeded"
    assert (
        await _scalar(factory, "SELECT count(*) FROM research_import_receipts")
        == receipts
    )
    assert (await _accounting(w, s1.id)).boxes == boxes

    # 7. Targeted staleness through GOO-307's walk; nothing was stamped.
    async with factory() as db:
        stale = (await draft_release_service._graph(db, w.p1)).stale_nodes()
    for k in ("r4", "r5"):
        assert rr.node("link", claim[k][0]) in stale
        assert rr.node("assessment", claim[k][1]) in stale
    assert rr.node("link", claim["r1"][0]) not in stale
    assert rr.node("assessment", claim["r1"][1]) not in stale
    listed = await _versions(w)
    assert listed[str(s1.id)].stale_counts["assessment"] == 2
    xmin_after = await _xmins(factory)
    assert {k: xmin_after[k] for k in xmin_before} == xmin_before

    # 8. Changed protocol: an amendment changes the title/abstract criteria;
    # the next successor carries nothing and requires every screened report.
    amended = uuid4()
    await _sql(
        factory,
        """INSERT INTO research_protocol_versions (
               id, protocol_id, version, parent_version_id, question_version_id,
               blueprint_id, execution_plan, snapshot, content_hash, status,
               change_kind, amendment_reason, author_user_id, approved_by_user_id,
               approved_at, superseded_at, created_at)
           SELECT :new, protocol_id, 2, id, question_version_id, blueprint_id,
               execution_plan,
               jsonb_set(snapshot, '{eligibility}', '{"population": "amended"}'),
               repeat('f', 64), 'approved', 'amendment', 'Fixture amendment',
               author_user_id, approved_by_user_id, now(), NULL, now()
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
    strategy2 = _crossref_strategy(str(amended), "search-2")
    await _journal(w, [w.strategy, strategy2])
    schedule = schedules[winner]
    await _as(
        w,
        "O",
        SUPERVISE,
        lambda db, ctx: updates.version_schedule(
            db,
            ctx,
            w.ids["O"],
            schedule.schedule_id,
            SearchScheduleVersionCreate(
                expected_tip_id=schedule.tip.id,
                source_run_id=w.ids["run"],
                step_id="search-2",
                strategy_version=strategy2["strategy_version"],
                idempotency_key="amended-strategy",
            ),
        ),
    )
    async with factory() as db:
        claimed = await updates.claim_due(db, fire(1))
    async with factory() as db:
        mine = (
            await db.execute(
                text("""SELECT id FROM research_search_executions
                        WHERE schedule_id = :s AND id = ANY(:ids)"""),
                {"s": schedule.schedule_id, "ids": claimed},
            )
        ).scalar_one()
    async with factory() as db:
        assert (
            await updates.run_execution(
                db, mine, lambda _org: w.connectors, worker="test"
            )
            == "succeeded"
        )
    delta2 = await _delta(w, mine)
    assert delta2.baseline_execution_id == delta.execution_id
    s2, _ = await _successor(w, "s2", s1.id, delta2)
    assert s2.carried == []
    assert set(s2.required_work[TA]) == {
        r[k] for k in ("r1", "r2", "r4", "r5", "n1", "n2")
    }
    assert [(m.report_id, m.kind) for m in s2.missing_history] == [
        (r["r6"], "decision_missing")
    ]

    # 9. Release lineage: P on the root, P2 supersedes P on the successor;
    # a wrong supersession is refused and P's row never changes.
    p_row = await _scalar(
        factory,
        "SELECT row_to_json(m)::text FROM manuscript_releases m WHERE id = :p",
        p=release_p,
    )

    def link(version: UUID, release: UUID, supersedes: UUID | None, key: str) -> Any:
        return _as(
            w,
            "O",
            SUPERVISE,
            lambda db, ctx: svc.link_release(
                db,
                ctx,
                w.ids["O"],
                version,
                ReviewReleaseLinkCreate(
                    release_id=release,
                    supersedes_release_id=supersedes,
                    idempotency_key=key,
                ),
            ),
        )

    await _refused(link(s1.id, release_p, None, "too-old"), 409, svc.RELEASE_TOO_OLD)
    root_link, _ = await link(root.id, release_p, None, "p")
    assert root_link.package_sha256 == _sha("package-P")
    release_p2 = await _release(w, "P2")
    await _refused(link(s1.id, release_p2, None, "p2-none"), 409, svc.WRONG_SUPERSEDED)
    s1_link, _ = await link(s1.id, release_p2, release_p, "p2")
    assert s1_link.supersedes_release_id == release_p
    release_p3 = await _release(w, "P3")
    await _refused(
        link(s2.id, release_p3, release_p, "p3-wrong"), 409, svc.WRONG_SUPERSEDED
    )
    await _refused(
        link(root.id, release_p3, None, "root-again"), 409, svc.RELEASE_LINKED
    )
    assert (
        await _scalar(
            factory,
            "SELECT row_to_json(m)::text FROM manuscript_releases m WHERE id = :p",
            p=release_p,
        )
        == p_row
    )

    # 10. Exports: each version rebuilds its own corpus, decisions with
    # actors, accounting and release from the rows it references.
    async def export(version: UUID) -> dict[str, Any]:
        package = await _as(
            w, "R", VIEW, lambda db, ctx: svc.export_version(db, ctx, version)
        )
        assert package["schema"] == svc.EXPORT_SCHEMA
        assert package["body_sha256"] == _sha(_canonical(package["body"]))
        return cast(dict[str, Any], package["body"])

    root_body, s1_body = await export(root.id), await export(s1.id)
    assert [c["report_id"] for c in root_body["corpus"]] == [
        str(x) for x in root.report_ids
    ]
    r4_root = next(
        c
        for c in root_body["carried"]
        if c["report_id"] == str(r["r4"]) and c["stage"] == TA
    )
    assert r4_root["resolution"]["basis"] == "adjudicated"
    assert r4_root["resolution"]["event_actor_id"] == str(w.ids["A"])
    assert set(r4_root["resolution"]["reviewer_ids"]) == {
        str(w.ids["R"]),
        str(w.ids["B"]),
    }
    assert root_body["release"]["release_id"] == str(release_p)
    assert str(s1.id) not in json.dumps(root_body)
    carried_r1 = next(c for c in s1_body["carried"] if c["report_id"] == str(r["r1"]))
    assert carried_r1["resolution"]["basis"] == "agreement"
    assert set(carried_r1["resolution"]["reviewer_ids"]) == {
        str(w.ids["R"]),
        str(w.ids["B"]),
    }
    assert {p["resolution_id"] for p in s1_body["predecessors"]} >= {str(r4_parent)}
    ta_body = next(x for x in s1_body["work"] if x["stage"] == TA)
    assert {x["report_id"] for x in ta_body["resolutions"]} == {
        str(r[k]) for k in ("r4", "r5", "n1", "n2")
    }
    assert s1_body["accounting"]["boxes"] == boxes
    assert s1_body["release"]["package_sha256"] == _sha("package-P2")
    assert s1_body["version"]["accepted_execution_id"] == str(delta.execution_id)

    # 11. Archived/deleted, insert-only, replay, downgrade.
    await _sql(
        factory,
        "UPDATE workspaces SET is_archived = true WHERE id = :w",
        w=w.ids["workspace"],
    )
    await _refused(_successor(w, "archived", s2.id, delta2), 409)
    assert await _versions(w)
    await _sql(
        factory,
        "UPDATE workspaces SET is_archived = false WHERE id = :w",
        w=w.ids["workspace"],
    )
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
    assert kinds.count("review_update.versioned") == 3
    assert kinds.count("review_update.release_linked") == 2
    async with factory() as db:
        connection = await db.connection()

        def refuse(sync: Connection) -> None:
            with pytest.raises(RuntimeError, match="refusing"):
                _migration(sync, "downgrade")

        await connection.run_sync(refuse)
        await db.rollback()
    await _sql(
        factory, "UPDATE collections SET is_deleted = true WHERE id = :c", c=w.p1
    )
    await _refused(_versions(w), 404)
    if os.getenv("RESEARCH_DECISION_DATABASE_URL"):
        await _downgrade_on_fresh_schema()


async def _draft(db: AsyncSession, w: Any) -> Any:
    """One current draft whose sentences the claims anchor to."""
    draft = GeneratedDraft(
        project_id=w.p1,
        version=1,
        title="Review",
        content="## Results\nClaim r1 holds. Claim r4 holds. Claim r5 holds.\n",
        is_current=True,
    )
    db.add(draft)
    await db.commit()
    return draft
