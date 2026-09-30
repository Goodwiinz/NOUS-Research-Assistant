"""Real PostgreSQL proof for GOO-302 blind dual review and adjudication.

Reuses GOO-301's ``screening_factory`` (which rebuilds the screening tables
through ``e1f3a5c7d9b2`` -> ``f3b5d7e9a1c4``) and its seed: supervisor/owner O
(no ADJUDICATOR role), reviewers R and R2, adjudicator A, role-less viewer V
and foreign-org user F.

Mutation verification (docs/engineering/testing.md), full record in
``docs/testing/agent-orchestration-mutation-checks.md`` (GOO-302 section):

- GOO-301 lock order: both ``.with_for_update(of=Collection)`` in
  ``project_access.lock_active_project`` and the stream ``.with_for_update()``
  in ``ledger._locked_stream`` neutralized -> ``-k simultaneous`` fails:
  session 2 then waits on the ledger's stream upsert instead of the
  Collection row. (It still waits, and still yields one resolution, because
  the stream row is updated by session 1; hence the pg_stat_activity check.)
- ``screening_service.adjudicate`` stale-input comparison removed ->
  ``-k stale`` fails: the pre-reopen tip and ids get 200 instead of 409.
- ``screening_service.history`` redaction returns the raw payload ->
  ``-k redaction`` fails on the sentinel assertion.
- The post-lock role reload in ``resolve_project`` is already mutation-verified
  by ``test_research_authorization_concurrency.py``; not repeated here.
"""

import asyncio
import json
from typing import Any, cast
from uuid import UUID

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from src.models.research_decision import ResearchDecisionEvent
from src.models.research_project_role import (
    ResearchProjectRole,
    ResearchProjectRoleAssignment,
)
from src.models.screening import ScreeningObservation, ScreeningResolution
from src.schemas.research_engine import (
    ScreeningAdjudicateRequest,
    ScreeningReopenRequest,
)
from src.services.research_engine import screening_rules, screening_service
from src.services.research_engine.project_access import ResearchAction, resolve_project
from tests.integration.test_report_identity_postgres import _wait_until_blocked
from tests.integration.test_screening_queue_postgres import (  # noqa: F401
    _act,
    _assign,
    _body,
    _count,
    _create_queue,
    _status,
    _submit,
    _world,
    _World,
    screening_factory,
)

pytestmark = pytest.mark.integration

SENTINEL = "R2-SENTINEL-7f3"


def _dump(value: Any) -> str:
    if isinstance(value, list):
        return json.dumps([_dump(item) for item in value])
    if hasattr(value, "model_dump"):
        return json.dumps(value.model_dump(mode="json"))
    return json.dumps(value, default=str)


async def _read(
    factory: async_sessionmaker[AsyncSession], world: _World, user: str, call: Any
) -> Any:
    """A VIEW read in its own session, never committed."""
    async with factory() as db:
        context = await resolve_project(
            db, world.collection, world.ids[user], ResearchAction.VIEW
        )
        return await call(db, context)


def _resolutions(queue_id: UUID, report_id: UUID | None = None) -> Any:
    query = (
        select(func.count())
        .select_from(ScreeningResolution)
        .where(ScreeningResolution.queue_id == queue_id)
    )
    if report_id is not None:
        query = query.where(ScreeningResolution.report_id == report_id)
    return query


async def _conflict_on(
    factory: async_sessionmaker[AsyncSession],
    world: _World,
    queue: Any,
    mine: Any,
    peer: Any,
    report: UUID,
    key: str,
) -> tuple[Any, Any]:
    first = await _submit(factory, world, "R", queue, _body(queue, mine, report, key))
    second = await _submit(
        factory,
        world,
        "R2",
        queue,
        _body(queue, peer, report, key + "-2", decision="exclude"),
    )
    assert second.resolution is not None and second.resolution.basis == "conflict"
    return first, second


