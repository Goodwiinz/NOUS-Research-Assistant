"""Real PostgreSQL proof for GOO-303 full-text acquisition and the derived
PRISMA 2020 flow.

Reuses GOO-301's ``screening_factory``: ``create_all``, then the acquisition,
resolution, screening, import and identity tables are dropped and rebuilt by
their own revisions in chain order (``c9d2e4f6a8b1`` -> ``d4e6f8a0b2c3`` ->
``e1f3a5c7d9b2`` -> ``f3b5d7e9a1c4`` -> ``f2a4c6e8b0d3``), which proves this
migration on real PostgreSQL. Seed: owner/supervisor O (workspace owner, so
EDIT), reviewers R and R2, adjudicator A, role-less viewer V, foreign-org F.

Every exported count is compared with an expected value computed here by raw
SQL; the expected side never calls ``prisma`` (the module is imported only for
its ``SCHEMA`` constant). The raw SQL applies the same universe (final reports
that have records) and merged-report rules as the derivation, so it stays
honest for seeds with merges that carry outcomes or requests.

Mutation verification (docs/engineering/testing.md); full record in
``docs/testing/agent-orchestration-mutation-checks.md`` (GOO-303 section):

- Head check in ``acquisition_service.record_attempt``
  (``if data.previous_attempt_id != (None if head is None else head.id)``)
  neutralized -> ``-k concurrent`` fails: the blocked writer hits
  ``IntegrityError`` on ``uq_research_fulltext_attempt_head`` instead of 409.
- Idempotent replay in ``request_fulltext`` / ``record_attempt``
  (``if replayed is not None``) neutralized -> ``-k recomputes`` fails:
  the replayed request gets 409 ``Full text already requested``; with only the
  ``record_attempt`` replay neutralized, the replayed retry gets 409
  ``Full text already retrieved``.
- Full-text gate in ``screening_service.submit`` (``if queue.stage ==
  "full_text" and not await retrieved_report_ids(...)``) neutralized ->
  ``-k concurrent`` fails: the blocked full-text exclude returns 200.
- Duplicate-row check ``prisma._unique`` made a no-op -> the unit test
  ``test_duplicate_input_rows_raise[record]`` fails (records inflate); doubled
  attempt/outcome rows are additionally caught by the chain-linearity checks.
"""

import asyncio
from datetime import date
from typing import Any, TypeAlias, cast
from uuid import UUID, uuid4

import pytest
from fastapi import HTTPException, Response
from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from src.api.research_engine import acquisition as routes
from src.models.collection import CollectionDocument
from src.models.document import Document, DocumentType
from src.models.research_decision import ResearchDecisionEvent
from src.models.research_fulltext import (
    ResearchFulltextAttempt,
    ResearchFulltextRequest,
)
from src.models.research_report import ResearchReportObservation
from src.models.research_run import ResearchRun
from src.models.research_source import ResearchSource
from src.models.screening import ScreeningObservation, ScreeningQueue
from src.models.user import User
from src.schemas.research_engine import (
    FulltextAttemptCreate,
    FulltextRequestCreate,
    ImportDeclaration,
    ReportMergeRequest,
    ScreeningAdjudicateRequest,
    ScreeningReopenRequest,
    StudyLinkRequest,
)
from src.services.research_decisions import replay_decisions
from src.services.research_engine import (
    acquisition_service,
    corpus_service,
    identity_service,
    prisma,
    prisma_service,
    screening_service,
)
from src.services.research_engine.project_access import ResearchAction, resolve_project
from tests.integration.test_report_identity_postgres import (
    _observe,
    _wait_until_blocked,
)
from tests.integration.test_screening_queue_postgres import (  # noqa: F401
    _act,
    _assign,
    _body,
    _create_queue,
    _status,
    _submit,
    _world,
    _World,
    screening_factory,
)

pytestmark = pytest.mark.integration

Factory: TypeAlias = async_sessionmaker[AsyncSession]


# --- helpers ---------------------------------------------------------------


async def _document(factory: Factory, world: _World, org: str = "org") -> UUID:
    """An uploaded, hashed document attached to the project (projects.py:390)."""
    async with factory() as db:
        document = Document(
            title="full text",
            filename="full.pdf",
            file_path="local:///full.pdf",
            file_size_bytes=1,
            mime_type="application/pdf",
            document_type=DocumentType.PDF,
            organization_id=world.ids[org],
            checksum_sha256=uuid4().hex * 2,
        )
        db.add(document)
        await db.flush()
        db.add(
            CollectionDocument(collection_id=world.collection, document_id=document.id)
        )
        await db.commit()
        return cast(UUID, document.id)


