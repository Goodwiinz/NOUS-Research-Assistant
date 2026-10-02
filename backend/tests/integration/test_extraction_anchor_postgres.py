"""Real PostgreSQL proof for GOO-305 source anchors and reconciliation.

Runs on GOO-301's ``screening_factory`` schema, whose migration chain now ends
at ``b8d0f2a4c6e9`` (the anchor columns), with GOO-299's seed: owner O is the
editor, reviewers R (R1) and R2, adjudicator A (J in the plan), foreign-org
user F. The LLM is stubbed at ``ExtractionMatrixService._get_openai_client``.

Mutation verification (docs/engineering/testing.md), full record in
``docs/testing/agent-orchestration-mutation-checks.md`` (GOO-305 section):

- ``assert_anchor_acceptable`` ambiguous branch
  (``extraction_forms_service.py:849``) -> ``if False:``: the no-start accept
  on the ambiguous allocation value is refused as unverified, not ambiguous
  (test 1, step 3).
- ``assert_anchor_acceptable`` source-hash comparison (``:820``) off: the
  accept after the text change is inserted and writes no ``staled`` (test 2,
  ``DID NOT RAISE``).
- ``_document``'s ``project_documents_query`` (``:581``) -> bare
  ``select(Document)``: the soft-deleted document's evidence is served
  (test 3, step 2, ``DID NOT RAISE``).

Command: ``RESEARCH_DECISION_DATABASE_URL=... pytest -q -x
tests/integration/test_extraction_anchor_postgres.py`` (from ``backend/``).
"""

import asyncio
import json
from types import SimpleNamespace
from typing import Any, TypeAlias, cast
from uuid import UUID

import pytest
from fastapi import HTTPException
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from src.api.research import extraction_matrix as routes
from src.models.collection import CollectionDocument
from src.models.document import Document, DocumentType
from src.models.extraction_matrix import ExtractionMatrix, ExtractionObservation
from src.models.research_decision import ResearchDecisionEvent
from src.models.research_project_role import (
    ResearchProjectRole,
    ResearchProjectRoleAssignment,
)
from src.services.research import extraction_forms_service as svc
from src.services.research import extraction_matrix_service as ems
from src.services.research_decisions import replay_decisions
from src.services.research_engine.project_access import ResearchAction
from src.shared.scispace_schemas import (
    ExtractionAcceptCreate,
    ExtractionObservationCreate,
)
from tests.integration.test_report_identity_postgres import _wait_until_blocked
from tests.integration.test_screening_queue_postgres import (  # noqa: F401
    _World,
    _world,
    screening_factory,
)

pytestmark = pytest.mark.integration

Factory: TypeAlias = async_sessionmaker[AsyncSession]
SAMPLE = "We enrolled 412 participants."
REPEATED = "Allocation was concealed."
DESIGN = "Participants were randomized to two arms."
GARBLED = "Participants were random1zed to two arms."
LENGTH, SAMPLE_AT = 60_000, 55_000
COLUMNS = [{"name": "Sample size"}, {"name": "Allocation"}, {"name": "Design"}]


def _put(body: str, at: int, piece: str) -> str:
    return body[:at] + piece + body[at + len(piece) :]


