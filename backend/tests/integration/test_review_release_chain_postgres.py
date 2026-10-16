"""GOO-320: real manuscript bytes survive a corrected-source review update.

The older review proof uses synthetic release pointers, and the manuscript
proof reconstructs bytes without a review version. This joins those paths on
native PostgreSQL, using actual promotion, storage, invalidation and exports.
"""

from __future__ import annotations

import asyncio
import hashlib
import io
import json
import zipfile
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, cast
from uuid import UUID

import pytest
from sqlalchemy import text, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from src.models.generated_draft import GeneratedDraft
from src.schemas.research_engine import ReviewReleaseLinkCreate, SearchScheduleCreate
from src.services.artifacts.storage import LocalArtifactStorage
from src.services.research import manuscript_release_service as manuscripts
from src.services.research import statements_service as statements
from src.services.research_engine import review_update_service as reviews
from src.services.research_engine import search_update_rules as search_rules
from src.services.research_engine import search_update_service as updates
from src.services.research_engine.project_access import ResearchAction, resolve_project
from src.shared.statements_schemas import ApprovalCreate, StatementSetCreate
from tests.integration import test_draft_release_postgres as draft_proof
from tests.integration import test_manuscript_release_postgres as manuscript_proof
from tests.integration import test_review_versions_postgres as review_proof
from tests.integration import test_statements_venue_postgres as statement_proof
from tests.integration.test_report_identity_postgres import _wait_until_blocked
from tests.integration.test_screening_queue_postgres import (  # noqa: F401
    screening_factory,
)

pytestmark = [pytest.mark.integration, pytest.mark.requires_postgres]


def _sha(data: bytes | str) -> str:
    return hashlib.sha256(data.encode() if isinstance(data, str) else data).hexdigest()


async def _new_draft(world: Any, sentence: str, version: int) -> Any:
    async with world.factory() as db:
        await db.execute(
            update(GeneratedDraft)
            .where(GeneratedDraft.project_id == world.p1)
            .values(is_current=False)
        )
        draft = GeneratedDraft(
            project_id=world.p1,
            version=version,
            title=f"Review release {version}",
            content=(
                "# Abstract\n## Introduction\n## Methods\n"
                f"## Results\n{sentence}\n## Discussion\n"
            ),
            is_current=True,
        )
        db.add(draft)
        await db.commit()
        return draft


async def _verified_release(world: Any, draft: Any, key: str) -> Any:
    draft_release, _ = await draft_proof._promote(
        world, "A", draft.id, draft.version, f"draft-{key}", _sha(draft.content)
    )
    candidate = await manuscript_proof._candidate(
        world, draft.id, draft.content, f"candidate-{key}"
    )
    assert candidate.failing_obligations == []
    async with world.factory() as db:
        context = await resolve_project(
            db, world.p1, world.ids["O"], ResearchAction.VIEW
        )
        rebuilt = await manuscripts.build_snapshot(db, context, draft)
        saved = await db.scalar(
            text("SELECT snapshot FROM manuscript_releases WHERE id = :id"),
            {"id": candidate.id},
        )
    assert rebuilt == saved, {
        k: (saved.get(k), value)
        for k, value in rebuilt.items()
        if saved.get(k) != value
    }
    verified, replayed = await manuscript_proof._promote_release(
        world, "A", candidate, f"verified-{key}"
    )
    assert not replayed and verified.status == "verified"
    assert verified.draft_release_id == draft_release.id
    return verified


async def _link_release(
    world: Any,
    version: UUID,
    release: UUID,
    supersedes: UUID | None,
    key: str,
    pid_ready: asyncio.Future[int] | None = None,
) -> tuple[Any, bool]:
    async with world.factory() as db:
        if pid_ready is not None:
            pid_ready.set_result(await db.scalar(text("SELECT pg_backend_pid()")))
        context = await resolve_project(
            db, world.p1, world.ids["O"], ResearchAction.SUPERVISE
        )
        return await reviews.link_release(
            db,
            context,
            world.ids["O"],
            version,
            ReviewReleaseLinkCreate(
                release_id=release,
                supersedes_release_id=supersedes,
                idempotency_key=key,
            ),
        )