async def _request(factory: Factory, world: _World, report: UUID, key: str) -> Any:
    state, created = await _act(
        factory,
        world,
        "O",
        ResearchAction.EDIT,
        lambda db, ctx: acquisition_service.request_fulltext(
            db,
            ctx,
            world.ids["O"],
            FulltextRequestCreate(report_id=report, idempotency_key=key),
        ),
    )
    return state, created


def _attempt_body(outcome: str, key: str, **fields: Any) -> FulltextAttemptCreate:
    if outcome == "unavailable":
        fields.setdefault("reason", "not held by library")
    return FulltextAttemptCreate(
        outcome=outcome,  # type: ignore[arg-type]
        attempted_on=date(2026, 9, 29),
        idempotency_key=key,
        **fields,
    )


async def _attempt(
    factory: Factory, world: _World, request_id: UUID, body: FulltextAttemptCreate
) -> Any:
    return await _act(
        factory,
        world,
        "O",
        ResearchAction.EDIT,
        lambda db, ctx: acquisition_service.record_attempt(
            db, ctx, request_id, world.ids["O"], body
        ),
    )


async def _sources(
    factory: Factory, run_id: UUID, *specs: tuple[str, list[str]]
) -> list[ResearchSource]:
    """(doi, provenance connectors) per source; in-run merges keep every one."""
    async with factory() as db:
        rows = [
            ResearchSource(
                run_id=run_id,
                connector_type=connectors[0],
                title=f"Paper {doi}",
                metadata_={
                    "identifiers": {"doi": doi},
                    "provenance": [{"connector_type": c} for c in connectors],
                },
            )
            for doi, connectors in specs
        ]
        db.add_all(rows)
        await db.commit()
        return rows


async def _report_of(factory: Factory, source_id: UUID) -> UUID:
    async with factory() as db:
        return cast(
            UUID,
            (
                await db.execute(
                    select(ResearchReportObservation.report_id).where(
                        ResearchReportObservation.source_id == source_id
                    )
                )
            ).scalar_one(),
        )


async def _flow(factory: Factory, world: _World, user: str = "O") -> dict[str, Any]:
    """GET /prisma through the route function, in a fresh session."""
    async with factory() as db:
        current = await db.get(User, world.ids[user])
        assert isinstance(current, User)
        return cast(
            dict[str, Any],
            await routes.prisma_flow_route(world.collection, current, db),
        )


async def _both(
    factory: Factory,
    world: _World,
    queue: Any,
    mine: Any,
    peer: Any,
    report: UUID,
    key: str,
    decision: str = "include",
    **fields: Any,
) -> Any:
    await _submit(
        factory,
        world,
        "R",
        queue,
        _body(queue, mine, report, key, decision=decision, **fields),
    )
    return await _submit(
        factory,
        world,
        "R2",
        queue,
        _body(queue, peer, report, key + "-2", decision=decision, **fields),
    )


# --- raw SQL (independent of prisma.py) -------------------------------------

_FINALS = """
WITH RECURSIVE f(id, final) AS (
    SELECT id, id FROM research_reports
    WHERE collection_id = :c AND merged_into_report_id IS NULL
  UNION ALL
    SELECT r.id, f.final FROM research_reports r
    JOIN f ON r.merged_into_report_id = f.id
),
u AS (
    SELECT DISTINCT f.final FROM f WHERE f.id IN (
        SELECT report_id FROM research_report_observations
        WHERE collection_id = :c
        UNION SELECT report_id FROM research_import_records
        WHERE collection_id = :c AND status = 'accepted')
)
"""


