"""Real PostgreSQL proof for the GOO-308 audit bundle and journey.

Runs on GOO-301's ``screening_factory`` schema (migration chain ending at
``d7f9b1c3e5a8``). Seed: owner O (workspace owner, supervisor), reviewers R
(R1 in the plan) and R2, adjudicator A (J), role-less viewer V, foreign-org
F, plus a same-org user P who sees the workspace only because it is public.
One test, eight steps, in the plan's order except that the blind-review
comparison (step 8's second half) is captured during seeding, where a
one-sided submission exists.

Mutation verification (docs/engineering/testing.md), full record in
``docs/testing/agent-orchestration-mutation-checks.md`` (GOO-308 section):

- the ``SET TRANSACTION ISOLATION LEVEL REPEATABLE READ`` line in
  ``journey.begin_read_snapshot`` deleted: step 7's manifest and
  ``prisma-flow.json`` ``stream_heads`` differ;
- ``journey._select_facts`` counting ``screening_observations``: R's journey
  changes after R2 submits (the blind-review guard);
- GOO-300's restricted ``raw`` strip skipped: the import sentinel appears in
  ``corpus.json`` (step 4).

Command (from ``backend/``): ``RESEARCH_DECISION_DATABASE_URL=... pytest -q
tests/integration/test_audit_bundle_postgres.py``.
"""

import hashlib
import io
import json
import zipfile
from types import SimpleNamespace
from typing import Any, TypeAlias, cast
from uuid import UUID, uuid4

import pytest
from fastapi import HTTPException
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from src.api.research_engine import journey as routes
from src.models.collection import CollectionDocument
from src.models.document import Document, DocumentType
from src.models.extraction_matrix import ExtractionMatrix
from src.models.generated_draft import GeneratedDraft
from src.models.research_protocol import ResearchProtocolVersion
from src.models.user import User
from src.schemas.research_engine import ImportDeclaration, ScreeningAdjudicateRequest
from src.services.research import claims_service
from src.services.research import extraction_forms_service as forms
from src.services.research_engine import audit_bundle, corpus_service, screening_service
from src.services.research_engine.audit_bundle import verify_bundle
from src.services.research_engine.project_access import ResearchAction, resolve_project
from src.services.research_engine.protocol_service import (
    canonical_hash,
    protocol_content,
)
from src.services.research_engine.run_conformance import effective_plan_hash
from tests.integration.test_acquisition_prisma_postgres import (
    _attempt,
    _attempt_body,
    _raw_counts,
    _request,
)
from tests.integration.test_draft_release_postgres import (
    _accept,
    _assess,
    _claim,
    _link,
    _promote,
)
from tests.integration.test_screening_queue_postgres import (  # noqa: F401
    _act,
    _assign,
    _body,
    _create_queue,
    _submit,
    _world,
    _World,
    screening_factory,
)

pytestmark = [pytest.mark.integration, pytest.mark.requires_postgres]

Factory: TypeAlias = async_sessionmaker[AsyncSession]
RESTRICTED = "RESTRICTED-7f3a"
FULLTEXT = "FULLTEXT-91c2"
SAMPLE = "We enrolled 412 participants."
D_TEXT = f"Methods. {SAMPLE} Appendix {FULLTEXT} follows."
S1 = "The trial enrolled 412 participants."
V1 = f"## Results\n{S1}\n"


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _members(data: bytes) -> dict[str, bytes]:
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        return {name: archive.read(name) for name in archive.namelist()}


async def _bundle(factory: Factory, project: UUID, user: UUID) -> bytes:
    async with factory() as db:
        response = await routes.audit_bundle_route(
            project, current_user=cast(User, SimpleNamespace(id=user)), db=db
        )
        assert response.headers["content-disposition"].startswith(
            f'attachment; filename="audit-{project}-'
        )
        return cast(bytes, response.body)


async def _journey(factory: Factory, project: UUID, user: UUID) -> str:
    async with factory() as db:
        response = await routes.journey_route(
            project, current_user=cast(User, SimpleNamespace(id=user)), db=db
        )
        return cast(str, response.model_dump_json())


async def _denied(factory: Factory, project: UUID, user: UUID) -> int:
    with pytest.raises(HTTPException) as error:
        await _bundle(factory, project, user)
    return cast(int, error.value.status_code)


async def _scalar(factory: Factory, sql: str, **params: Any) -> Any:
    async with factory() as db:
        return (await db.execute(text(sql), params)).scalar_one()