async def _bind_statements(world: Any) -> None:
    statement_set, _ = await review_proof._as(
        world,
        "O",
        ResearchAction.EDIT,
        lambda db, ctx: statements.version_set(
            db,
            ctx,
            world.ids["O"],
            StatementSetCreate(
                body=statement_proof._body(
                    ethics="Controlled fixture approval.",
                    limitations="Synthetic corpus.",
                ),
                idempotency_key="fixture-statements",
            ),
        ),
    )
    for author in statement_set.authors:
        await review_proof._as(
            world,
            "O",
            ResearchAction.EDIT,
            lambda db, ctx: statements.approve(
                db,
                ctx,
                world.ids["O"],
                statement_set.id,
                ApprovalCreate(
                    author_key=author.author_key,
                    set_hash=statement_set.set_hash,
                    method="recorded_attestation",
                    attestation_note="Controlled fixture attestation.",
                    idempotency_key=f"fixture-approve-{author.author_key}",
                ),
            ),
        )


@pytest.mark.parametrize("with_statements", [False, True], ids=["plain", "statements"])
async def test_corrected_source_preserves_parent_and_successor_release_bytes(
    screening_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    with_statements: bool,
) -> None:
    factory = screening_factory
    artifact_root = tmp_path / "artifacts"
    monkeypatch.setattr(
        manuscripts, "get_artifact_storage", lambda: LocalArtifactStorage(artifact_root)
    )
    world = await review_proof._world(factory)
    if with_statements:
        await _bind_statements(world)
    # The shared helper seeds a completed search journal, not engine execution.
    # Declare its conformance as fixture input; promotion checks stay unmocked.
    await review_proof._sql(
        factory,
        "UPDATE research_runs SET conformance_status = 'conformant' WHERE id = :id",
        id=world.ids["run"],
    )
    reports = await review_proof._corpus(world)
    included = [reports[k] for k in ("r1", "r2", "r3", "r4", "r5")]
    title_queue = await review_proof._queue(
        world, "parent-ta", review_proof.TA, included
    )
    await review_proof._screen(
        world, title_queue.id, {report: ("include", "include") for report in included}
    )
    bodies = {k: f"Report {k} full text. Finding {k} holds." for k in reports}
    documents = {
        k: await review_proof._retrieve(world, reports[k], bodies[k])
        for k in ("r1", "r2", "r3", "r4", "r5")
    }
    fulltext_queue = await review_proof._queue(
        world, "parent-ft", review_proof.FT, included
    )
    await review_proof._screen(
        world,
        fulltext_queue.id,
        {report: ("include", "include") for report in included},
    )
    root, _ = await review_proof._create(world, "O", "root-release-review")

    # Real GOO-315 packages: P cites the soon-retracted R5; Q cites only R1.
    parent_draft = await _new_draft(world, "Claim r5 holds.", 1)
    await review_proof._claim_on(
        world, parent_draft, "Claim r5 holds.", documents["r5"], bodies["r5"]
    )
    parent_release = await _verified_release(world, parent_draft, "parent")
    await _link_release(world, root.id, parent_release.id, None, "link-parent")
    unrelated_draft = await _new_draft(world, "Claim r1 holds.", 2)
    await review_proof._claim_on(
        world, unrelated_draft, "Claim r1 holds.", documents["r1"], bodies["r1"]
    )
    unrelated_release = await _verified_release(world, unrelated_draft, "unrelated")
    pending_draft = await _new_draft(world, "Pending claim from r1.", 3)
    await review_proof._claim_on(
        world, pending_draft, "Pending claim from r1.", documents["r1"], bodies["r1"]
    )
    await draft_proof._promote(
        world,
        "A",
        pending_draft.id,
        pending_draft.version,
        "draft-pending",
        _sha(pending_draft.content),
    )
    pre_update_candidate = await manuscript_proof._candidate(
        world, pending_draft.id, pending_draft.content, "pre-update-candidate"
    )
    assert pre_update_candidate.failing_obligations == []
    before_update = await manuscript_proof._listing(world)
    assert before_update[parent_release.id].status == "verified"
    assert before_update[unrelated_release.id].status == "verified"
    original_root_export = await review_proof._as(
        world,
        "O",
        ResearchAction.VIEW,
        lambda db, ctx: reviews.export_version(db, ctx, root.id),
    )
    original_bytes, original_sha = await review_proof._as(
        world,
        "O",
        ResearchAction.VIEW,
        lambda db, ctx: manuscripts.package_bytes(db, ctx, parent_release.id),
    )
    assert _sha(original_bytes) == original_sha == parent_release.package_sha256
    parent_row = await review_proof._scalar(
        factory,
        "SELECT row_to_json(m)::text FROM manuscript_releases m WHERE id = :id",
        id=parent_release.id,
    )

    schedule, _ = await review_proof._as(
        world,
        "O",
        ResearchAction.SUPERVISE,
        lambda db, ctx: updates.create_schedule(
            db,
            ctx,
            world.ids["O"],
            SearchScheduleCreate(
                source_run_id=world.ids["run"],
                step_id="search",
                strategy_version=world.strategy["strategy_version"],
                cron=review_proof.CRON,
                timezone=review_proof.TZ,
                idempotency_key="release-update-schedule",
            ),
        ),
    )
    local = datetime.strptime(
        cast(str, schedule.next_fire_local), search_rules.LOCAL_FORMAT
    )
    zone = search_rules.parse_schedule(review_proof.CRON, review_proof.TZ)[1]
    world.crossref.docs = [review_proof._doc(k) for k in reports] + [
        review_proof._doc("n1")
    ]
    world.crossref.notices = {review_proof.DOI["r5"]: [review_proof.NOTICE]}
    async with factory() as db:
        executions = await updates.claim_due(
            db, search_rules.to_utc(local, zone) + timedelta(minutes=1)
        )
    assert len(executions) == 1
    async with factory() as db:
        outcome = await updates.run_execution(
            db, executions[0], lambda _org: world.connectors, worker="release-proof"
        )
    assert outcome == "succeeded"
    delta = await review_proof._delta(world, executions[0])
    classes = {item.report_id: item.delta_class for item in delta.items}
    assert classes[str(reports["r5"])] == "corrected_retracted"
    assert classes[str(reports["r1"])] == "unchanged"
    successor, _ = await review_proof._successor(
        world, "successor-release-review", root.id, delta
    )
    listed = await manuscript_proof._listing(world)
    assert listed[parent_release.id].status == "stale"
    assert listed[unrelated_release.id].status == "verified"
    await manuscript_proof._refused(
        manuscript_proof._promote_release(world, "A", pre_update_candidate, "too-old"),
        409,
        manuscripts.CANDIDATE_STALE,
    )
    verified_parent = await manuscript_proof._verify(world, parent_release.id)
    assert verified_parent.package_sha256_ok and verified_parent.bundle_ok
    assert (
        await review_proof._scalar(
            factory,
            "SELECT row_to_json(m)::text FROM manuscript_releases m WHERE id = :id",
            id=parent_release.id,
        )
        == parent_row
    )

    # Complete assigned update work; exclude the retracted report.
    title_work = review_proof._status(successor, review_proof.TA)
    decisions = {
        report: (
            ("exclude", "exclude")
            if report == reports["r5"]
            else ("include", "include")
        )
        for report in title_work.required_report_ids
    }
    await review_proof._screen(world, title_work.queue_id, decisions)
    successor = await review_proof._work(world, successor.id)
    fulltext_work = review_proof._status(successor, review_proof.FT)
    for report in fulltext_work.required_report_ids:
        await review_proof._retrieve(world, report, "New source full text.")
    if fulltext_work.required_report_ids:
        await review_proof._screen(
            world,
            fulltext_work.queue_id,
            {r: ("include", "include") for r in fulltext_work.required_report_ids},
        )
    accounting = await review_proof._accounting(world, successor.id)
    assert accounting.error is None and accounting.boxes is not None
    successor_draft = await _new_draft(world, "Updated claim from r1.", 4)
    await review_proof._claim_on(
        world,
        successor_draft,
        "Updated claim from r1.",
        documents["r1"],
        bodies["r1"],
    )
    successor_release = await _verified_release(world, successor_draft, "successor")

    entered, release = asyncio.Event(), asyncio.Event()
    original_append = reviews._append

    async def held_append(db: AsyncSession, *args: Any, **kwargs: Any) -> None:
        if (
            kwargs.get("event_type") == "review_update.release_linked"
            and kwargs.get("version_id") == successor.id
            and not entered.is_set()
        ):
            entered.set()
            await release.wait()
        await original_append(db, *args, **kwargs)

    monkeypatch.setattr(reviews, "_append", held_append)
    first = asyncio.create_task(
        _link_release(
            world, successor.id, successor_release.id, parent_release.id, "link-next"
        )
    )
    tasks = [first]
    try:
        await asyncio.wait_for(entered.wait(), 10)
        pid_ready: asyncio.Future[int] = asyncio.get_running_loop().create_future()
        tasks.append(
            asyncio.create_task(
                _link_release(
                    world,
                    successor.id,
                    successor_release.id,
                    parent_release.id,
                    "link-next",
                    pid_ready,
                )
            )
        )
        pid = await asyncio.wait_for(pid_ready, 10)
        async with factory() as observer:
            await _wait_until_blocked(observer, pid)
    finally:
        release.set()
        results = await asyncio.gather(*tasks, return_exceptions=True)
    completed = [result for result in results if isinstance(result, tuple)]
    assert len(completed) == 2, results
    assert sorted(replayed for _, replayed in completed) == [False, True]
    assert (
        await review_proof._scalar(
            factory,
            "SELECT count(*) FROM research_review_release_links "
            "WHERE review_version_id = :id",
            id=successor.id,
        )
        == 1
    )
    assert (
        await review_proof._scalar(
            factory,
            "SELECT count(*) FROM research_decision_events "
            "WHERE event_type = 'review_update.release_linked' "
            "AND payload->>'review_version_id' = :id",
            id=str(successor.id),
        )
        == 1
    )

    # New sessions and new storage adapters reload each retained package.
    hashes: dict[UUID, tuple[str, str]] = {}
    for _ in range(2):
        for version, manuscript in (
            (root.id, parent_release),
            (successor.id, successor_release),
        ):
            async with factory() as db:
                await db.execute(text("SET TRANSACTION READ ONLY"))
                context = await resolve_project(
                    db, world.p1, world.ids["O"], ResearchAction.VIEW
                )
                exported = await reviews.export_version(db, context, version)
                package, digest = await manuscripts.package_bytes(
                    db, context, manuscript.id
                )
                checked = await manuscripts.verify(db, context, manuscript.id)
                assert checked.package_sha256_ok and checked.bundle_ok
            assert _sha(package) == digest == manuscript.package_sha256
            assert exported["body"]["release"]["package_sha256"] == digest
            assert exported["body_sha256"] == _sha(
                review_proof._canonical(exported["body"])
            )
            with zipfile.ZipFile(io.BytesIO(package)) as archive:
                assert "manuscript.md" in archive.namelist()
                json.loads(archive.read("manifest.json"))
            current = (exported["body_sha256"], digest)
            assert hashes.setdefault(version, current) == current
            if version == root.id:
                assert package == original_bytes
                assert exported["body_sha256"] == original_root_export["body_sha256"]
    assert hashes[root.id] != hashes[successor.id]