async def _raw_counts(factory: Factory, collection_id: UUID) -> dict[str, Any]:
    c = {"c": collection_id}
    async with factory() as db:

        async def rows(sql: str) -> list[Any]:
            return list((await db.execute(text(sql), c)).all())

        providers = await rows(
            """SELECT COALESCE(p->>'connector_type', s.connector_type), count(*)
            FROM research_report_observations o
            JOIN research_sources s ON s.id = o.source_id
            CROSS JOIN LATERAL jsonb_array_elements(
                CASE WHEN jsonb_typeof(s.metadata->'provenance') = 'array'
                      AND jsonb_array_length(s.metadata->'provenance') > 0
                     THEN s.metadata->'provenance' ELSE '[{}]'::jsonb END) p
            WHERE o.collection_id = :c GROUP BY 1"""
        )
        imports = await rows(
            """SELECT COALESCE(rc.declared->>'database', rc.kind), count(*)
            FROM research_import_records ir
            JOIN research_import_receipts rc ON rc.id = ir.receipt_id
            WHERE ir.collection_id = :c AND ir.status = 'accepted' GROUP BY 1"""
        )
        [(rejected,)] = await rows("""SELECT count(*) FROM research_import_records
            WHERE collection_id = :c AND status = 'rejected'""")
        [(unique,)] = await rows(_FINALS + "SELECT count(*) FROM u")
        # One tip per (stage, final report) inside the universe: the
        # survivor's own tip wins, else the latest; only decided tips count.
        tips = [
            t
            for t in await rows(
                _FINALS + """SELECT DISTINCT ON (q.stage, f.final) q.stage, f.final,
                  CASE WHEN t.basis IN ('single', 'agreement', 'adjudicated')
                        AND t.outcome IN ('include', 'exclude')
                       THEN t.outcome END,
                  t.exclusion_reason
                FROM screening_resolutions t
                JOIN screening_queues q ON q.id = t.queue_id
                JOIN f ON f.id = t.report_id
                JOIN u ON u.final = f.final
                WHERE q.collection_id = :c
                  AND NOT EXISTS (SELECT 1 FROM screening_resolutions l
                                  WHERE l.supersedes_resolution_id = t.id)
                  AND NOT EXISTS (SELECT 1 FROM screening_queues n
                                  WHERE n.supersedes_queue_id = q.id)
                ORDER BY q.stage, f.final, (t.report_id = f.final) DESC,
                         t.created_at DESC, t.event_id::text DESC"""
            )
            if t[2] is not None
        ]
        # Chain heads (or bare requests) inside the universe, with the
        # ledger seq of the event that wrote each.
        heads = await rows(
            _FINALS
            + """SELECT f.final, a.outcome, e.seq FROM research_fulltext_requests rq
            JOIN f ON f.id = rq.report_id
            JOIN u ON u.final = f.final
            LEFT JOIN research_fulltext_attempts a ON a.request_id = rq.id
              AND NOT EXISTS (SELECT 1 FROM research_fulltext_attempts n
                              WHERE n.previous_attempt_id = a.id)
            JOIN research_decision_events e ON e.collection_id = :c
              AND ((a.id IS NOT NULL AND e.event_type <> 'acquisition.requested'
                    AND e.payload->>'attempt_id' = a.id::text)
                OR (a.id IS NULL AND e.event_type = 'acquisition.requested'
                    AND e.payload->>'request_id' = rq.id::text))
            WHERE rq.collection_id = :c"""
        )
        included = [
            final
            for stage, final, outcome, _ in tips
            if stage == "full_text" and outcome == "include"
        ]
        study_rows = (
            await db.execute(
                text("""SELECT id, study_id, study_link_status FROM research_reports
                    WHERE id = ANY(:ids)"""),
                {"ids": included},
            )
        ).all()
    records = sum(n for _, n in providers) + sum(n for _, n in imports)
    screened = [t for t in tips if t[0] == "title_abstract"]
    assessed = [t for t in tips if t[0] == "full_text"]
    # Retrieved if any request is; otherwise the latest request's head.
    state: dict[UUID, str] = {}
    for final, outcome, _seq in sorted(heads, key=lambda h: h[2]):
        if state.get(final) != "retrieved":
            state[final] = outcome or "pending"
    reasons: dict[str, int] = {}
    for _, _, outcome, reason in assessed:
        if outcome == "exclude":
            reasons[reason] = reasons.get(reason, 0) + 1
    confirmed = {s for _, s, status in study_rows if status == "confirmed"}
    unconfirmed = [r for r, _, status in study_rows if status != "confirmed"]
    return {
        "records_identified": records,
        "records_by_source": dict(sorted((str(k), n) for k, n in providers)),
        "records_by_import": dict(sorted((str(k), n) for k, n in imports)),
        "import_rejected": rejected,
        "duplicates_removed": records - unique,
        "unique_reports": unique,
        "records_screened": len(screened),
        "records_excluded": sum(t[2] == "exclude" for t in screened),
        "records_awaiting_screening": unique - len(screened),
        "reports_sought": len(state),
        "reports_not_retrieved": sum(v == "unavailable" for v in state.values()),
        "reports_awaiting_retrieval": sum(
            v in ("requested", "pending") for v in state.values()
        ),
        "reports_assessed": len(assessed),
        "reports_excluded_by_reason": dict(sorted(reasons.items())),
        "included_reports": len(included),
        "included_studies": len(confirmed) + len(unconfirmed),
        "unconfirmed_study_links": sum(
            status is not None and status != "confirmed" for _, _, status in study_rows
        ),
    }