async def _table_counts(factory: Factory) -> dict[str, Any]:
    """Every table's row count plus every stream's next_seq."""
    async with factory() as db:
        tables = (
            await db.execute(text("""SELECT table_name FROM information_schema.tables
                    WHERE table_schema = current_schema()
                      AND table_type = 'BASE TABLE'"""))
        ).scalars()
        counts: dict[str, Any] = {}
        for table in sorted(tables):
            counts[table] = (
                await db.execute(text(f'SELECT count(*) FROM "{table}"'))
            ).scalar_one()
        counts["next_seq"] = sorted(
            (
                await db.execute(
                    text("SELECT id::text, next_seq FROM research_decision_streams")
                )
            ).all()
        )
        return counts


async def _document(factory: Factory, world: _World) -> UUID:
    async with factory() as db:
        document = Document(
            title="d1",
            filename="d1.pdf",
            file_path="local:///d1.pdf",
            file_size_bytes=1,
            mime_type="application/pdf",
            document_type=DocumentType.PDF,
            organization_id=world.ids["org"],
            checksum_sha256=_sha(D_TEXT.encode()),
            content_text=D_TEXT,
        )
        db.add(document)
        await db.flush()
        db.add(
            CollectionDocument(collection_id=world.collection, document_id=document.id)
        )
        await db.commit()
        return cast(UUID, document.id)


async def _complete_run(factory: Factory, world: _World) -> None:
    """The fixture run becomes a conformant completed run with receipts."""
    async with factory() as db:
        version = await db.get(ResearchProtocolVersion, world.version_id)
        assert version is not None
        await db.execute(
            text("""UPDATE research_runs SET status = 'completed',
                    conformance_status = 'plan_verified',
                    protocol_version_id = :v, effective_plan_hash = :h,
                    reproducibility_manifest = CAST(:m AS jsonb)
                    WHERE id = :id"""),
            {
                "id": world.ids["run"],
                "v": world.version_id,
                "h": effective_plan_hash(version),
                "m": json.dumps(
                    {
                        "_search_receipts_v1": {"strategies": {}, "executions": {}},
                        "_pause_requested": True,
                    }
                ),
            },
        )
        await db.commit()


async def _adjudicate(
    factory: Factory, world: _World, queue: Any, conflict: Any, key: str
) -> None:
    await _act(
        factory,
        world,
        "A",
        ResearchAction.ADJUDICATE,
        lambda db, ctx: screening_service.adjudicate(
            db,
            ctx,
            queue.id,
            conflict.report_id,
            world.ids["A"],
            ScreeningAdjudicateRequest(
                resolution_id=conflict.id,
                input_observation_ids=conflict.input_observation_ids,
                criteria_hash=queue.criteria_hash,
                decision="exclude",
                rationale="off topic",
                idempotency_key=key,
            ),
        ),
    )


async def _add_public_viewer(factory: Factory, world: _World) -> UUID:
    user_id = uuid4()
    async with factory() as db:
        db.add(
            User(
                id=user_id,
                email=f"{user_id}@test.invalid",
                password_hash="unused",
                first_name="Test",
                last_name="P",
                organization_id=world.ids["org"],
            )
        )
        await db.execute(
            text("UPDATE workspaces SET is_public = true WHERE id = :w"),
            {"w": world.ids["workspace"]},
        )
        await db.commit()
    return user_id