def _d1_text() -> str:
    body = ("lorem ipsum dolor " * (LENGTH // 18 + 1))[:LENGTH]
    body = _put(body, 0, "[Page 1]\n")
    for page in range(2, 7):
        body = _put(body, 10_000 * (page - 1) - 10, f"\n[Page {page}]\n")
    for at, piece in ((1_000, REPEATED), (2_000, REPEATED), (5_000, DESIGN)):
        body = _put(body, at, piece)
    body = _put(body, SAMPLE_AT, SAMPLE)
    assert len(body) == LENGTH and body.index(SAMPLE) == SAMPLE_AT
    return body


class _LLM:
    """Answers each window from what that window actually contains."""

    def __init__(self) -> None:
        self.calls = 0
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self.create))

    async def create(self, **kwargs: Any) -> Any:
        self.calls += 1
        prompt = kwargs["messages"][0]["content"]
        answer: dict[str, Any] = {
            "Sample size": {"missing": "not_reported"},
            "Allocation": {"missing": "not_reported"},
            "Design": {"missing": "not_reported"},
        }
        if SAMPLE in prompt:
            answer["Sample size"] = {"value": "412", "citation": SAMPLE}
        if REPEATED in prompt:
            answer["Allocation"] = {"value": "concealed", "citation": REPEATED}
        if DESIGN in prompt:
            answer["Design"] = {"value": "RCT", "citation": GARBLED}
        message = SimpleNamespace(content=json.dumps(answer))
        return SimpleNamespace(choices=[SimpleNamespace(message=message)])


class _Cell(SimpleNamespace):
    world: _World
    matrix_id: UUID
    version_id: UUID
    d1: UUID
    d2: UUID
    fields: dict[str, UUID]
    llm: _LLM


def _user(world: _World, key: str) -> Any:
    return SimpleNamespace(id=world.ids[key])


async def _setup(factory: Factory, monkeypatch: pytest.MonkeyPatch) -> _Cell:
    world = await _world(factory)
    async with factory() as db:
        docs = []
        for name, body in (("d1", _d1_text()), ("d2", "")):
            document = Document(
                title=name,
                filename=f"{name}.pdf",
                file_path="local:///d.pdf",
                file_size_bytes=1,
                mime_type="application/pdf",
                document_type=DocumentType.PDF,
                organization_id=world.ids["org"],
                checksum_sha256=(name[-1] * 64),
                content_text=body,
            )
            db.add(document)
            await db.flush()
            db.add(
                CollectionDocument(
                    collection_id=world.collection, document_id=document.id
                )
            )
            docs.append(cast(UUID, document.id))
        context = await routes.resolve_project(
            db, world.collection, world.ids["O"], ResearchAction.EDIT
        )
        matrix = ExtractionMatrix(
            project_id=world.collection, name="anchors", columns=COLUMNS
        )
        db.add(matrix)
        version = await svc.create_version(db, context, matrix, COLUMNS, world.ids["O"])
    llm = _LLM()
    monkeypatch.setattr(ems, "AsyncSessionLocal", factory)
    monkeypatch.setattr(
        ems.ExtractionMatrixService,
        "_get_openai_client",
        staticmethod(lambda: (llm, "stub-model")),
    )
    return _Cell(
        world=world,
        matrix_id=cast(UUID, matrix.id),
        version_id=cast(UUID, version.id),
        d1=docs[0],
        d2=docs[1],
        fields={f["name"]: UUID(f["field_id"]) for f in version.fields},
        llm=llm,
    )


async def _run(factory: Factory, cell: _Cell, task_id: str, docs: list[UUID]) -> Any:
    async with factory() as db:
        matrix = await db.get(ExtractionMatrix, cell.matrix_id)
        kwargs = await svc.extraction_task_kwargs(
            db, matrix, docs, cell.world.ids["O"], task_id
        )
    await ems.ExtractionMatrixService().run_background_extraction(
        matrix_id=cell.matrix_id,
        document_ids=docs,
        columns=kwargs["columns"],
        task_id=task_id,
        form_version_id=UUID(kwargs["form_version_id"]),
        initiated_by_user_id=UUID(kwargs["initiated_by_user_id"]),
        source_hashes=kwargs["source_hashes"],
    )
    return ems._extraction_status[task_id]


async def _machine(factory: Factory, cell: _Cell, doc: UUID, name: str) -> list[Any]:
    async with factory() as db:
        return list(
            (
                await db.execute(
                    select(ExtractionObservation)
                    .where(
                        ExtractionObservation.document_id == doc,
                        ExtractionObservation.field_id == cell.fields[name],
                        ExtractionObservation.kind == "machine",
                    )
                    .order_by(ExtractionObservation.created_at)
                )
            )
            .scalars()
            .all()
        )