async def _row_counts(factory: Factory) -> tuple[int, ...]:
    async with factory() as db:
        return tuple(
            [
                int(
                    (await db.execute(select(func.count()).select_from(m))).scalar_one()
                )
                for m in (
                    ResearchFulltextRequest,
                    ResearchFulltextAttempt,
                    ScreeningObservation,
                    ResearchDecisionEvent,
                )
            ]
        )


# --- tests -------------------------------------------------------------------


@pytest.mark.asyncio
async def test_flow_recomputes_from_raw_rows_after_commit_and_reopen(
    screening_factory: Factory,
) -> None:
    factory = screening_factory
    world = await _world(factory)
    ids = world.ids
    r1, r2, r3 = world.reports

    # Known corpus. Run 1: openalex+pubmed return one DOI (one source, two
    # provenance entries); run 2 finds that DOI again; five more DOIs.
    async with factory() as db:
        run = await db.get(ResearchRun, ids["run"])
        assert isinstance(run, ResearchRun)
        run2 = ResearchRun(blueprint_id=run.blueprint_id, blueprint_version=1)
        db.add(run2)
        await db.commit()
        run2_id = cast(UUID, run2.id)
    shared, s4, s5, s6, s7, s8, s9 = await _sources(
        factory,
        ids["run"],
        ("10.1000/shared", ["openalex", "pubmed"]),
        ("10.1000/s4", ["openalex"]),
        ("10.1000/s5", ["pubmed"]),
        ("10.1000/s6", ["openalex"]),
        ("10.1000/s7", ["openalex"]),
        ("10.1000/s8", ["pubmed"]),
        ("10.1000/s9", ["openalex"]),
    )
    [again] = await _sources(factory, run2_id, ("10.1000/shared", ["openalex"]))
    await _observe(factory, world.collection, [shared, s4, s5, s6, s7, s8, s9, again])
    ra, r4, r5, r6, r7, r8, r9 = [
        await _report_of(factory, cast(UUID, s.id))
        for s in (shared, s4, s5, s6, s7, s8, s9)
    ]
    assert await _report_of(factory, cast(UUID, again.id)) == ra

    # GOO-300 import: one duplicate of r4 and one rejected record.
    ris = (
        b"TY  - JOUR\nTI  - Paper four\nDO  - 10.1000/s4\nER  - \n"
        b"TY  - JOUR\nAU  - Nobody\nER  - \n"
    )
    await _act(
        factory,
        world,
        "O",
        ResearchAction.EDIT,
        lambda db, ctx: corpus_service.import_file(
            db,
            ctx,
            ids["O"],
            ImportDeclaration(database="Embase"),
            fmt="ris",
            filename="embase.ris",
            data=ris,
        ),
    )

    # GOO-299: merge one pair; r4 and r5 describe one confirmed study.
    await _act(
        factory,
        world,
        "A",
        ResearchAction.ADJUDICATE,
        lambda db, ctx: identity_service.merge_reports(
            db,
            ctx,
            ids["A"],
            ReportMergeRequest(
                surviving_report_id=r8,
                merged_report_ids=[r9],
                rationale="same paper",
                idempotency_key="merge",
            ),
        ),
    )
    linked = await _act(
        factory,
        world,
        "A",
        ResearchAction.ADJUDICATE,
        lambda db, ctx: identity_service.link_study(
            db,
            ctx,
            r4,
            ids["A"],
            StudyLinkRequest(status="confirmed", rationale="s", idempotency_key="l4"),
        ),
    )
    await _act(
        factory,
        world,
        "A",
        ResearchAction.ADJUDICATE,
        lambda db, ctx: identity_service.link_study(
            db,
            ctx,
            r5,
            ids["A"],
            StudyLinkRequest(
                study_id=linked.study_id,
                status="confirmed",
                rationale="s",
                idempotency_key="l5",
            ),
        ),
    )

    # Title/abstract: dual review; one conflict adjudicated; one reopened and
    # re-resolved; r7 has one vote only (awaiting).
    ta = await _create_queue(factory, world, "ta")
    mine = await _assign(factory, world, ta.id, "R")
    peer = await _assign(factory, world, ta.id, "R2")
    for report in (r1, r2, r3, r4, r5, r6, r8):
        await _both(factory, world, ta, mine, peer, report, f"ta-{report}")
    await _submit(factory, world, "R", ta, _body(ta, mine, ra, "ta-a"))
    conflict = (
        await _submit(
            factory,
            world,
            "R2",
            ta,
            _body(ta, peer, ra, "ta-a-2", decision="exclude"),
        )
    ).resolution
    assert conflict is not None and conflict.basis == "conflict"
    await _act(
        factory,
        world,
        "A",
        ResearchAction.ADJUDICATE,
        lambda db, ctx: screening_service.adjudicate(
            db,
            ctx,
            ta.id,
            ra,
            ids["A"],
            ScreeningAdjudicateRequest(
                resolution_id=conflict.id,
                input_observation_ids=conflict.input_observation_ids,
                criteria_hash=ta.criteria_hash,
                decision="exclude",
                exclusion_reason=None,
                rationale="off topic",
                idempotency_key="adj",
            ),
        ),
    )
    agreed = await _act(
        factory,
        world,
        "R",
        ResearchAction.VIEW,
        lambda db, ctx: screening_service._tip(db, ta.id, r8),
    )
    await _act(
        factory,
        world,
        "A",
        ResearchAction.ADJUDICATE,
        lambda db, ctx: screening_service.reopen(
            db,
            ctx,
            ta.id,
            r8,
            ids["A"],
            ScreeningReopenRequest(
                resolution_id=agreed.id, rationale="recheck", idempotency_key="reopen"
            ),
        ),
    )
    olds = {UUID(str(i)) for i in agreed.input_observation_ids}
    async with factory() as db:
        by_reviewer: dict[UUID, UUID] = {
            cast(UUID, o.reviewer_id): cast(UUID, o.id)
            for o in (
                await db.execute(
                    select(ScreeningObservation).where(
                        ScreeningObservation.id.in_(olds)
                    )
                )
            ).scalars()
        }
    for user, assignment in (("R", mine), ("R2", peer)):
        await _submit(
            factory,
            world,
            user,
            ta,
            _body(
                ta,
                assignment,
                r8,
                f"re-{user}",
                decision="exclude",
                supersedes_observation_id=by_reviewer[ids[user]],
            ),
        )
    await _submit(factory, world, "R", ta, _body(ta, mine, r7, "ta-7"))

    # Acquisition: r2 unavailable; r3 unavailable then retrieved; r4, r5
    # retrieved; r6 requested with no attempts.
    requests = {}
    for report in (r2, r3, r4, r5, r6):
        requests[report] = (await _request(factory, world, report, f"rq-{report}"))[0]
    await _attempt(
        factory, world, requests[r2].request_id, _attempt_body("unavailable", "u2")
    )
    first3, _ = await _attempt(
        factory, world, requests[r3].request_id, _attempt_body("unavailable", "u3")
    )
    retry_body = _attempt_body(
        "retrieved",
        "g3",
        document_id=await _document(factory, world),
        previous_attempt_id=first3.head_attempt_id,
    )
    await _attempt(factory, world, requests[r3].request_id, retry_body)
    for report in (r4, r5):
        await _attempt(
            factory,
            world,
            requests[report].request_id,
            _attempt_body(
                "retrieved", f"g-{report}", document_id=await _document(factory, world)
            ),
        )

    # Full text: r3 excluded, r4 and r5 included; r2 is gated.
    ft = await _create_queue(
        factory, world, "ft", stage="full_text", report_ids=[r2, r3, r4, r5]
    )
    ft_mine = await _assign(factory, world, ft.id, "R")
    ft_peer = await _assign(factory, world, ft.id, "R2")
    assert await _status(
        _submit(
            factory,
            world,
            "R",
            ft,
            _body(
                ft,
                ft_mine,
                r2,
                "ft-2",
                decision="exclude",
                exclusion_reason="wrong design",
            ),
        )
    ) == (409, "Full text not retrieved")
    await _both(
        factory,
        world,
        ft,
        ft_mine,
        ft_peer,
        r3,
        "ft-3",
        "exclude",
        exclusion_reason="wrong design",
    )
    for report in (r4, r5):
        await _both(factory, world, ft, ft_mine, ft_peer, report, f"ft-{report}")

    # Replays: same keys, same bodies -> 200 and nothing new.
    before = await _row_counts(factory)
    for report in (r2, r3, r4, r5, r6):
        replayed, created = await _request(factory, world, report, f"rq-{report}")
        assert (replayed.request_id, created) == (requests[report].request_id, False)
    state, created = await _attempt(factory, world, requests[r3].request_id, retry_body)
    assert (state.state, created) == ("retrieved", False)
    again_obs = await _submit(
        factory, world, "R", ft, _body(ft, ft_mine, r4, f"ft-{r4}")
    )
    assert again_obs.report_id == r4
    assert await _row_counts(factory) == before

    # A new session recomputes from raw rows; compare with independent SQL.
    flow = await _flow(factory, world)
    body = flow["body"]
    raw = await _raw_counts(factory, world.collection)
    assert {k: body["counts"][k] for k in raw} == raw
    assert raw["records_by_source"] == {"openalex": 9, "pubmed": 3}
    assert raw["records_by_import"] == {"Embase": 1}
    assert raw["import_rejected"] == 1
    assert (raw["unique_reports"], raw["duplicates_removed"]) == (9, 4)
    assert (raw["records_screened"], raw["records_excluded"]) == (8, 2)
    assert raw["records_awaiting_screening"] == 1
    assert (raw["reports_sought"], raw["reports_not_retrieved"]) == (5, 1)
    assert raw["reports_awaiting_retrieval"] == 1
    assert raw["reports_excluded_by_reason"] == {"wrong design": 1}
    assert (raw["included_reports"], raw["included_studies"]) == (2, 1)
    assert all(body["checks"].values())
    kinds = {(a["kind"], a["report_id"]) for a in body["amendments"]}
    assert ("title_abstract.reopened", str(r8)) in kinds
    assert ("title_abstract.adjudicated", str(ra)) in kinds
    assert ("acquisition.retrieved", str(r3)) in kinds

    # Every stream still replays.
    async with factory() as db:
        for aggregate_type, aggregate_id in (
            ("research_identity", world.collection),
            ("research_acquisition", world.collection),
            *(
                ("research_screening", queue_id)
                for queue_id in (
                    await db.execute(
                        select(ScreeningQueue.id).where(
                            ScreeningQueue.collection_id == world.collection
                        )
                    )
                ).scalars()
            ),
        ):
            assert await replay_decisions(
                db,
                collection_id=world.collection,
                aggregate_type=aggregate_type,
                aggregate_id=aggregate_id,
            )

    # The export is bound to the stream heads.
    assert (await _flow(factory, world))["body_sha256"] == flow["body_sha256"]
    await _attempt(
        factory, world, requests[r6].request_id, _attempt_body("requested", "q6")
    )
    assert (await _flow(factory, world))["body_sha256"] != flow["body_sha256"]