def _adjudication(
    queue: Any, resolution_id: UUID, inputs: list[UUID], key: str
) -> ScreeningAdjudicateRequest:
    return ScreeningAdjudicateRequest(
        resolution_id=resolution_id,
        input_observation_ids=inputs,
        criteria_hash=queue.criteria_hash,
        decision="exclude",
        rationale="protocol 3.2",
        idempotency_key=key,
    )


def _adjudicate(world: _World, user: str, queue: Any, report: UUID, body: Any) -> Any:
    return lambda db, ctx: screening_service.adjudicate(
        db, ctx, queue.id, report, world.ids[user], body
    )


@pytest.mark.asyncio
async def test_pre_reveal_redaction_on_every_read_path(
    screening_factory: async_sessionmaker[AsyncSession],
) -> None:
    factory = screening_factory
    world = await _world(factory)
    r1, _r2, r3 = world.reports
    queue = await _create_queue(
        factory, world, "q1", stage="full_text", report_ids=[r1, r3]
    )
    mine = await _assign(factory, world, queue.id, "R")
    peer = await _assign(factory, world, queue.id, "R2")
    hidden = await _submit(
        factory,
        world,
        "R2",
        queue,
        _body(
            queue,
            peer,
            r1,
            "p1",
            decision="exclude",
            exclusion_reason="wrong design",
            note=SENTINEL,
        ),
    )
    changed = await _submit(
        factory,
        world,
        "R2",
        queue,
        _body(
            queue,
            peer,
            r1,
            "p2",
            decision="exclude",
            exclusion_reason="wrong population",
            note=SENTINEL,
            supersedes_observation_id=hidden.id,
        ),
    )
    await _submit(factory, world, "R", queue, _body(queue, mine, r3, "own"))
    peer_ids = {str(hidden.id), str(changed.id)}

    for user in ("R", "V"):
        history = await _read(
            factory,
            world,
            user,
            lambda db, ctx: screening_service.history(
                db, ctx, queue.id, world.ids[user]
            ),
        )
        assert SENTINEL not in _dump(history)
        redacted = [e for e in history if e.payload.get("observation_id") in peer_ids]
        assert len(redacted) == 2
        assert all(e.redacted and e.reason is None for e in redacted)
        assert not any(
            key in e.payload
            for e in redacted
            for key in ("decision", "exclusion_reason")
        )

    my_queue = await _read(
        factory,
        world,
        "R",
        lambda db, ctx: screening_service.my_queue(db, ctx, queue.id, world.ids["R"]),
    )
    body = _dump(my_queue)
    assert SENTINEL not in body and not any(i in body for i in peer_ids)
    assert my_queue.items[0].reveal_state == "hidden"
    assert my_queue.items[0].others == []
    listed = await _read(factory, world, "R", screening_service.list_queues)
    assert SENTINEL not in _dump(listed)
    assert (listed[0].resolved_count, listed[0].conflict_count) == (0, 0)
    assert await _status(
        _read(
            factory,
            world,
            "R",
            lambda db, ctx: screening_service.conflicts(
                db, ctx, queue.id, world.ids["R"]
            ),
        )
    ) == (403, "adjudicator role required")
    # GOO-300's corpus package carries no screening data at all.
    from src.services.research_engine import corpus_export

    package = await _read(factory, world, "R", corpus_export.build_package)
    assert SENTINEL not in _dump(package)
    assert not any(i in _dump(package) for i in peer_ids)
    assert (
        await _read(
            factory,
            world,
            "A",
            lambda db, ctx: screening_service.conflicts(
                db, ctx, queue.id, world.ids["A"]
            ),
        )
        == []
    )


