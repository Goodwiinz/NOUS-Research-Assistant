"""Real PostgreSQL proof for GOO-314 peer-review responses.

Runs on GOO-301's ``screening_factory`` schema, whose migration chain now
ends at ``d2a4c6e8f0b1``: the five peer-review tables, their CHECKs, partial
unique indexes and insert-only triggers come from the migration, not
``Base.metadata``. GOO-299's seed: owner O (no roles), reviewer R,
adjudicator A, foreign-org F; this file adds a second project P2.

Mutation verification (docs/engineering/testing.md), full record in
``docs/testing/agent-orchestration-mutation-checks.md`` (GOO-314 section):
the change-or-rationale CHECK dropped (step 4), the response tip check plus
``UNIQUE(supersedes_response_id)`` removed (step 8), resolution gated on
EDIT instead of ADJUDICATE (step 5), ``touches`` returning ``True`` (step 3).

Command (from ``backend/``): ``RESEARCH_DECISION_DATABASE_URL=... pytest -q
tests/integration/test_peer_review_postgres.py``.
"""

import asyncio
import hashlib
import json
from types import SimpleNamespace
from typing import Any, Awaitable, Callable, TypeAlias, TypeVar, cast
from uuid import UUID, uuid4

import pytest
from fastapi import HTTPException
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from src.models.collection import Collection
from src.models.generated_draft import GeneratedDraft
from src.models.peer_review import PeerReviewResponse
from src.services.research import claims_service as claims
from src.services.research import peer_review_service as svc
from src.services.research.draft_generation_service import (
    DraftGenerationService,
    DraftRetainedError,
)
from src.services.research_decisions import replay_decisions
from src.services.research_engine.contracts import canonical_json_sha256
from src.services.research_engine.project_access import (
    ProjectContext,
    ResearchAction,
    resolve_project,
)
from src.shared.claim_schemas import ClaimCreate
from src.shared.peer_review_schemas import (
    AnchorCreate,
    CommentCreate,
    DecisionCreate,
    ResponseCreate,
    ReviewerCreate,
    RoundCreate,
)
from tests.integration.test_report_identity_postgres import _seed
from tests.integration.test_screening_queue_postgres import (  # noqa: F401
    screening_factory,
)

pytestmark = [pytest.mark.integration, pytest.mark.requires_postgres]

Factory: TypeAlias = async_sessionmaker[AsyncSession]
T = TypeVar("T")
ALPHA = "Alpha holds in all arms."
BETA = "Beta rises sharply in the treated arm."
BETA2 = "Beta rises modestly in the treated arm."
GAMMA = "Gamma was measured twice."
DELTA = "Delta stays flat."
V1 = f"## Results\n{ALPHA} {BETA} {GAMMA} {DELTA}\n"
V2 = f"## Results\n{ALPHA} {BETA2} {DELTA}\n"  # rewrites BETA, deletes GAMMA
V3 = f"## Results\nAlpha holds in most arms. {BETA} {GAMMA} {DELTA}\n"
EDIT, VIEW = ResearchAction.EDIT, ResearchAction.VIEW
TABLES = (
    "peer_review_rounds",
    "peer_review_reviewers",
    "peer_review_comments",
    "peer_review_responses",
    "peer_review_decisions",
)