@pytest.mark.asyncio
async def test_concurrent_acquisition_and_eligibility_stay_reconstructable(
    screening_factory: Factory,
) -> None:
    factory = screening_factory
    world = await _world(factory)
    ids = world.ids
    r1, r2, r3 = world.reports
    req1, _ = await _request(factory, world, r1, "rq-1")
    req2, _ = await _request(factory, world, r2, "rq-2")
    req3, _ = await _request(factory, world, r3, "rq-3")

    async def race(
        request_id: UUID, first: FulltextAttemptCreate, second: FulltextAttemptCreate
    ) -> tuple[Any, Any]:
        """Session one holds the Collection lock; session two is seen waiting."""
        async with factory() as one, factory() as two, factory() as observer:
            pid = cast(
                int, (await two.execute(text("SELECT pg_backend_pid()"))).scalar_one()
            )
            ctx = await resolve_project(
                one, world.collection, ids["O"], ResearchAction.EDIT
            )
            result = await acquisition_service.record_attempt(
                one, ctx, request_id, ids["O"], first
            )

            async def blocked() -> Any:
                ctx2 = await resolve_project(
                    two, world.collection, ids["O"], ResearchAction.EDIT
                )
                return await acquisition_service.record_attempt(
                    two, ctx2, request_id, ids["O"], second
                )

            task = asyncio.create_task(blocked())
            try:
                await _wait_until_blocked(observer, pid)
                await one.commit()
                try:
                    other = await task
                    await two.commit()
                except HTTPException as error:
                    other = error
                    await two.rollback()
            finally:
                if not task.done():
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)
        return result, other

    async def attempts(request_id: UUID) -> int:
        async with factory() as db:
            return int(
                (
                    await db.execute(
                        select(func.count())
                        .select_from(ResearchFulltextAttempt)
                        .where(ResearchFulltextAttempt.request_id == request_id)
                    )
                ).scalar_one()
            )

    # 1. Same head, different keys: one wins, the other is stale; one head.
    won, lost = await race(
        req1.request_id,
        _attempt_body("requested", "k1"),
        _attempt_body("unavailable", "k2"),
    )
    assert isinstance(lost, HTTPException)
    assert (lost.status_code, lost.detail) == (
        409,
        "Attempt is stale; reload acquisition state",
    )
    assert won[0].state == "requested" and await attempts(req1.request_id) == 1

    # 2. Same key and body: both see one attempt, one row, one event.
    same = _attempt_body("requested", "same")
    first, second = await race(req2.request_id, same, same)
    assert not isinstance(second, HTTPException)
    assert second == (first[0].model_copy(), False) or (
        second[0].head_attempt_id == first[0].head_attempt_id and second[1] is False
    )
    assert await attempts(req2.request_id) == 1

    # 3. Unavailable vs. a full-text exclude on r3: the lock serializes them,
    # and the exclude can never land without a retrieval.
    ft = await _create_queue(factory, world, "ft", stage="full_text", report_ids=[r3])
    reviewer = await _assign(factory, world, ft.id, "R")
    exclude = _body(
        ft, reviewer, r3, "ex", decision="exclude", exclusion_reason="wrong design"
    )
    async with factory() as one, factory() as two, factory() as observer:
        pid = cast(
            int, (await two.execute(text("SELECT pg_backend_pid()"))).scalar_one()
        )
        ctx = await resolve_project(
            one, world.collection, ids["O"], ResearchAction.EDIT
        )
        await acquisition_service.record_attempt(
            one, ctx, req3.request_id, ids["O"], _attempt_body("unavailable", "u3")
        )

        async def review() -> Any:
            ctx2 = await resolve_project(
                two, world.collection, ids["R"], ResearchAction.REVIEW
            )
            return await screening_service.submit(two, ctx2, ft.id, ids["R"], exclude)

        task = asyncio.create_task(review())
        try:
            await _wait_until_blocked(observer, pid)
            await one.commit()
            assert await _status(task) == (409, "Full text not retrieved")
        finally:
            if not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
            await two.rollback()
    assert await _status(_submit(factory, world, "R", ft, exclude)) == (
        409,
        "Full text not retrieved",
    )

    # 4. The database refuses a second chain head outright.
    async with factory() as db:
        with pytest.raises(IntegrityError, match="uq_research_fulltext_attempt_head"):
            await db.execute(
                text("""INSERT INTO research_fulltext_attempts
                    (id, request_id, outcome, attempted_on, actor_id)
                    VALUES (:id, :rq, 'requested', CURRENT_DATE, :actor)"""),
                {"id": uuid4(), "rq": req1.request_id, "actor": ids["O"]},
            )

    # 5. The flow still equals raw SQL, and r3 is never an exclusion.
    body = (await _flow(factory, world))["body"]
    raw = await _raw_counts(factory, world.collection)
    assert {k: body["counts"][k] for k in raw} == raw
    assert body["counts"]["reports_excluded_by_reason"] == {}
    assert (raw["reports_sought"], raw["reports_not_retrieved"]) == (3, 1)


