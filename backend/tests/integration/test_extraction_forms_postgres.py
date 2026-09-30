"""Real PostgreSQL proof for GOO-304 extraction forms and observations.

Reuses GOO-301's ``screening_factory`` (``create_all`` + the migration chain
through ``a3c5e7f9b1d4``) and GOO-299's seed: owner O is the editor E,
reviewers R and R2, adjudicator A (J in the plan), foreign-org user F. The
test downgrades ``a3c5e7f9b1d4`` after seeding legacy matrices and cells, so
its upgrade runs against real pre-migration rows.

Mutation verification (docs/engineering/testing.md), full record in
``docs/testing/agent-orchestration-mutation-checks.md`` (GOO-304 section):

- ``accept_value`` tip check (``supersedes_accepted_value_id != tip``) off ->
  the accept on doc2 whose ``supersedes`` names doc1's tip is inserted (it
  passes both unique indexes): ``DID NOT RAISE HTTPException``. Same-cell
  stale accepts still get 409 from the index backstop.
- Tip check off AND the ``_flush_or_conflict`` backstop re-raising -> the
  second ``supersedes=None`` accept raises ``UniqueViolationError`` on
  ``uq_extraction_accepted_initial``.
- The backstop alone re-raising survives: with the tip check on, the stream
  lock serializes accepts, so no writer reaches the index.
- Worker pre-LLM idempotency lookup off -> the task-1 retry re-pays the LLM
  and its append is refused: ``(completed, failed, calls) == (0, 1, 3)``.
"""

import asyncio
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any, TypeAlias, cast
from uuid import UUID, uuid4

import pytest
from fastapi import HTTPException
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from src.api.research import extraction_matrix as routes
from src.models.collection import Collection, CollectionDocument
from src.models.document import Document, DocumentType
from src.models.extraction_matrix import (
    ExtractionAcceptedValue,
    ExtractionCell,
    ExtractionFormVersion,
    ExtractionMatrix,
    ExtractionObservation,
)
from src.models.research_decision import ResearchDecisionEvent
from src.services.research import extraction_forms_service as svc
from src.services.research import extraction_matrix_service as ems
from src.services.research_decisions import replay_decisions
from src.shared.scispace_schemas import (
    ExtractionAcceptCreate,
    ExtractionObservationCreate,
    UpdateMatrixRequest,
)
from tests.integration.test_screening_queue_postgres import (  # noqa: F401
    _World,
    _world,
    screening_factory,
)

pytestmark = pytest.mark.integration

Factory: TypeAlias = async_sessionmaker[AsyncSession]
MIGRATION = (
    Path(__file__).parents[2]
    / "alembic"
    / "versions"
    / "a3c5e7f9b1d4_version_extraction_forms.py"
)
ANCHORS_MIGRATION = MIGRATION.with_name("b8d0f2a4c6e9_add_extraction_source_anchors.py")
CLAIMS_MIGRATION = MIGRATION.with_name("c4e6a8b0d2f5_create_research_claims.py")


class _LLM:
    """Chat-completions stub returning one fixed extraction."""

    def __init__(self, payload: dict[str, Any]) -> None:
        self.payload, self.calls = payload, 0
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self.create))

    async def create(self, **_: Any) -> Any:
        self.calls += 1
        message = SimpleNamespace(content=json.dumps(self.payload))
        return SimpleNamespace(choices=[SimpleNamespace(message=message)])


def _module(path: Path) -> Any:
    spec = importlib.util.spec_from_file_location(path.stem, path)
    assert spec is not None and spec.loader is not None
    module: Any = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


async def _migration(factory: Factory, *steps: str) -> None:
    """Run a3c5e7f9b1d4's steps. Its upgrade also re-applies the GOO-305
    anchor columns (b8d0f2a4c6e9), which the ORM models now carry; the GOO-306
    claim tables (c4e6a8b0d2f5) reference it, so they come off first and go
    back on last."""
    from alembic.migration import MigrationContext
    from alembic.operations import Operations

    module, anchors = _module(MIGRATION), _module(ANCHORS_MIGRATION)
    claims = _module(CLAIMS_MIGRATION)
    async with factory() as db:

        def run(sync_connection: Any) -> None:
            module.op = anchors.op = claims.op = Operations(
                MigrationContext.configure(sync_connection)
            )
            for step in steps:
                if step == "downgrade":
                    claims.downgrade()
                getattr(module, step)()
                if step == "upgrade":
                    anchors.upgrade()
                    claims.upgrade()

        await (await db.connection()).run_sync(run)
        await db.commit()