async def _observe(
    factory: Factory, cell: _Cell, user: str, key: str, value: Any, citation: str
) -> Any:
    async with factory() as db:
        return await routes.create_observation(
            cell.matrix_id,
            ExtractionObservationCreate(
                document_id=cell.d1,
                field_id=cell.fields["Design"],
                form_version_id=cell.version_id,
                value=value,
                citation=citation,
                idempotency_key=key,
            ),
            current_user=_user(cell.world, user),
            db=db,
        )


async def _listing(
    factory: Factory, cell: _Cell, user: str, name: str, doc: UUID | None = None
) -> Any:
    async with factory() as db:
        return await routes.list_cell_observations(
            cell.matrix_id,
            doc or cell.d1,
            cell.fields[name],
            current_user=_user(cell.world, user),
            db=db,
        )


def _accept_body(
    cell: _Cell, name: str, key: str, cited: list[Any], **body: Any
) -> ExtractionAcceptCreate:
    return ExtractionAcceptCreate(
        document_id=body.pop("document", cell.d1),
        field_id=cell.fields[name],
        form_version_id=cell.version_id,
        observation_ids=[o.id for o in cited],
        value=body.pop("value", cited[0].value),
        rationale="checked against the source text",
        idempotency_key=key,
        **body,
    )


async def _accept(
    factory: Factory,
    cell: _Cell,
    user: str,
    name: str,
    key: str,
    cited: list[Any],
    **body: Any,
) -> Any:
    async with factory() as db:
        return await routes.accept_extraction_value(
            cell.matrix_id,
            _accept_body(cell, name, key, cited, **body),
            current_user=_user(cell.world, user),
            db=db,
        )


async def _status(code: int, call: Any) -> str:
    with pytest.raises(HTTPException) as error:
        await call
    assert error.value.status_code == code, error.value.detail
    return str(error.value.detail)


async def _replay(factory: Factory, cell: _Cell) -> list[Any]:
    async with factory() as db:
        return list(
            await replay_decisions(
                db,
                collection_id=cell.world.collection,
                aggregate_type="research_extraction",
                aggregate_id=cell.matrix_id,
            )
        )


async def _accepted_rows(factory: Factory) -> list[tuple[Any, ...]]:
    async with factory() as db:
        return [
            tuple(row)
            for row in await db.execute(
                text("SELECT * FROM extraction_accepted_values ORDER BY id")
            )
        ]