@pytest.mark.asyncio
async def test_audit_bundle_matches_rows_scope_and_snapshot(
    screening_factory: Factory, monkeypatch: pytest.MonkeyPatch
) -> None:
    factory = screening_factory
    # 1. Seed.
    world = await _world(factory)
    ids, p = world.ids, world.collection
    r1, r2, r3 = world.reports
    ris = (
        b"TY  - JOUR\nTI  - Imported paper\nDO  - 10.1000/imported\n"
        + f"N1  - {RESTRICTED}\n".encode()
        + b"ER  - \n"
    )
    receipt, _ = await _act(
        factory,
        world,
        "O",
        ResearchAction.EDIT,
        lambda db, ctx: corpus_service.import_file(
            db,
            ctx,
            ids["O"],
            ImportDeclaration(database="Embase"),  # restricted by default
            fmt="ris",
            filename="embase.ris",
            data=ris,
        ),
    )
    r4 = UUID(
        str(
            await _scalar(
                factory,
                "SELECT report_id FROM research_import_records WHERE receipt_id = :r",
                r=receipt.id,
            )
        )
    )
    await _complete_run(factory, world)
    d1 = await _document(factory, world)

    queue = await _create_queue(factory, world, "ta")
    mine = await _assign(factory, world, queue.id, "R")
    peer = await _assign(factory, world, queue.id, "R2")

    async def both(report: UUID, first: str, second: str) -> Any:
        await _submit(
            factory,
            world,
            "R",
            queue,
            _body(queue, mine, report, f"{report}-r", decision=first),
        )
        return (
            await _submit(
                factory,
                world,
                "R2",
                queue,
                _body(queue, peer, report, f"{report}-r2", decision=second),
            )
        ).resolution

    # Blind review: R's journey is identical before and after R2's
    # unrevealed submission on a report R has not screened.
    await _submit(factory, world, "R", queue, _body(queue, mine, r1, "r1-r"))
    blind_before = await _journey(factory, p, ids["R"])
    await _submit(factory, world, "R2", queue, _body(queue, peer, r3, "r3-r2"))
    blind_after = await _journey(factory, p, ids["R"])
    await _submit(factory, world, "R2", queue, _body(queue, peer, r1, "r1-r2"))
    await _submit(factory, world, "R", queue, _body(queue, mine, r3, "r3-r"))
    conflict = await both(r2, "include", "exclude")
    assert conflict.basis == "conflict"
    await _adjudicate(factory, world, queue, conflict, "adj-r2")
    open_conflict = await both(r4, "exclude", "include")
    assert open_conflict.basis == "conflict"  # adjudicated in step 7

    state, _ = await _request(factory, world, r3, "rq-r3")
    await _attempt(
        factory, world, state.request_id, _attempt_body("unavailable", "ua-r3")
    )

    columns = [{"name": "Sample size"}]
    async with factory() as db:
        context = await resolve_project(db, p, ids["O"], ResearchAction.EDIT)
        matrix = ExtractionMatrix(project_id=p, name="journey", columns=columns)
        db.add(matrix)
        version = await forms.create_version(db, context, matrix, columns, ids["O"])
        matrix_id, version_id = cast(UUID, matrix.id), cast(UUID, version.id)
        field_id = UUID(version.fields[0]["field_id"])
        draft = GeneratedDraft(
            project_id=p, version=1, title="v1", content=V1, is_current=True
        )
        db.add(draft)
        await db.commit()
        draft_id = cast(UUID, draft.id)
    w = SimpleNamespace(
        factory=factory,
        ids=ids,
        p1=p,
        matrix=matrix_id,
        form_version=version_id,
        fields={"Sample size": field_id},
    )
    accepted, _ = await _accept(w, "Sample size", d1, "412", SAMPLE, "s")
    claim = await _claim(w, draft_id, V1, S1, "c1")
    link = await _link(w, claim, "l1", kind="extraction", accepted_value_id=accepted)
    await _assess(w, claim, "a1", [link])
    release, _ = await _promote(w, "A", draft_id, 1, "promote", _sha(V1.encode()))

    # 2. Download as O: it opens, verifies, and every SHA256SUMS line holds.
    before = await _table_counts(factory)
    data = await _bundle(factory, p, ids["O"])
    verified = verify_bundle(data)
    members = _members(data)
    for line in members["SHA256SUMS"].decode().splitlines():
        digest, path = line.split("  ", 1)
        assert _sha(members[path]) == digest
    manifest = json.loads(members["manifest.json"])
    assert verified["manifest_sha256"] == _sha(members["manifest.json"])
    statuses = {part["path"]: part["status"] for part in manifest["parts"]}
    # GOO-309/310/311/312: no appraisal, evidence table, synthesis or
    # experiment in this world, so those parts are present but empty.
    assert statuses.pop("appraisal.json") == "empty"
    assert statuses.pop("evidence.json") == "empty"
    assert statuses.pop("synthesis.json") == "empty"
    assert statuses.pop("experiments.json") == "empty"
    assert set(statuses.values()) == {"ok"}

    # 3. Hashes match rows.
    source = members[f"drafts/{draft_id}-v1.source.md"]
    claims_body = json.loads(members["claims.json"])["body"]
    release_hash = await _scalar(
        factory, "SELECT content_hash FROM draft_releases WHERE id = :r", r=release.id
    )
    assert _sha(source) == release_hash == claims_body["drafts"][0]["content_hash"]
    assert claims_body["stream_head"] == await _scalar(
        factory,
        """SELECT next_seq - 1 FROM research_decision_streams
           WHERE aggregate_type = :t AND aggregate_id = :c""",
        t=claims_service.AGGREGATE_TYPE,
        c=p,
    )
    flow = json.loads(members["prisma-flow.json"])["body"]
    raw = await _raw_counts(factory, p)
    assert {k: flow["counts"][k] for k in raw} == raw
    assert raw["reports_not_retrieved"] == 1 and raw["records_excluded"] == 1
    methods = json.loads(members["methods.json"])["body"]
    [protocol] = methods["protocol_versions"]
    assert (
        canonical_hash(
            protocol_content(
                UUID(protocol["question_version_id"]),
                UUID(protocol["blueprint_id"]),
                protocol["snapshot"],
                protocol["execution_plan"],
            )
        )
        == protocol["content_hash"]
    )
    [run] = methods["runs"]
    assert "_pause_requested" not in run["reproducibility_manifest"]
    assert run["effective_plan_hash"] == canonical_hash(
        {
            "protocol_content_hash": protocol["content_hash"],
            "execution_plan": protocol["execution_plan"],
        }
    )
    [extraction] = json.loads(members["extraction.json"])["body"]
    [tip] = extraction["accepted_tips"]
    assert tip["id"] == str(accepted) and tip["stale"] is False
    assert (
        f"VERIFIED release {release.id}" in members[f"drafts/{draft_id}-v1.md"].decode()
    )

    # 4. Restricted content absent from every member.
    for path, member in members.items():
        assert RESTRICTED.encode() not in member, path
        assert FULLTEXT.encode() not in member, path

    # 6. Zero writes; a second download has identical body hashes.
    second = await _bundle(factory, p, ids["O"])
    assert await _table_counts(factory) == before
    body_hashes = {e["path"]: e["body_sha256"] for e in manifest["parts"]}
    second_manifest = json.loads(_members(second)["manifest.json"])
    assert {
        e["path"]: e["body_sha256"] for e in second_manifest["parts"]
    } == body_hashes

    journey = json.loads(await _journey(factory, p, ids["O"]))
    assert {s["key"]: s["status"] for s in journey["stages"]}["select"] == "attention"

    # 7. Snapshot: a commit between two parts changes neither PRISMA nor heads.
    real_claims = audit_bundle._claims

    async def claims_after_commit(db: AsyncSession, context: Any) -> Any:
        await _adjudicate(factory, world, queue, open_conflict, "adj-r4")
        return await real_claims(db, context)

    monkeypatch.setattr(audit_bundle, "_claims", claims_after_commit)
    racing = _members(await _bundle(factory, p, ids["O"]))
    monkeypatch.setattr(audit_bundle, "_claims", real_claims)
    racing_manifest = json.loads(racing["manifest.json"])
    racing_flow = json.loads(racing["prisma-flow.json"])["body"]
    assert racing_manifest["stream_heads"] == racing_flow["versions"]["stream_heads"]
    assert racing_manifest["stream_heads"] == manifest["stream_heads"]
    assert racing_flow["counts"] == flow["counts"]  # the new resolution is absent
    after = json.loads(_members(await _bundle(factory, p, ids["O"]))["manifest.json"])
    assert after["stream_heads"] != manifest["stream_heads"]  # it did commit

    # 8. Journey rides along: Plan through Write complete, and blind review.
    journey = json.loads(await _journey(factory, p, ids["O"]))
    assert {s["key"]: s["status"] for s in journey["stages"]} == {
        k: "complete" for k in ("plan", "discover", "select", "extract", "write")
    }, journey
    assert journey["current"] is None
    assert blind_after == blind_before

    # 5. Scope: foreign and public-only 404, archived 200, deleted 404.
    public_only = await _add_public_viewer(factory, world)
    assert await _denied(factory, p, ids["F"]) == 404
    assert await _denied(factory, p, public_only) == 404
    async with factory() as db:
        await db.execute(
            text("UPDATE collections SET research_status = 'archived' WHERE id = :c"),
            {"c": p},
        )
        await db.commit()
    verify_bundle(await _bundle(factory, p, ids["O"]))
    async with factory() as db:
        await db.execute(
            text("UPDATE collections SET is_deleted = true WHERE id = :c"), {"c": p}
        )
        await db.commit()
    assert await _denied(factory, p, ids["O"]) == 404