def _user(world: _World, key: str) -> Any:
    return SimpleNamespace(id=world.ids[key])


async def _seed(factory: Factory, world: _World) -> tuple[list[UUID], UUID, UUID]:
    """Two checksummed documents, one live and one soft-deleted legacy matrix."""
    async with factory() as db:
        docs: list[UUID] = []
        for i in range(2):
            document = Document(
                title=f"d{i}",
                filename=f"d{i}.pdf",
                file_path="local:///d.pdf",
                file_size_bytes=1,
                mime_type="application/pdf",
                document_type=DocumentType.PDF,
                organization_id=world.ids["org"],
                checksum_sha256=uuid4().hex * 2,
                content_text="We enrolled 12 participants in an RCT.",
            )
            db.add(document)
            await db.flush()
            db.add(
                CollectionDocument(
                    collection_id=world.collection, document_id=document.id
                )
            )
            docs.append(cast(UUID, document.id))
        live = ExtractionMatrix(
            project_id=world.collection,
            name="live",
            columns=[
                {"name": "Sample size", "description": "n"},
                {"name": "Design", "description": None},
            ],
        )
        deleted = ExtractionMatrix(
            project_id=world.collection,
            name="gone",
            columns=[{"name": "Design"}],
            is_deleted=True,
        )
        db.add_all([live, deleted])
        await db.flush()
        for column, doc_id, value in (
            ("Sample size", docs[0], "twelve"),
            ("Design", docs[1], "RCT"),
            ("Old col", docs[0], "orphan"),  # a column removed in the past
        ):
            db.add(
                ExtractionCell(
                    matrix_id=live.id,
                    document_id=doc_id,
                    column_name=column,
                    value=value,
                    citation_snippet="p1",
                    confidence=0.8,
                )
            )
        await db.commit()
        return docs, cast(UUID, live.id), cast(UUID, deleted.id)


async def _cells(factory: Factory) -> list[tuple[Any, ...]]:
    async with factory() as db:
        return [
            tuple(row)
            for row in await db.execute(text("""SELECT id, matrix_id, document_id,
                    column_name, value, citation_snippet, confidence, is_deleted
                    FROM extraction_cells ORDER BY id"""))
        ]


async def _view(factory: Factory, matrix_id: UUID, docs: list[UUID]) -> Any:
    async with factory() as db:
        matrix = await db.get(ExtractionMatrix, matrix_id)
        allowed = (
            (
                await db.execute(
                    svc.project_documents_query(cast(Any, matrix).project_id)
                    .with_only_columns(Document.id)
                    .where(Document.id.in_(docs))
                )
            )
            .scalars()
            .all()
        )
        return await svc.cell_view(db, matrix, sorted(allowed, key=str))


async def _patch(factory: Factory, world: _World, matrix_id: UUID, cols: list) -> Any:
    async with factory() as db:
        return await routes.update_matrix(
            matrix_id,
            UpdateMatrixRequest.model_validate({"columns": cols}),
            current_user=_user(world, "O"),
            db=db,
        )


async def _versions(factory: Factory, matrix_id: UUID) -> list[Any]:
    async with factory() as db:
        return list(
            (
                await db.execute(
                    select(ExtractionFormVersion)
                    .where(ExtractionFormVersion.matrix_id == matrix_id)
                    .order_by(ExtractionFormVersion.version_no)
                )
            )
            .scalars()
            .all()
        )


async def _count(factory: Factory, model: Any) -> int:
    async with factory() as db:
        return int(
            (await db.execute(select(func.count()).select_from(model))).scalar_one()
        )


async def _status(code: int, call: Any) -> str:
    with pytest.raises(HTTPException) as error:
        await call
    assert error.value.status_code == code, error.value.detail
    return str(error.value.detail)