async def test_anchors_and_reconciliations_survive_commit_and_reopen(
    screening_factory: Factory, monkeypatch: pytest.MonkeyPatch
) -> None:
    factory = screening_factory
    cell = await _setup(factory, monkeypatch)

    # 1. The worker reads every window of D1 and nothing of the empty D2.
    status = await _run(factory, cell, "task-1", [cell.d1, cell.d2])
    assert (status["completed"], status["failed"]) == (2, 0)
    assert cell.llm.calls == 6  # 60k chars: 6 windows, <= 8 (was 1)
    (sample,) = await _machine(factory, cell, cell.d1, "Sample size")
    assert (sample.anchor_status, sample.anchor_start_char) == ("verified", SAMPLE_AT)
    assert sample.anchor_page == 6
    (allocation,) = await _machine(factory, cell, cell.d1, "Allocation")
    assert allocation.anchor_status == "ambiguous"
    assert allocation.anchor_occurrences == [1_000, 2_000]
    assert allocation.anchor_start_char is None
    (design,) = await _machine(factory, cell, cell.d1, "Design")
    assert (design.anchor_status, design.value) == ("unverified", "RCT")
    for name in cell.fields:
        (empty,) = await _machine(factory, cell, cell.d2, name)
        assert (empty.missingness, empty.inspected_coverage) == ("unavailable_text", [])
        assert empty.anchor_status is None

    # 2. Two reviewers record different Design values, each citing the text.
    r1 = await _observe(factory, cell, "R", "r1", "RCT", DESIGN)
    r2 = await _observe(factory, cell, "R2", "r2", "cohort", REPEATED)
    assert r1.anchor is not None and r1.anchor.status == "verified"
    assert r2.anchor is not None and r2.anchor.status == "ambiguous"
    assert r2.anchor.occurrences_in_text == 2
    listing = await _listing(factory, cell, "V", "Design")
    assert {o.id for o in listing.observations} == {design.id, r1.id, r2.id}
    shown = next(o for o in listing.observations if o.id == r1.id)
    assert shown.context_before is not None and shown.context_after is not None
    assert shown.coverage_complete is None and shown.source_changed is False
    listed = await _listing(factory, cell, "V", "Allocation")
    (ambiguous,) = listed.observations
    assert ambiguous.anchor is not None
    assert [o.start_char for o in ambiguous.anchor.occurrence_contexts] == [
        1_000,
        2_000,
    ]
    assert ambiguous.coverage_complete is True

    # 3. J resolves: ambiguous needs an occurrence; unverified needs consent.
    detail = await _status(
        409, _accept(factory, cell, "A", "Allocation", "j1", [allocation])
    )
    assert detail == svc.ANCHOR_AMBIGUOUS
    concealed = await _accept(
        factory, cell, "A", "Allocation", "j2", [allocation], anchor_start=2_000
    )
    assert (concealed.anchor_resolution, concealed.anchor_start_char) == (
        "disambiguated",
        2_000,
    )
    detail = await _status(409, _accept(factory, cell, "A", "Design", "j3", [design]))
    assert detail == svc.ANCHOR_UNVERIFIED
    unverified = await _accept(
        factory, cell, "A", "Design", "j4", [design], accept_unverified=True
    )
    assert unverified.anchor_resolution == "accepted_unverified"
    override = await _accept(
        factory,
        cell,
        "A",
        "Design",
        "j5",
        [design, r1],
        supersedes_accepted_value_id=unverified.id,
    )
    assert (override.anchor_resolution, override.anchor_observation_id) == (
        "verified",
        r1.id,
    )
    counted = await _accept(factory, cell, "A", "Sample size", "j6", [sample])
    assert (counted.anchor_resolution, counted.anchor_end_char) == (
        "verified",
        SAMPLE_AT + len(SAMPLE),
    )

    # 4. A new session proves every verified offset in SQL, not in Python.
    async with factory() as db:
        verified = (await db.execute(text("""
                SELECT o.citation,
                       substring(d.content_text FROM o.anchor_start_char + 1
                                 FOR o.anchor_end_char - o.anchor_start_char),
                       o.text_sha256
                         = encode(sha256(convert_to(d.content_text, 'UTF8')), 'hex')
                FROM extraction_observations o
                JOIN documents d ON d.id = o.document_id
                WHERE o.anchor_status = 'verified'
                """))).all()
        coverage = (
            (
                await db.execute(
                    text("""SELECT DISTINCT inspected_coverage::text
                        FROM extraction_observations
                        WHERE kind = 'machine' AND document_id = :d"""),
                    {"d": cell.d1},
                )
            )
            .scalars()
            .all()
        )
        resolutions = (
            (
                await db.execute(
                    text("SELECT anchor_resolution FROM extraction_accepted_values")
                )
            )
            .scalars()
            .all()
        )
    assert len(verified) == 2  # the sample size and R1's Design override
    assert all(citation == sliced and hashed for citation, sliced, hashed in verified)
    assert coverage == [json.dumps([[0, LENGTH]])]
    assert sorted(resolutions) == [
        "accepted_unverified",
        "disambiguated",
        "verified",
        "verified",
    ]

    # 5. The whole stream replays.
    events = await _replay(factory, cell)
    assert {e.event_schema_version for e in events} == {2}