@pytest.mark.asyncio
async def test_tenancy_and_lifecycle(screening_factory: Factory) -> None:
    factory = screening_factory
    world = await _world(factory)
    other = await _world(factory)
    ids = world.ids
    r1, r2, _r3 = world.reports
    request, _ = await _request(factory, world, r1, "rq-1")
    foreign_request, _ = await _request(factory, other, other.reports[0], "rq-x")

    # 1. A foreign-org user sees no project on any route (one session each).
    calls = (
        lambda db, user: routes.prisma_flow_route(world.collection, user, db),
        lambda db, user: routes.list_fulltext_route(world.collection, user, db),
        lambda db, user: routes.record_attempt_route(
            world.collection,
            request.request_id,
            _attempt_body("requested", "f"),
            Response(),
            user,
            db,
        ),
    )
    for call in calls:
        async with factory() as db:
            foreigner = await db.get(User, ids["F"])
            assert isinstance(foreigner, User)
            assert await _status(call(db, foreigner)) == (404, "Project not found")

    # 2. Another Collection's request id looks missing.
    assert await _status(
        _attempt(
            factory,
            world,
            foreign_request.request_id,
            _attempt_body("requested", "x"),
        )
    ) == (404, "Full text request not found")

    # 3. A document from another organization looks missing.
    foreign_doc = await _document(factory, world, org="foreign_org")
    assert await _status(
        _attempt(
            factory,
            world,
            request.request_id,
            _attempt_body("retrieved", "d", document_id=foreign_doc),
        )
    ) == (404, "Document not found")

    # 5. A role-less workspace viewer cannot write (EDIT).
    assert await _status(
        _act(
            factory,
            world,
            "V",
            ResearchAction.EDIT,
            lambda db, ctx: acquisition_service.request_fulltext(
                db,
                ctx,
                ids["V"],
                FulltextRequestCreate(report_id=r2, idempotency_key="v"),
            ),
        )
    ) == (404, "Project not found")

    # 4. Archive race: the request waits on the Collection lock, then is
    # refused; reads of the archived project still return the same flow.
    before = (await _flow(factory, world))["body"]["counts"]
    async with factory() as archiver, factory() as db, factory() as observer:
        await archiver.execute(
            text("SELECT id FROM collections WHERE id = :id FOR UPDATE"),
            {"id": world.collection},
        )
        await archiver.execute(
            text("UPDATE collections SET research_status = 'archived' WHERE id = :id"),
            {"id": world.collection},
        )
        pid = cast(
            int, (await db.execute(text("SELECT pg_backend_pid()"))).scalar_one()
        )

        async def attempt_request() -> Any:
            ctx = await resolve_project(
                db, world.collection, ids["O"], ResearchAction.EDIT
            )
            return await acquisition_service.request_fulltext(
                db,
                ctx,
                ids["O"],
                FulltextRequestCreate(report_id=r2, idempotency_key="late"),
            )

        task = asyncio.create_task(attempt_request())
        try:
            await _wait_until_blocked(observer, pid)
            await archiver.commit()
            assert await _status(task) == (409, "Project is not writable")
        finally:
            if not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
            await db.rollback()
    assert (await _flow(factory, world))["body"]["counts"] == before
    assert prisma.SCHEMA == (await _flow(factory, world))["schema"]
    async with factory() as db:
        assert (
            await db.execute(select(func.count()).select_from(ResearchFulltextRequest))
        ).scalar_one() == 2
    # The loader is read-only and refuses to run inside a writing transaction.
    async with factory() as db:
        ctx = await resolve_project(
            db, other.collection, other.ids["O"], ResearchAction.EDIT
        )
        with pytest.raises(RuntimeError):
            await prisma_service.load_inputs(db, ctx)


@pytest.mark.asyncio
async def test_loader_reads_one_snapshot_and_refuses_writers(
    screening_factory: Factory,
) -> None:
    """Mutations: drop ``wrote is not None`` in ``_begin_snapshot`` -> the
    flushed-writer case loads instead of raising; delete its
    ``execution_options`` -> isolation stays ``read committed``."""
    factory = screening_factory
    world = await _world(factory)
    async with factory() as db:
        ctx = await resolve_project(
            db, world.collection, world.ids["O"], ResearchAction.VIEW
        )
        await prisma_service.load_inputs(db, ctx)
        level = (await db.execute(text("SHOW transaction_isolation"))).scalar_one()
        assert level == "repeatable read"
    async with factory() as db:
        ctx = await resolve_project(
            db, world.collection, world.ids["O"], ResearchAction.VIEW
        )
        # A flushed write leaves nothing pending in the session, only an xid.
        await db.execute(
            text("UPDATE collections SET name = name WHERE id = :id"),
            {"id": world.collection},
        )
        assert not (db.new or db.dirty or db.deleted)
        with pytest.raises(RuntimeError, match="writing transaction"):
            await prisma_service.load_inputs(db, ctx)
        await db.rollback()