@pytest.mark.asyncio
async def test_simultaneous_final_submissions_one_resolution(
    screening_factory: async_sessionmaker[AsyncSession],
) -> None:
    factory = screening_factory
    world = await _world(factory)
    r2 = world.reports[1]
    queue = await _create_queue(factory, world, "q1")
    mine = await _assign(factory, world, queue.id, "R")
    peer = await _assign(factory, world, queue.id, "R2")

    one, two, observer = factory(), factory(), factory()
    async with one, two, observer:
        pid = cast(
            int, (await two.execute(text("SELECT pg_backend_pid()"))).scalar_one()
        )
        context = await resolve_project(
            one, world.collection, world.ids["R"], ResearchAction.REVIEW
        )
        first = await screening_service.submit(
            one, context, queue.id, world.ids["R"], _body(queue, mine, r2, "r")
        )
        assert first.resolution is None  # one observation: still hidden

        async def second() -> Any:
            ctx = await resolve_project(
                two, world.collection, world.ids["R2"], ResearchAction.REVIEW
            )
            return await screening_service.submit(
                two,
                ctx,
                queue.id,
                world.ids["R2"],
                _body(queue, peer, r2, "r2", decision="exclude"),
            )

        attempt = asyncio.create_task(second())
        try:
            await _wait_until_blocked(observer, pid)
            # The documented lock order: session 2 waits in resolve_project on
            # the Collection row, before it has read any screening state.
            waiting = (
                await observer.execute(
                    text("SELECT query FROM pg_stat_activity WHERE pid = :pid"),
                    {"pid": pid},
                )
            ).scalar_one()
            assert "FOR UPDATE OF collections" in waiting, waiting
            await one.commit()
            revealed = await attempt
            await two.commit()
        finally:
            if not attempt.done():
                attempt.cancel()
                await asyncio.gather(attempt, return_exceptions=True)

    async with factory() as db:
        [row] = (
            (
                await db.execute(
                    select(ScreeningResolution).where(
                        ScreeningResolution.queue_id == queue.id,
                        ScreeningResolution.report_id == r2,
                    )
                )
            )
            .scalars()
            .all()
        )
        observed_by_r2 = (
            await db.execute(
                select(ResearchDecisionEvent.id).where(
                    ResearchDecisionEvent.event_type == "screening.observed",
                    ResearchDecisionEvent.actor_user_id == world.ids["R2"],
                )
            )
        ).scalar_one()
    assert row.basis == "conflict" and row.outcome is None
    assert set(row.input_observation_ids) == {str(first.id), str(revealed.id)}
    assert row.event_id == observed_by_r2
    assert row.id == screening_rules.auto_resolution_id(observed_by_r2)
    assert revealed.resolution is not None and revealed.resolution.id == row.id

    for user in ("R", "R2"):
        view = await _read(
            factory,
            world,
            user,
            lambda db, ctx: screening_service.my_queue(
                db, ctx, queue.id, world.ids[user]
            ),
        )
        item = next(i for i in view.items if i.report_id == r2)
        assert item.reveal_state == "revealed" and len(item.others) == 1
    history = await _read(
        factory,
        world,
        "V",
        lambda db, ctx: screening_service.history(db, ctx, queue.id, world.ids["V"]),
    )
    assert not any(e.redacted for e in history)

    async with factory() as db:
        with pytest.raises(IntegrityError, match="uq_screening_resolution_initial"):
            async with db.begin_nested():
                db.add(
                    ScreeningResolution(
                        queue_id=queue.id,
                        report_id=r2,
                        basis="conflict",
                        input_observation_ids=[],
                        criteria_hash=queue.criteria_hash,
                        event_id=observed_by_r2,
                    )
                )
                await db.flush()