async def test_source_change_stales_and_blocks_decision(
    screening_factory: Factory, monkeypatch: pytest.MonkeyPatch
) -> None:
    factory = screening_factory
    cell = await _setup(factory, monkeypatch)
    await _run(factory, cell, "task-1", [cell.d1])
    (sample,) = await _machine(factory, cell, cell.d1, "Sample size")
    (allocation,) = await _machine(factory, cell, cell.d1, "Allocation")
    (design,) = await _machine(factory, cell, cell.d1, "Design")
    tip_a = await _accept(factory, cell, "A", "Sample size", "j1", [sample])
    tip_b = await _accept(
        factory, cell, "A", "Allocation", "j2", [allocation], anchor_start=1_000
    )
    before = await _accepted_rows(factory)

    # 1. Reprocessing appends a caption block; the checksum does not change.
    async with factory() as db:
        await db.execute(
            text("""UPDATE documents SET content_text = content_text
                    || E'\\nFIGURES: Figure 1 caption merged.' WHERE id = :id"""),
            {"id": cell.d1},
        )
        await db.commit()

    # 2. Evidence reads say so and show no context.
    listing = await _listing(factory, cell, "V", "Sample size")
    (shown,) = listing.observations
    assert shown.source_changed is True
    assert (shown.context_before, shown.context_after) == (None, None)
    assert [a.source_changed for a in listing.accepted_chain] == [True]

    # 3. The next decision stales both tips, commits that, and refuses.
    detail = await _status(
        409,
        _accept(factory, cell, "A", "Design", "j3", [design], accept_unverified=True),
    )
    assert detail == svc.SOURCE_CHANGED
    async with factory() as db:
        staled = (
            (
                await db.execute(
                    select(ResearchDecisionEvent).where(
                        ResearchDecisionEvent.event_type == "extraction.staled"
                    )
                )
            )
            .scalars()
            .all()
        )
    (event,) = staled
    assert event.event_schema_version == 2 and event.actor_role == "adjudicator"
    assert event.payload["reason"] == "source_changed"
    assert event.payload["document_id"] == str(cell.d1)
    assert event.payload["accepted_value_ids"] == sorted([str(tip_a.id), str(tip_b.id)])
    assert await _accepted_rows(factory) == before  # column-for-column
    await _status(
        409,
        _accept(factory, cell, "A", "Design", "j3", [design], accept_unverified=True),
    )  # a retry stays refused and stales nothing twice

    # 4. A rerun appends observations pinned to the new text; old ones stay.
    await _run(factory, cell, "task-2", [cell.d1])
    rows = await _machine(factory, cell, cell.d1, "Sample size")
    assert [r.id for r in rows][:1] == [sample.id] and len(rows) == 2
    assert rows[0].text_sha256 != rows[1].text_sha256
    fresh = await _accept(
        factory,
        cell,
        "A",
        "Sample size",
        "j4",
        [rows[1]],
        supersedes_accepted_value_id=tip_a.id,
    )
    assert fresh.anchor_resolution == "verified" and fresh.source_changed is False
    await _replay(factory, cell)