async def test_extraction_forms_legacy_migration_and_observation_lifecycle(
    screening_factory: Factory, monkeypatch: pytest.MonkeyPatch
) -> None:
    factory = screening_factory
    world = await _world(factory)
    ids = world.ids

    # 1. Pre-migration state: legacy rows exist, then the revision is re-run.
    await _migration(factory, "downgrade")
    docs, matrix_id, deleted_id = await _seed(factory, world)
    cells_before = await _cells(factory)

    # 2. Migrate in place.
    await _migration(factory, "upgrade")
    versions = {v.matrix_id: v for v in await _versions(factory, matrix_id)}
    versions |= {v.matrix_id: v for v in await _versions(factory, deleted_id)}
    assert set(versions) == {matrix_id, deleted_id}
    v1 = versions[matrix_id]
    for version in versions.values():
        assert (version.version_no, version.provenance) == (1, "legacy_unversioned")
        assert version.created_by_id is None and version.protocol_version_id is None
    assert [f["name"] for f in v1.fields] == ["Sample size", "Design", "Old col"]
    assert {f["type"] for f in v1.fields} == {"text"}
    assert await _cells(factory) == cells_before  # frozen, byte-identical
    summary, cells = await _view(factory, matrix_id, docs)
    assert summary["provenance"] == "legacy_unversioned"
    assert sorted((c["column_name"], c["source"], c["stale"]) for c in cells) == [
        ("Design", "legacy", False),
        ("Old col", "legacy", False),
        ("Sample size", "legacy", False),
    ]
    assert {c["confidence"] for c in cells} == {0.8}
    assert await _count(factory, ExtractionAcceptedValue) == 0

    # 3. Commit/reopen: a fresh session reads the same cells.
    assert (await _view(factory, matrix_id, docs))[1] == cells

    # 4. Amend: E makes Sample size a number -> v2 bound to the approved protocol.
    cols = [{"name": "Sample size", "type": "number"}, {"name": "Design"}]
    await _patch(factory, world, matrix_id, cols)
    await _patch(factory, world, matrix_id, cols)  # identical: no v3
    v1_, v2 = await _versions(factory, matrix_id)
    assert v1_.id == v1.id and v1_.fields == v1.fields
    assert (v2.provenance, v2.created_by_id) == ("authored", ids["O"])
    assert v2.protocol_version_id == world.version_id
    async with factory() as db:
        context = await routes.resolve_project(db, world.collection, ids["V"])
        listed = await svc.list_versions(db, context, matrix_id)
        assert [v.version_no for v in listed] == [1, 2]
    size = next(f for f in v2.fields if f["name"] == "Sample size")
    field_id = UUID(size["field_id"])
    assert size["field_id"] == next(
        f["field_id"] for f in v1.fields if f["name"] == "Sample size"
    )

    # 5. Machine run, rerun and Celery retry of run 1.
    llm = _LLM(
        {
            "Sample size": {"value": "12 participants"},
            "Design": {"missing": "not_reported"},
        }
    )
    monkeypatch.setattr(ems, "AsyncSessionLocal", factory)
    monkeypatch.setattr(
        ems.ExtractionMatrixService,
        "_get_openai_client",
        staticmethod(lambda: (llm, "stub-model")),
    )

    async def run(task_id: str, document_ids: list[UUID]) -> dict[str, Any]:
        async with factory() as db:
            matrix = await db.get(ExtractionMatrix, matrix_id)
            kwargs = await svc.extraction_task_kwargs(
                db, matrix, document_ids, ids["O"], task_id
            )
        await ems.ExtractionMatrixService().run_background_extraction(
            matrix_id=matrix_id,
            document_ids=document_ids,
            columns=kwargs["columns"],
            task_id=task_id,
            form_version_id=UUID(kwargs["form_version_id"]),
            initiated_by_user_id=UUID(kwargs["initiated_by_user_id"]),
            source_hashes=kwargs["source_hashes"],
        )
        return ems._extraction_status[task_id]

    for task in ("task-1", "task-2"):
        assert (await run(task, [docs[0]]))["completed"] == 1
    calls = llm.calls
    retry = await run("task-1", [docs[0]])
    assert (retry["completed"], retry["failed"], llm.calls) == (1, 0, calls)
    async with factory() as db:
        machine = (await db.execute(select(ExtractionObservation))).scalars().all()
        observed = (
            (
                await db.execute(
                    select(ResearchDecisionEvent).where(
                        ResearchDecisionEvent.event_type == "extraction.observed"
                    )
                )
            )
            .scalars()
            .all()
        )
    assert len(machine) == 4 and {o.kind for o in machine} == {"machine"}
    assert {o.actor_user_id for o in machine} == {ids["O"]}
    assert (
        sorted(str(o.extractor_run_id) for o in machine)
        == ["task-1"] * 2 + ["task-2"] * 2
    )
    for o in machine:
        if o.field_id == field_id:
            assert (o.validation_state, o.value) == ("invalid", "12 participants")
        else:
            assert (o.missingness, o.value) == ("not_reported", None)
    assert [e.actor_role for e in observed] == ["machine", "machine"]

    # 6. The worker never accepts.
    assert await _count(factory, ExtractionAcceptedValue) == 0

    # 7. Two reviewers observe the same cell concurrently; both are kept.
    async def observe(user: str, value: Any, key: str, document: UUID) -> Any:
        async with factory() as db:
            return await routes.create_observation(
                matrix_id,
                ExtractionObservationCreate(
                    document_id=document,
                    field_id=field_id,
                    form_version_id=v2.id,
                    value=value,
                    idempotency_key=key,
                ),
                current_user=_user(world, user),
                db=db,
            )

    r1, r2 = await asyncio.gather(
        observe("R", "12", "r1", docs[0]), observe("R2", 14, "r2", docs[0])
    )
    assert (r1.value, r2.value, r1.kind) == (12, 14, "human")

    async def listing(user: str) -> Any:
        async with factory() as db:
            return await routes.list_cell_observations(
                matrix_id, docs[0], field_id, current_user=_user(world, user), db=db
            )

    assert {o.id for o in (await listing("V")).observations} >= {r1.id, r2.id}

    # 8. Accept.
    async def accept(user: str, key: str, supersedes: UUID | None, **body: Any) -> Any:
        async with factory() as db:
            return await routes.accept_extraction_value(
                matrix_id,
                ExtractionAcceptCreate(
                    document_id=body.pop("document", docs[0]),
                    field_id=field_id,
                    form_version_id=v2.id,
                    observation_ids=body.pop("cited", [r1.id, r2.id]),
                    rationale="R1 matches the methods section",
                    supersedes_accepted_value_id=supersedes,
                    idempotency_key=key,
                    # GOO-305: these observations cite nothing (unverified).
                    **({"value": 12, "accept_unverified": True} | body),
                ),
                current_user=_user(world, user),
                db=db,
            )

    await _status(422, accept("A", "a0", None, value=13))
    first = await accept("A", "a1", None)
    assert (first.form_version_id, first.accepted_by_id) == (v2.id, ids["A"])
    assert first.observation_ids == [r1.id, r2.id]
    stale = await _status(409, accept("A", "a2", None))
    assert stale == svc.ACCEPTED_STALE
    raced = await asyncio.gather(
        accept("A", "a3", first.id, value=None, missingness="unresolved_disagreement"),
        accept("A", "a4", first.id, value=14),
        return_exceptions=True,
    )
    winners = [r for r in raced if not isinstance(r, BaseException)]
    losers = [r for r in raced if isinstance(r, HTTPException)]
    assert len(winners) == 1 and [r.status_code for r in losers] == [409]
    tip = winners[0]
    async with factory() as db:
        assert (await db.execute(text("SELECT 1"))).scalar() == 1
    await _status(403, accept("R", "a5", tip.id))
    # The tip check alone guards this: a supersedes naming ANOTHER cell's tip
    # passes both unique indexes, so without the check it would fork the chain.
    other = await observe("R", "14", "r-doc2", docs[1])
    wrong = accept("A", "a6", tip.id, document=docs[1], cited=[other.id], value=14)
    assert await _status(409, wrong) == svc.ACCEPTED_STALE

    # 9. Stale: a unit change makes v3; the accepted row is untouched.
    async with factory() as db:
        before = (await db.get(ExtractionAcceptedValue, tip.id)).__dict__.copy()
    cols3 = [{"name": "Sample size", "type": "number", "unit": "participants"}]
    await _patch(factory, world, matrix_id, cols3 + [{"name": "Design"}])
    async with factory() as db:
        after = (await db.get(ExtractionAcceptedValue, tip.id)).__dict__.copy()
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
    columns = [c.key for c in ExtractionAcceptedValue.__table__.columns]
    assert [before[c] for c in columns] == [after[c] for c in columns]
    assert [e.payload["accepted_value_ids"] for e in staled] == [[str(tip.id)]]
    _, cells = await _view(factory, matrix_id, docs)
    cell = next(
        c
        for c in cells
        if c["document_id"] == str(docs[0]) and c["column_name"] == "Sample size"
    )
    assert (cell["source"], cell["stale"]) == ("accepted", True)
    v3 = (await _versions(factory, matrix_id))[-1]
    assert v3.version_no == 3

    # 10. Tenancy and lifecycle.
    for call in (
        listing("F"),
        observe("F", "12", "f1", docs[0]),
        accept("F", "f2", tip.id),
    ):
        await _status(404, call)
    async with factory() as db:  # doc2 leaves the project
        await db.execute(
            CollectionDocument.__table__.update()
            .where(CollectionDocument.document_id == docs[1])
            .values(is_deleted=True)
        )
        await db.commit()
    _, cells = await _view(factory, matrix_id, docs)
    assert {c["document_id"] for c in cells} == {str(docs[0])}
    await _status(404, observe("R", "12", "doc2", docs[1]))

    rows = await _count(factory, ExtractionObservation)
    async with factory() as db:  # archive the project
        collection = await db.get(Collection, world.collection)
        status = cast(Any, collection).research_status
        cast(Any, collection).research_status = "archived"
        await db.commit()
    await _status(409, observe("R", "12", "arch1", docs[0]))
    await _status(409, accept("A", "arch2", tip.id))
    archived = await run("task-archived", [docs[0]])
    assert (archived["completed"], archived["failed"]) == (0, 1)
    assert await _count(factory, ExtractionObservation) == rows
    async with factory() as db:
        collection = await db.get(Collection, world.collection)
        cast(Any, collection).research_status = status
        await db.commit()

    async with factory() as db:  # soft-delete the matrix (as DELETE does)
        matrix = await db.get(ExtractionMatrix, matrix_id)
        cast(Any, matrix).soft_delete()
        await db.commit()
    for call in (
        listing("V"),
        observe("R", "12", "del1", docs[0]),
        accept("A", "del2", tip.id),
    ):
        await _status(404, call)
    ems._extraction_status.pop("task-deleted", None)
    await ems.ExtractionMatrixService().run_background_extraction(
        matrix_id,
        [docs[0]],
        [],
        "task-deleted",
        form_version_id=v3.id,
        initiated_by_user_id=ids["O"],
        source_hashes={},
    )
    assert ems._extraction_status["task-deleted"]["skipped"] == 1
    assert await _count(factory, ExtractionObservation) == rows

    # 11. Replay: the whole stream validates and is contiguous.
    async with factory() as db:
        events = await replay_decisions(
            db,
            collection_id=world.collection,
            aggregate_type="research_extraction",
            aggregate_id=matrix_id,
        )
    assert [e.seq for e in events] == list(range(1, len(events) + 1))
    assert [str(e.event_type) for e in events].count("extraction.accepted") == 2

    # 12. Downgrade: new tables go, legacy storage and the v3 mirror stay.
    await _migration(factory, "downgrade")
    async with factory() as db:
        tables = set(
            (
                await db.execute(
                    text(
                        "SELECT tablename FROM pg_tables"
                        " WHERE schemaname = current_schema()"
                    )
                )
            )
            .scalars()
            .all()
        )
        mirror = (
            await db.execute(
                text("SELECT columns FROM extraction_matrices WHERE id = :id"),
                {"id": matrix_id},
            )
        ).scalar()
    assert not tables & {
        "extraction_form_versions",
        "extraction_observations",
        "extraction_accepted_values",
    }
    assert await _cells(factory) == cells_before
    assert mirror == [
        {"name": "Sample size", "description": None},
        {"name": "Design", "description": None},
    ]