@pytest.mark.asyncio
async def test_adjudication_stale_input_and_races(
    screening_factory: async_sessionmaker[AsyncSession],
) -> None:
    factory = screening_factory
    world = await _world(factory)
    r1, r2, _r3 = world.reports
    queue = await _create_queue(factory, world, "q1")
    mine = await _assign(factory, world, queue.id, "R")
    peer = await _assign(factory, world, queue.id, "R2")

    # 1. A reads the conflict on r2 (tip T1).
    first, second = await _conflict_on(factory, world, queue, mine, peer, r2, "c")
    conflicts = await _read(
        factory,
        world,
        "A",
        lambda db, ctx: screening_service.conflicts(db, ctx, queue.id, world.ids["A"]),
    )
    [t1] = [c.resolution for c in conflicts]
    # 2. A reopens (T2), 3. both reviewers supersede, still in conflict (T3).
    await _act(
        factory,
        world,
        "A",
        ResearchAction.ADJUDICATE,
        lambda db, ctx: screening_service.reopen(
            db,
            ctx,
            queue.id,
            r2,
            world.ids["A"],
            ScreeningReopenRequest(
                resolution_id=t1.id, rationale="recheck", idempotency_key="reopen"
            ),
        ),
    )
    third = await _submit(
        factory,
        world,
        "R",
        queue,
        _body(queue, mine, r2, "c2", supersedes_observation_id=first.id),
    )
    fourth = await _submit(
        factory,
        world,
        "R2",
        queue,
        _body(
            queue,
            peer,
            r2,
            "c2-2",
            decision="exclude",
            supersedes_observation_id=second.id,
        ),
    )
    t3 = fourth.resolution
    assert t3 is not None and t3.basis == "conflict"

    # 4. The pre-reopen tip and ids are stale; nothing is written.
    async with factory() as db:
        events = await _count(
            db, select(func.count()).select_from(ResearchDecisionEvent)
        )
        resolutions = await _count(db, _resolutions(queue.id))
        before = (
            (
                await db.execute(
                    select(ScreeningObservation).order_by(ScreeningObservation.id)
                )
            )
            .scalars()
            .all()
        )
        snapshot = [
            (o.id, o.decision, o.exclusion_reason, o.note, o.supersedes_observation_id)
            for o in before
        ]
    stale = _adjudication(queue, t1.id, t1.input_observation_ids, "stale")
    assert await _status(
        _act(
            factory,
            world,
            "A",
            ResearchAction.ADJUDICATE,
            _adjudicate(world, "A", queue, r2, stale),
        )
    ) == (409, "Adjudication inputs are stale")
    # An adjudicator who reviewed this report is refused, whatever the inputs.
    async with factory() as db:
        db.add(
            ResearchProjectRoleAssignment(
                collection_id=world.collection,
                user_id=world.ids["R2"],
                role=ResearchProjectRole.ADJUDICATOR,
                assigned_by_id=world.ids["O"],
            )
        )
        await db.commit()
    current = _adjudication(queue, t3.id, t3.input_observation_ids, "ok")
    assert await _status(
        _act(
            factory,
            world,
            "R2",
            ResearchAction.ADJUDICATE,
            _adjudicate(world, "R2", queue, r2, current),
        )
    ) == (403, "Adjudicator reviewed this report")
    async with factory() as db:
        assert (
            await _count(db, select(func.count()).select_from(ResearchDecisionEvent))
            == events
        )
        assert await _count(db, _resolutions(queue.id)) == resolutions

    # 5. The current tip adjudicates; no observation changed.
    done = await _act(
        factory,
        world,
        "A",
        ResearchAction.ADJUDICATE,
        _adjudicate(world, "A", queue, r2, current),
    )
    assert (done.basis, done.outcome) == ("adjudicated", "exclude")
    assert done.supersedes_resolution_id == t3.id
    assert set(done.input_observation_ids) == {third.id, fourth.id}
    async with factory() as db:
        after = (
            (
                await db.execute(
                    select(ScreeningObservation).order_by(ScreeningObservation.id)
                )
            )
            .scalars()
            .all()
        )
        assert [
            (o.id, o.decision, o.exclusion_reason, o.note, o.supersedes_observation_id)
            for o in after
        ] == snapshot
    history = await _read(
        factory,
        world,
        "V",
        lambda db, ctx: screening_service.history(db, ctx, queue.id, world.ids["V"]),
    )
    assert history[-1].event_type == "screening.adjudicated"
    assert history[-1].reason == "protocol 3.2"

    # 7. Archive race: A's reopen waits on the Collection lock, then is refused.
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

        async def attempt_reopen() -> Any:
            ctx = await resolve_project(
                db, world.collection, world.ids["A"], ResearchAction.ADJUDICATE
            )
            return await screening_service.reopen(
                db,
                ctx,
                queue.id,
                r2,
                world.ids["A"],
                ScreeningReopenRequest(
                    resolution_id=done.id, rationale="late", idempotency_key="late"
                ),
            )

        attempt = asyncio.create_task(attempt_reopen())
        try:
            await _wait_until_blocked(observer, pid)
            await archiver.commit()
            # lock_active_project re-evaluates the locked row after the wait.
            assert await _status(attempt) == (409, "Project is not writable")
        finally:
            if not attempt.done():
                attempt.cancel()
                await asyncio.gather(attempt, return_exceptions=True)
            await db.rollback()
    async with factory() as db:
        assert await _count(db, _resolutions(queue.id, r2)) == 4
        # Fixture reset, not the system under test: unarchive for step 6.
        await db.execute(
            text("UPDATE collections SET research_status = 'active' WHERE id = :id"),
            {"id": world.collection},
        )
        await db.commit()

    # 6. Role revocation race: O revokes A's role while A's adjudicate waits.
    await _conflict_on(factory, world, queue, mine, peer, r1, "d")
    [conflict] = await _read(
        factory,
        world,
        "A",
        lambda db, ctx: screening_service.conflicts(db, ctx, queue.id, world.ids["A"]),
    )
    body = _adjudication(
        queue,
        conflict.resolution.id,
        conflict.resolution.input_observation_ids,
        "raced",
    )
    async with factory() as revoker, factory() as adjudicator, factory() as observer:
        await resolve_project(
            revoker, world.collection, world.ids["O"], ResearchAction.MANAGE
        )
        role = (
            await revoker.execute(
                select(ResearchProjectRoleAssignment).where(
                    ResearchProjectRoleAssignment.collection_id == world.collection,
                    ResearchProjectRoleAssignment.user_id == world.ids["A"],
                    ResearchProjectRoleAssignment.role
                    == ResearchProjectRole.ADJUDICATOR,
                )
            )
        ).scalar_one()
        role.soft_delete()
        await revoker.flush()
        pid = cast(
            int,
            (await adjudicator.execute(text("SELECT pg_backend_pid()"))).scalar_one(),
        )

        async def attempt_adjudicate() -> Any:
            ctx = await resolve_project(
                adjudicator, world.collection, world.ids["A"], ResearchAction.ADJUDICATE
            )
            return await screening_service.adjudicate(
                adjudicator, ctx, queue.id, r1, world.ids["A"], body
            )

        attempt = asyncio.create_task(attempt_adjudicate())
        try:
            await _wait_until_blocked(observer, pid)
            await revoker.commit()
            assert await _status(attempt) == (403, "adjudicator role required")
        finally:
            if not attempt.done():
                attempt.cancel()
                await asyncio.gather(attempt, return_exceptions=True)
            await adjudicator.rollback()
    async with factory() as db:
        assert await _count(db, _resolutions(queue.id, r1)) == 1
        assert (
            await _count(
                db,
                select(func.count())
                .select_from(ResearchDecisionEvent)
                .where(ResearchDecisionEvent.idempotency_key == "raced"),
            )
            == 0
        )

    # 8. Foreign user and foreign queue both look missing.
    assert await _status(
        _read(
            factory,
            world,
            "F",
            lambda db, ctx: screening_service.conflicts(
                db, ctx, queue.id, world.ids["F"]
            ),
        )
    ) == (404, "Project not found")
    async with factory() as db:
        assert await _status(
            resolve_project(
                db, world.collection, world.ids["F"], ResearchAction.ADJUDICATE
            )
        ) == (404, "Project not found")
    other = await _world(factory)
    other_queue = await _create_queue(factory, other, "q-other")
    assert await _status(
        _read(
            factory,
            world,
            "R2",
            lambda db, ctx: screening_service.conflicts(
                db, ctx, other_queue.id, world.ids["R2"]
            ),
        )
    ) == (404, "Screening queue not found")