async def test_authorization_at_fetch_and_decision(
    screening_factory: Factory, monkeypatch: pytest.MonkeyPatch
) -> None:
    factory = screening_factory
    cell = await _setup(factory, monkeypatch)
    await _run(factory, cell, "task-1", [cell.d1])
    (allocation,) = await _machine(factory, cell, cell.d1, "Allocation")

    def accept(user: str, key: str, **body: Any) -> Any:
        return _accept(
            factory,
            cell,
            user,
            "Allocation",
            key,
            [allocation],
            anchor_start=1_000,
            **body,
        )

    # 1. Another organization sees nothing.
    await _status(404, _listing(factory, cell, "F", "Allocation"))
    await _status(404, accept("F", "f1"))

    # 2. Soft-deleted, then detached: evidence and decision both 404.
    for table, column in (("documents", "id"), ("collection_documents", "document_id")):
        async with factory() as db:
            await db.execute(
                text(f"UPDATE {table} SET is_deleted = true WHERE {column} = :d"),
                {"d": cell.d1},
            )
            await db.commit()
        detail = await _status(404, _listing(factory, cell, "V", "Allocation"))
        assert detail == svc.DOCUMENT_NOT_FOUND and REPEATED not in detail
        await _status(404, accept("A", f"gone-{table}"))
        async with factory() as db:
            await db.execute(
                text(f"UPDATE {table} SET is_deleted = false WHERE {column} = :d"),
                {"d": cell.d1},
            )
            await db.commit()

    # 3. The role is reloaded after the lock: revoked between read and accept.
    await _listing(factory, cell, "A", "Allocation")
    async with factory() as db:
        await db.execute(
            ResearchProjectRoleAssignment.__table__.update()
            .where(
                ResearchProjectRoleAssignment.user_id == cell.world.ids["A"],
                ResearchProjectRoleAssignment.role == ResearchProjectRole.ADJUDICATOR,
            )
            .values(is_deleted=True)
        )
        await db.commit()
    assert await _status(403, accept("A", "revoked")) == "adjudicator role required"
    async with factory() as db:
        await db.execute(
            ResearchProjectRoleAssignment.__table__.update()
            .where(ResearchProjectRoleAssignment.user_id == cell.world.ids["A"])
            .values(is_deleted=False)
        )
        await db.commit()

    # 4. Archiving commits while J waits on the Collection lock.
    async with factory() as db:
        research_status = (
            await db.execute(
                text("SELECT research_status FROM collections WHERE id = :id"),
                {"id": cell.world.collection},
            )
        ).scalar_one()
    async with factory() as archiver, factory() as db, factory() as observer:
        await archiver.execute(
            text("SELECT id FROM collections WHERE id = :id FOR UPDATE"),
            {"id": cell.world.collection},
        )
        await archiver.execute(
            text("UPDATE collections SET research_status = 'archived' WHERE id = :id"),
            {"id": cell.world.collection},
        )
        pid = cast(
            int, (await db.execute(text("SELECT pg_backend_pid()"))).scalar_one()
        )
        attempt = asyncio.create_task(
            routes.accept_extraction_value(
                cell.matrix_id,
                _accept_body(
                    cell, "Allocation", "arch", [allocation], anchor_start=1_000
                ),
                current_user=_user(cell.world, "A"),
                db=db,
            )
        )
        try:
            await _wait_until_blocked(observer, pid)
            await archiver.commit()
            # lock_active_project re-reads the locked row after the wait.
            assert await _status(409, attempt) == "Project is not writable"
        finally:
            if not attempt.done():
                attempt.cancel()
                await asyncio.gather(attempt, return_exceptions=True)
            await db.rollback()
    assert await _status(409, accept("A", "arch2")) == "Project is not writable"
    assert (await _listing(factory, cell, "V", "Allocation")).observations
    async with factory() as db:
        await db.execute(
            text("UPDATE collections SET research_status = :s WHERE id = :id"),
            {"id": cell.world.collection, "s": research_status},
        )
        await db.commit()

    # 5. Two accepts race on one disambiguated anchor: the chain guard holds.
    raced = await asyncio.gather(
        accept("A", "race-1"), accept("A", "race-2"), return_exceptions=True
    )
    winners = [r for r in raced if not isinstance(r, BaseException)]
    losers = [r for r in raced if isinstance(r, HTTPException)]
    assert len(winners) == 1 and winners[0].anchor_resolution == "disambiguated"
    assert [(r.status_code, r.detail) for r in losers] == [(409, svc.ACCEPTED_STALE)]
    await _replay(factory, cell)