def _sha(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


async def _as(
    w: Any,
    user: str,
    action: ResearchAction,
    call: Callable[[AsyncSession, ProjectContext], Awaitable[T]],
    project: UUID | None = None,
) -> T:
    """One route-shaped request: resolve, then the service (which commits)."""
    async with w.factory() as db:
        context = await resolve_project(db, project or w.p1, w.ids[user], action)
        return await call(db, context)


async def _refused(awaitable: Awaitable[Any], status: int, detail: str = "") -> Any:
    with pytest.raises(HTTPException) as error:
        await awaitable
    assert error.value.status_code == status, error.value.detail
    assert detail in str(error.value.detail)
    return error.value.detail


async def _count(factory: Factory, sql: str, **params: Any) -> int:
    async with factory() as db:
        return int((await db.execute(text(sql), params)).scalar_one())


async def _claim_version(w: Any, project: UUID, draft: UUID, content: str) -> UUID:
    start = content.index(ALPHA)
    claim, _ = await _as(
        w,
        "O",
        EDIT,
        lambda db, ctx: claims.create_claim(
            db,
            ctx,
            w.ids["O"],
            ClaimCreate(
                draft_id=draft,
                start_char=start,
                end_char=start + len(ALPHA),
                text=ALPHA,
                idempotency_key=f"claim-{project}",
            ),
        ),
        project,
    )
    return cast(UUID, claim.version.id)


async def _setup(factory: Factory) -> Any:
    ids = await _seed(factory)
    p1, p2 = ids["collection"], uuid4()
    async with factory() as db:
        db.add(Collection(id=p2, workspace_id=ids["workspace"], name="p2"))
        drafts = {
            name: GeneratedDraft(
                project_id=project,
                version=version,
                title=name,
                content=content,
                is_current=current,
            )
            for name, project, version, content, current in (
                ("v1", p1, 1, V1, False),
                ("v2", p1, 2, V2, True),
                ("v3", p1, 3, V3, False),
                ("p2v1", p2, 1, V1, True),
                ("p2v2", p2, 2, V2, False),
            )
        }
        db.add_all(drafts.values())
        await db.commit()
    w = SimpleNamespace(
        factory=factory,
        ids=ids,
        p1=p1,
        p2=p2,
        **{name: cast(UUID, d.id) for name, d in drafts.items()},
    )
    w.evidence = await _claim_version(w, p1, w.v1, V1)
    w.foreign_evidence = await _claim_version(w, p2, w.p2v1, V1)
    return w


def _anchor(quote: str) -> AnchorCreate:
    start = V1.index(quote)
    return AnchorCreate(start=start, end=start + len(quote), quote=quote)


async def _respond(w: Any, root: UUID, key: str, **data: Any) -> Any:
    result, _ = await _as(
        w,
        "O",
        EDIT,
        lambda db, ctx: svc.respond(
            db, ctx, w.ids["O"], root, ResponseCreate(idempotency_key=key, **data)
        ),
    )
    return result


async def _decide(w: Any, user: str, root: UUID, key: str, **data: Any) -> Any:
    request = DecisionCreate(idempotency_key=key, **data)
    result, _ = await _as(
        w,
        user,
        svc.decision_action(request.kind),
        lambda db, ctx: svc.decide(db, ctx, w.ids[user], root, request),
    )
    return result


async def test_peer_review_round_change_no_change_unresolved_and_concurrency(
    screening_factory: Factory,
) -> None:
    factory = screening_factory
    w = await _setup(factory)
    draft_reviews = await _count(factory, "SELECT count(*) FROM draft_reviews")

    # 1. Round on v1: two external reviewers, three anchored comments.
    round_, replayed = await _as(
        w,
        "O",
        EDIT,
        lambda db, ctx: svc.create_round(
            db,
            ctx,
            w.ids["O"],
            RoundCreate(
                draft_id=w.v1,
                draft_content_hash=_sha(V1),
                label="Round 1",
                reviewers=[
                    ReviewerCreate(label="Reviewer 1"),
                    ReviewerCreate(label="Reviewer 2", display_name="Dr. Two"),
                ],
                idempotency_key="round-1",
            ),
        ),
    )
    assert not replayed and round_.draft_version == 1
    r1, r2 = (r.id for r in round_.reviewers)
    await _refused(
        _as(
            w,
            "O",
            EDIT,
            lambda db, ctx: svc.create_round(
                db,
                ctx,
                w.ids["O"],
                RoundCreate(
                    draft_id=w.v1,
                    draft_content_hash=_sha(V2),
                    label="Wrong hash",
                    reviewers=[ReviewerCreate(label="Reviewer 1")],
                    idempotency_key="round-bad",
                ),
            ),
        ),
        409,
        svc.HASH_MISMATCH,
    )

    async def comment(reviewer: UUID, number: int, quote: str) -> UUID:
        row, _ = await _as(
            w,
            "O",
            EDIT,
            lambda db, ctx: svc.add_comment(
                db,
                ctx,
                w.ids["O"],
                round_.id,
                CommentCreate(
                    reviewer_id=reviewer,
                    number=number,
                    body=f"Please revisit: {quote}",
                    anchor=_anchor(quote),
                    idempotency_key=f"comment-{number}-{reviewer}",
                ),
            ),
        )
        assert row.comment_root_id == row.id
        return cast(UUID, row.id)

    c1 = await comment(r1, 1, BETA)
    c2 = await comment(r1, 2, DELTA)
    c3 = await comment(r2, 3, GAMMA)
    await _refused(comment(r2, 1, ALPHA), 409, svc.NUMBER_TAKEN)

    # 2. Assign C1 (EDIT): the role-less owner may assign to a member.
    assigned = await _decide(
        w, "O", c1, "assign-1", kind="assigned", assignee_id=w.ids["R"]
    )
    assert assigned.actor_role == "editor"
    await _refused(
        _decide(w, "O", c1, "assign-f", kind="assigned", assignee_id=w.ids["F"]),
        422,
        "not a project member",
    )

    # 3. Change response to C1 with v2 (+ evidence); an untouched revision 422s.
    await _refused(
        _respond(
            w,
            c1,
            "c1-untouched",
            kind="change",
            body="Edited the intro.",
            revised_draft_id=w.v3,
        ),
        422,
        "does not touch",
    )
    change = await _respond(
        w,
        c1,
        "c1-change",
        kind="change",
        body="Softened the claim.",
        revised_draft_id=w.v2,
        evidence_claim_version_ids=[w.evidence],
    )
    assert change.base_draft_id == w.v1 and change.diff_sha256
    assert change.revised_content_hash == _sha(V2)

    # 4. No-change response to C2: an attributed rationale.
    no_change = await _respond(
        w,
        c2,
        "c2-keep",
        kind="no_change",
        body="We keep this sentence.",
        rationale="The flat trend is the primary finding.",
    )
    assert no_change.author_id == w.ids["O"] and no_change.revised_draft_id is None
    replay, replayed = await _as(
        w,
        "O",
        EDIT,
        lambda db, ctx: svc.respond(
            db,
            ctx,
            w.ids["O"],
            c2,
            ResponseCreate(
                kind="no_change",
                body="We keep this sentence.",
                rationale="The flat trend is the primary finding.",
                idempotency_key="c2-keep",
            ),
        ),
    )
    assert replayed and replay.id == no_change.id
    await _refused(
        _respond(w, c2, "c2-blank", kind="no_change", body="x", rationale="  "),
        422,
        "requires a rationale",
    )
    # The database refuses an empty no-change even past the service.
    async with factory() as db:
        db.add(
            PeerReviewResponse(
                comment_root_id=c3,
                kind="no_change",
                body="x",
                rationale=" ",
                evidence_claim_version_ids=[],
                author_id=w.ids["O"],
            )
        )
        with pytest.raises(IntegrityError, match="ck_peer_review_responses"):
            await db.commit()

    # 5. Resolve C1: the role-less owner is refused; the adjudicator resolves.
    await _refused(
        _decide(w, "O", c1, "resolve-o", kind="resolved", response_id=change.id),
        403,
        "adjudicator",
    )
    resolved = await _decide(
        w, "A", c1, "resolve-a", kind="resolved", response_id=change.id
    )
    assert resolved.actor_role == "adjudicator"

    # 6. Reload in a fresh session against v2.
    detail = await _as(
        w, "R", VIEW, lambda db, ctx: svc.get_round(db, ctx, round_.id, w.v2)
    )
    by_root = {c.comment_root_id: c for c in detail.comments}
    first, second, third = by_root[c1], by_root[c2], by_root[c3]
    assert (first.status, first.anchor_state) == ("resolved", "unresolved_anchor")
    assert first.current.quote == BETA and first.current.start_char == V1.index(BETA)
    assert first.response is not None and first.response.diff_verified is True
    assert any(BETA2 in h.new_text for h in first.response.hunks)
    assert [e.claim_version_id for e in first.response.evidence] == [w.evidence]
    assert (second.status, second.anchor_state) == ("responded", "carried")
    assert (second.anchor_start, second.anchor_end) == (
        V2.index(DELTA),
        V2.index(DELTA) + len(DELTA),
    )
    assert second.anchor_start != V1.index(DELTA)
    assert (third.status, third.anchor_state) == ("open", "unresolved_anchor")
    assert third.current.quote == GAMMA
    obligations = await _as(
        w, "R", VIEW, lambda db, ctx: svc.open_obligations(db, w.p1, w.v2)
    )
    assert {o["comment_root_id"] for o in obligations} == {str(c2), str(c3)}

    # 7. Export: every comment; unresolved items are C2 and C3.
    markdown, _, media = await _as(
        w,
        "R",
        VIEW,
        lambda db, ctx: svc.export_round(db, ctx, round_.id, "markdown", w.v2),
    )
    text_ = markdown.decode()
    assert media == "text/markdown"
    for number, state in (
        (1, "unresolved_anchor"),
        (2, "carried"),
        (3, "unresolved_anchor"),
    ):
        assert f"### Comment {number} (" in text_ and f"anchor: {state}" in text_
    unresolved = text_.split("## Unresolved items", 1)[1]
    assert "comment 2: responded" in unresolved and "comment 3: open" in unresolved
    assert "comment 1:" not in unresolved and GAMMA in unresolved
    raw, _, _ = await _as(
        w, "R", VIEW, lambda db, ctx: svc.export_round(db, ctx, round_.id, "json", w.v2)
    )
    package = json.loads(raw)
    assert package["schema"] == svc.EXPORT_SCHEMA
    assert package["body_sha256"] == canonical_json_sha256(package["body"])
    assert len(package["body"]["comments"]) == 3
    assert {u["comment_root_id"] for u in package["body"]["unresolved"]} == {
        str(c2),
        str(c3),
    }

    # 8. Concurrency: two responses superseding the same C2 tip.
    async def contend(key: str, body: str) -> Any:
        try:
            return await _respond(
                w,
                c2,
                key,
                kind="no_change",
                body=body,
                rationale=f"{body} rationale",
                supersedes_response_id=no_change.id,
            )
        except HTTPException as error:
            return error

    outcomes = await asyncio.gather(
        contend("race-a", "Winner A"), contend("race-b", "Winner B")
    )
    winners = [o for o in outcomes if not isinstance(o, HTTPException)]
    losers = [o for o in outcomes if isinstance(o, HTTPException)]
    assert len(winners) == 1 and len(losers) == 1, outcomes
    assert losers[0].status_code == 409
    stale = cast(dict[str, Any], losers[0].detail)
    assert stale["message"] == svc.RESPONSE_STALE
    assert stale["current_tip_id"] == str(winners[0].id)
    tips = await _count(
        factory,
        """SELECT count(*) FROM peer_review_responses r WHERE r.comment_root_id = :c
           AND NOT EXISTS (SELECT 1 FROM peer_review_responses n
                           WHERE n.supersedes_response_id = r.id)""",
        c=c2,
    )
    assert tips == 1
    bodies = await _count(
        factory,
        "SELECT count(*) FROM peer_review_responses WHERE comment_root_id = :c",
        c=c2,
    )
    assert bodies == 2  # the original and the winner; nothing overwritten

    # 9. Cross-project evidence and revisions look missing.
    await _refused(
        _respond(
            w,
            c3,
            "c3-foreign-claim",
            kind="no_change",
            body="x",
            rationale="y",
            evidence_claim_version_ids=[w.foreign_evidence],
        ),
        404,
        svc.CLAIM_VERSION_NOT_FOUND,
    )
    await _refused(
        _respond(
            w,
            c3,
            "c3-foreign-draft",
            kind="change",
            body="x",
            revised_draft_id=w.p2v2,
        ),
        404,
        svc.DRAFT_NOT_FOUND,
    )

    # 10. Reviewed and revised versions are retained.
    for draft in (w.v1, w.v2):
        async with factory() as db:
            with pytest.raises(DraftRetainedError):
                await DraftGenerationService(db).delete_draft(w.p1, draft)

    # 11. Insert-only tables; the family replays.
    for table in TABLES:
        for statement in (
            f"UPDATE {table} SET created_at = created_at",
            f"DELETE FROM {table}",
        ):
            async with factory() as db:
                with pytest.raises(DBAPIError) as blocked:
                    await db.execute(text(statement))
                assert getattr(blocked.value.orig, "sqlstate", None) == "55000"
    async with factory() as db:
        events = await replay_decisions(
            db,
            collection_id=w.p1,
            aggregate_type="research_peer_review",
            aggregate_id=w.p1,
        )
    assert [e.seq for e in events] == list(range(1, len(events) + 1))
    assert [str(e.event_type) for e in events].count("review_response.versioned") == 3

    # 12. Machine citation review is untouched throughout.
    assert await _count(factory, "SELECT count(*) FROM draft_reviews") == draft_reviews
