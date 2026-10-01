"""Real PostgreSQL proof for GOO-309 blind appraisal, adjudication and staleness.

Runs on GOO-301's ``screening_factory`` schema, whose migration chain now
ends at ``e2a4c6b8d0f1``: ``appraisal_assessments``, its partial unique
indexes, CHECKs and insert-only trigger come from the migration. GOO-299's
seed gives owner O (no roles), reviewer R (reviewer A in the plan),
adjudicator A (J in the plan), role-less viewer V and foreign-org F; this file
adds reviewer B and J2, who holds both ADJUDICATOR and REVIEWER.

Mutation verification (docs/engineering/testing.md), full record in
``docs/testing/agent-orchestration-mutation-checks.md`` (GOO-309 section):

- ``_visible`` returning every row: step 4, B sees A's row;
- the revealed-edit refusal in ``_write`` skipped: step 5, A's post-reveal
  successor is recorded;
- the self-adjudication check skipped: step 7, J2 adjudicates R3;
- the ``resolves_assessment_ids == tips`` check skipped: step 6, the stale
  adjudication is recorded;
- the insert-only trigger function returning NEW: step 9, the UPDATE succeeds;
- ``graph_part`` adding an edge from every accepted value: step 10, A's R3
  row is stale.

Command (from ``backend/``): ``RESEARCH_DECISION_DATABASE_URL=... pytest -q
tests/integration/test_appraisal_postgres.py``.
"""

import asyncio
import importlib.util
import io
import json
import zipfile
from datetime import date
from types import SimpleNamespace
from typing import Any, Awaitable, Callable, TypeAlias, TypeVar, cast
from uuid import UUID, uuid4

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from fastapi import HTTPException
from sqlalchemy import select, text
from sqlalchemy.engine import Connection
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from src.api.research import extraction_matrix as matrix_routes
from src.api.research_engine import journey as journey_routes
from src.models.collection import CollectionDocument
from src.models.extraction_matrix import ExtractionMatrix
from src.models.research_project_role import (
    ResearchProjectRole,
    ResearchProjectRoleAssignment,
)
from src.models.research_report import ResearchReport, ResearchStudy
from src.models.research_run import ResearchRun
from src.models.research_step import ResearchStep
from src.models.user import User
from src.models.workspace import WorkspaceMember, WorkspaceRole
from src.schemas.research_engine import (
    AppraisalAdjudicate,
    AppraisalSubmit,
    FulltextAttemptCreate,
    FulltextRequestCreate,
)
from src.services.research import extraction_forms_service as forms
from src.services.research_decisions import replay_decisions
from src.services.research_engine import acquisition_service, appraisal_rules
from src.services.research_engine import appraisal_service as svc
from src.services.research_engine.project_access import (
    ProjectContext,
    ResearchAction,
    resolve_project,
)
from src.shared.scispace_schemas import ExtractionAcceptCreate
from tests.integration.research_engine_postgres_support import (
    _PROTOCOL_SNAPSHOT,
    seed_approved_protocol_binding,
)
from tests.integration.test_draft_release_postgres import _accept, _document
from tests.integration.test_report_identity_postgres import _seed, _wait_until_blocked
from tests.integration.test_screening_queue_postgres import (  # noqa: F401
    VERSIONS,
    screening_factory,
)

pytestmark = [pytest.mark.integration, pytest.mark.requires_postgres]

Factory: TypeAlias = async_sessionmaker[AsyncSession]
T = TypeVar("T")
VIEW, REVIEW, ADJUDICATE, EDIT = (
    ResearchAction.VIEW,
    ResearchAction.REVIEW,
    ResearchAction.ADJUDICATE,
    ResearchAction.EDIT,
)
RCT = "randomized_parallel_group"
OUTCOME, TIMEPOINT = "depressive_symptoms", "12 weeks"
D1_TEXT = (
    "Methods. Outcome assessors were not blinded to allocation. "
    "Depressive symptoms were measured at 12 weeks."
)
D3_TEXT = "Design. Single-arm cohort. Outcome assessors were masked."
BLIND_Q = "Outcome assessors were not blinded to allocation."
MASKED_Q = "Outcome assessors were masked."
SNAPSHOT = {
    **_PROTOCOL_SNAPSHOT,
    "appraisal_synthesis": {
        "method": "narrative",
        "appraisal": {
            "instrument": "rob2",
            "version": "2019-08-22",
            "mode": "dual_independent",
        },
    },
    "outcomes": {
        "declared": [
            {"key": OUTCOME, "label": "Depressive symptoms", "timepoints": [TIMEPOINT]}
        ]
    },
    "reviewer_mode": {"mode": "dual_independent"},
}
# Plan names -> GOO-299 seed keys.
REV_A, REV_B, J, J2 = "R", "B", "A", "J2"


def _user(user_id: UUID) -> Any:
    return SimpleNamespace(id=user_id)


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


async def _committed(
    w: Any,
    user: str,
    action: ResearchAction,
    call: Callable[[AsyncSession, ProjectContext], Awaitable[T]],
) -> T:
    """For services that leave the commit to the route (GOO-303)."""
    async with w.factory() as db:
        context = await resolve_project(db, w.p1, w.ids[user], action)
        result = await call(db, context)
        await db.commit()
        return result


async def _refused(awaitable: Awaitable[Any], status: int, detail: str = "") -> None:
    with pytest.raises(HTTPException) as error:
        await awaitable
    assert error.value.status_code == status, error.value.detail
    assert detail in str(error.value.detail), error.value.detail


async def _scalar(factory: Factory, sql: str, **params: Any) -> Any:
    async with factory() as db:
        return (await db.execute(text(sql), params)).scalar_one()


async def _add_user(
    factory: Factory, ids: dict[str, UUID], key: str, *roles: ResearchProjectRole
) -> None:
    ids[key] = uuid4()
    async with factory() as db:
        db.add(
            User(
                id=ids[key],
                email=f"{ids[key]}@test.invalid",
                password_hash="unused",
                first_name="Test",
                last_name=key,
                organization_id=ids["org"],
            )
        )
        await db.flush()
        db.add(
            WorkspaceMember(
                workspace_id=ids["workspace"],
                user_id=ids[key],
                role=WorkspaceRole.VIEWER,
            )
        )
        for role in roles:
            db.add(
                ResearchProjectRoleAssignment(
                    collection_id=ids["collection"],
                    user_id=ids[key],
                    role=role,
                    assigned_by_id=ids["O"],
                )
            )
        await db.commit()


async def _retrieve(w: Any, report: UUID, document: UUID) -> None:
    """GOO-303: record ``document`` as ``report``'s retrieved full text."""
    state, _ = await _committed(
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
    await _committed(
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
                document_id=document,
                idempotency_key=f"rt-{report}",
            ),
        ),
    )


async def _setup(factory: Factory) -> Any:
    ids = await _seed(factory)
    await _add_user(factory, ids, REV_B, ResearchProjectRole.REVIEWER)
    await _add_user(
        factory,
        ids,
        J2,
        ResearchProjectRole.ADJUDICATOR,
        ResearchProjectRole.REVIEWER,
    )
    p1 = ids["collection"]
    async with factory() as db:
        run = await db.get(ResearchRun, ids["run"])
        assert run is not None
        binding = await seed_approved_protocol_binding(
            db,
            blueprint_id=cast(UUID, run.blueprint_id),
            collection_id=p1,
            author_id=ids["O"],
            steps=[],
            parameters={},
            snapshot=SNAPSHOT,
        )
        study = ResearchStudy(collection_id=p1, label="S")
        db.add(study)
        await db.flush()
        reports = {}
        for name, study_id, status in (
            ("R1", study.id, "confirmed"),
            ("R2", study.id, "confirmed"),
            ("R3", None, None),
            ("R4", study.id, "proposed"),
        ):
            report = ResearchReport(
                collection_id=p1,
                title_snapshot=name,
                study_id=study_id,
                study_link_status=status,
            )
            db.add(report)
            await db.flush()
            reports[name] = cast(UUID, report.id)
        d1 = await _document(db, ids["org"], "d1", D1_TEXT)
        d3 = await _document(db, ids["org"], "d3", D3_TEXT)
        for document in (d1, d3):
            db.add(CollectionDocument(collection_id=p1, document_id=document))
        await db.commit()
    w = SimpleNamespace(
        factory=factory,
        ids=ids,
        p1=p1,
        version=binding.protocol_version_id,
        study=cast(UUID, study.id),
        d1=d1,
        d3=d3,
        **reports,
    )
    await _retrieve(w, w.R1, d1)
    await _retrieve(w, w.R3, d3)
    columns = [{"name": "Blinding"}]
    async with factory() as db:
        context = await resolve_project(db, p1, ids["O"], EDIT)
        matrix = ExtractionMatrix(project_id=p1, name="appraisal", columns=columns)
        db.add(matrix)
        version = await forms.create_version(db, context, matrix, columns, ids["O"])
        w.fields = {f["name"]: UUID(f["field_id"]) for f in version.fields}
        w.matrix, w.form_version = cast(UUID, matrix.id), cast(UUID, version.id)
    w.blinding, w.blinding_obs = await _accept(
        w, "Blinding", d1, "not blinded", BLIND_Q, "b1"
    )
    w.masked, _ = await _accept(w, "Blinding", d3, "masked", MASKED_Q, "b3")
    return w


def _domains(
    evidence: dict[str, list[dict[str, Any]]] | None = None,
    **judgments: str | None,
) -> dict[str, Any]:
    domains = {}
    for domain_id in appraisal_rules.SPEC["domains"]:
        judgment = judgments.get(domain_id, "low")
        domains[domain_id] = {
            "judgment": judgment,
            "rationale": None if judgment is None else f"{domain_id} judged {judgment}",
            "evidence": (evidence or {}).get(domain_id, []),
        }
    return domains


def _ref(kind: str, value: UUID) -> dict[str, Any]:
    return {"kind": kind, "id": str(value)}


def _body(w: Any, key: str, **fields: Any) -> AppraisalSubmit:
    target = fields.pop("target", {"study_id": w.study})
    values = {
        "protocol_version_id": w.version,
        "instrument_key": "rob2",
        "instrument_version": "2019-08-22",
        "outcome_key": OUTCOME,
        "timepoint": TIMEPOINT,
        "study_design": RCT,
        "applicability": "applicable",
        "domains": _domains(),
        "overall": None,
        "idempotency_key": key,
        **target,
        **fields,
    }
    return cast(AppraisalSubmit, AppraisalSubmit.model_validate(values))


def _submit(w: Any, user: str, body: AppraisalSubmit) -> Awaitable[Any]:
    return _as(w, user, REVIEW, lambda db, ctx: svc.submit(db, ctx, w.ids[user], body))


def _adjudicate(w: Any, user: str, body: AppraisalAdjudicate) -> Awaitable[Any]:
    return _as(
        w,
        user,
        ADJUDICATE,
        lambda db, ctx: svc.adjudicate(db, ctx, w.ids[user], body),
    )


def _adjudication(
    w: Any, key: str, resolves: list[UUID], **fields: Any
) -> AppraisalAdjudicate:
    body = _body(w, key, **fields)
    return cast(
        AppraisalAdjudicate,
        AppraisalAdjudicate.model_validate(
            {
                **body.model_dump(),
                "resolves_assessment_ids": resolves,
                "rationale": "Adjudicated against the full text.",
            }
        ),
    )


async def _list(w: Any, user: str) -> dict[tuple[str, str, str], Any]:
    listing = await _as(
        w, user, VIEW, lambda db, ctx: svc.list_appraisals(db, ctx, w.ids[user])
    )
    return {(r.target_key, r.outcome_key, r.timepoint): r for r in listing.results}


_APPRAISAL_MIGRATION = "e2a4c6b8d0f1_create_appraisal_assessments.py"
# GOO-310's triggers depend on the function this migration owns: step down
# through it first and back up after.
_EVIDENCE_MIGRATION = "f4b6d8a0c2e3_create_evidence_certainty.py"
# GOO-311's synthesis results reference GOO-310's tables and use the function.
_SYNTHESIS_MIGRATION = "a6c8e0b2d4f5_create_synthesis_results.py"


def _migration(connection: Connection, direction: str, filename: str) -> None:
    spec = importlib.util.spec_from_file_location(filename[:-3], VERSIONS / filename)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    setattr(module, "op", Operations(MigrationContext.configure(connection)))
    cast(Callable[[], None], getattr(module, direction))()


async def _run_migration(factory: Factory, direction: str) -> None:
    order = [_SYNTHESIS_MIGRATION, _EVIDENCE_MIGRATION, _APPRAISAL_MIGRATION]
    if direction == "upgrade":
        order.reverse()
    async with factory() as db:
        connection = await db.connection()
        for filename in order:
            await connection.run_sync(
                lambda sync, name=filename: _migration(sync, direction, name)
            )
        await db.commit()


async def _bundle_appraisal(w: Any, user: str) -> dict[str, Any]:
    async with w.factory() as db:
        response = await journey_routes.audit_bundle_route(
            w.p1, current_user=cast(User, _user(w.ids[user])), db=db
        )
    with zipfile.ZipFile(io.BytesIO(cast(bytes, response.body))) as archive:
        return cast(dict[str, Any], json.loads(archive.read("appraisal.json")))


async def test_appraisal_independence_adjudication_and_staleness(
    screening_factory: Factory,
) -> None:
    factory = screening_factory
    # 1. Seed.
    w = await _setup(factory)
    s_key = (f"study:{w.study}", OUTCOME, TIMEPOINT)
    r3_key = (f"report:{w.R3}", OUTCOME, TIMEPOINT)

    # 2. Legacy quality marks are never backfilled.
    async with factory() as db:
        db.add(
            ResearchStep(
                run_id=w.ids["run"],
                step_index=0,
                step_type="verify",
                quality_marks=[{"check": "retraction", "passed": True}],
            )
        )
        await db.commit()
    await _run_migration(factory, "downgrade")
    await _run_migration(factory, "upgrade")
    assert await _scalar(factory, "SELECT count(*) FROM appraisal_assessments") == 0
    assert (
        await _scalar(
            factory,
            "SELECT count(*) FROM research_steps WHERE quality_marks IS NOT NULL",
        )
        == 1
    )

    # 3. Rejections.
    for user in ("O", "V"):
        await _refused(_submit(w, user, _body(w, f"{user}-try")), 403, "reviewer")
    await _refused(_submit(w, "F", _body(w, "f-try")), 404)
    await _refused(
        _submit(w, REV_A, _body(w, "robins", instrument_key="robins-i")),
        422,
        svc.NOT_PROTOCOL_INSTRUMENT,
    )
    await _refused(
        _submit(w, REV_A, _body(w, "r1", target={"report_id": w.R1})),
        422,
        svc.APPRAISE_STUDY,
    )
    await _refused(
        _submit(w, REV_A, _body(w, "r4", target={"report_id": w.R4})),
        409,
        svc.LINK_UNRESOLVED,
    )
    await _refused(
        _submit(w, REV_A, _body(w, "stale", protocol_version_id=uuid4())),
        409,
        svc.PROTOCOL_STALE,
    )
    foreign_evidence = _domains({"D4": [_ref("accepted_value", w.masked)]})
    await _refused(
        _submit(w, REV_A, _body(w, "d3-ev", domains=foreign_evidence)),
        422,
        svc.EVIDENCE_FOREIGN,
    )
    await _refused(
        _submit(w, REV_A, _body(w, "cohort", study_design="cohort")),
        422,
        "does not apply to cohort",
    )
    assert await _scalar(factory, "SELECT count(*) FROM appraisal_assessments") == 0

    # 4. Blind: A's answers stay hidden from B, B's export and the bundle.
    a_domains = _domains({"D4": [_ref("observation", w.blinding_obs)]}, D3=None)
    a1, replayed = await _submit(w, REV_A, _body(w, "a-1", domains=a_domains))
    assert not replayed and a1.overall is None
    again, replayed = await _submit(w, REV_A, _body(w, "a-1", domains=a_domains))
    assert replayed and again.id == a1.id
    b_view = await _list(w, REV_B)
    assert b_view[s_key].status == "awaiting_independent"
    assert b_view[s_key].rows == [] and not b_view[s_key].mine
    assert str(a1.id) not in json.dumps(
        [r.model_dump(mode="json") for r in b_view.values()]
    )
    a_view = await _list(w, REV_A)
    assert [r.id for r in a_view[s_key].rows] == [a1.id]
    assert a_view[s_key].status == "awaiting_independent"
    assert w.blinding in {o.id for o in a_view[s_key].evidence_options}
    assert w.masked in {o.id for o in a_view[r3_key].evidence_options}
    exported = await _as(
        w, REV_B, VIEW, lambda db, ctx: svc.export_package(db, ctx, w.ids[REV_B])
    )
    assert str(a1.id) not in json.dumps(exported)
    assert str(a1.id) not in json.dumps(await _bundle_appraisal(w, REV_B))

    # 5. Reveal and conflict.
    a2, _ = await _submit(
        w,
        REV_A,
        _body(w, "a-2", domains=a_domains, supersedes_assessment_id=a1.id),
    )
    b_domains = _domains(
        {"D4": [_ref("accepted_value", w.blinding)]}, D3="low", D4="high"
    )
    b1, _ = await _submit(w, REV_B, _body(w, "b-1", domains=b_domains, overall="high"))
    v_view = await _list(w, "V")
    assert v_view[s_key].status == "conflict"
    assert {"D3", "D4"} <= set(v_view[s_key].unresolved_domains)
    assert {r.id for r in v_view[s_key].rows} == {a1.id, a2.id, b1.id}
    assert next(r for r in v_view[s_key].rows if r.id == a1.id).superseded
    await _refused(
        _submit(
            w,
            REV_A,
            _body(w, "a-3", domains=a_domains, supersedes_assessment_id=a2.id),
        ),
        409,
        svc.REVEALED,
    )

    # 6. Adjudication.
    resolved = _domains({"D4": [_ref("accepted_value", w.blinding)]}, D4="high")
    await _refused(
        _as(
            w,
            REV_B,
            ADJUDICATE,
            lambda db, ctx: svc.adjudicate(
                db, ctx, w.ids[REV_B], _adjudication(w, "b-adj", [a2.id, b1.id])
            ),
        ),
        403,
        "adjudicator",
    )
    await _refused(
        _adjudicate(
            w,
            J,
            _adjudication(
                w, "j-stale", [a1.id, b1.id], domains=resolved, overall="high"
            ),
        ),
        409,
        svc.APPRAISAL_STALE,
    )
    j1, _ = await _adjudicate(
        w,
        J,
        _adjudication(w, "j-1", [a2.id, b1.id], domains=resolved, overall="high"),
    )
    s_result = (await _list(w, "V"))[s_key]
    assert s_result.status == "adjudicated" and s_result.unresolved_domains == []
    assert {(r.id, r.assessor_id) for r in s_result.rows} == {
        (a1.id, w.ids[REV_A]),
        (a2.id, w.ids[REV_A]),
        (b1.id, w.ids[REV_B]),
        (j1.id, w.ids[J]),
    }

    # 7. Self-adjudication and not applicable (R3).
    r3 = {"target": {"report_id": w.R3}}
    a_r3, _ = await _submit(
        w,
        REV_A,
        _body(
            w,
            "a-r3",
            study_design="cohort",
            applicability="not_applicable",
            domains={},
            **r3,
        ),
    )
    j2_r3, _ = await _submit(w, J2, _body(w, "j2-r3", **r3))
    assert (await _list(w, "V"))[r3_key].status == "conflict"
    na = {"study_design": "cohort", "applicability": "not_applicable", "domains": {}}
    await _refused(
        _adjudicate(w, J2, _adjudication(w, "j2-adj", [a_r3.id, j2_r3.id], **na, **r3)),
        403,
        svc.SELF_ADJUDICATION,
    )
    j_r3, _ = await _adjudicate(
        w, J, _adjudication(w, "j-r3", [a_r3.id, j2_r3.id], **na, **r3)
    )
    async with factory() as db:
        stored = (
            (
                await db.execute(
                    text("""SELECT domains, overall, kind FROM appraisal_assessments
                            WHERE id = :id"""),
                    {"id": j_r3.id},
                )
            )
            .mappings()
            .one()
        )
    assert dict(stored) == {"domains": {}, "overall": None, "kind": "adjudicated"}
    assert (await _list(w, "V"))[r3_key].status == "adjudicated"

    # 8. Round trip in new sessions.
    async with factory() as db:
        written = {
            row["id"]: row
            for row in (
                await db.execute(text("SELECT * FROM appraisal_assessments"))
            ).mappings()
        }
    listed = await _list(w, "V")
    rows = [r for result in listed.values() for r in result.rows]
    assert {r.id for r in rows} == set(written)
    for row in rows:
        stored_row = written[row.id]
        assert (
            row.instrument_spec_hash,
            row.protocol_version_id,
            row.input_hash,
            row.assessor_id,
        ) == (
            stored_row["instrument_spec_hash"],
            stored_row["protocol_version_id"],
            stored_row["input_hash"],
            stored_row["assessor_id"],
        )
        assert row.instrument_spec_hash == appraisal_rules.SPEC_HASH
    first = await _as(w, "V", VIEW, lambda db, ctx: svc.export_package(db, ctx, None))
    second = await _as(w, "V", VIEW, lambda db, ctx: svc.export_package(db, ctx, None))
    assert first["body_sha256"] == second["body_sha256"]
    exported_rows = {
        r["id"]: r for result in first["body"]["results"] for r in result["rows"]
    }
    assert set(exported_rows) == {str(i) for i in written}
    assert exported_rows[str(j1.id)]["input_hash"] == written[j1.id]["input_hash"]
    bundle = await _bundle_appraisal(w, "V")
    assert bundle["body_sha256"] == first["body_sha256"]

    # 9. Insert-only.
    for statement in (
        "UPDATE appraisal_assessments SET overall = 'low'"
        " WHERE applicability = 'applicable'",
        "DELETE FROM appraisal_assessments",
    ):
        async with factory() as db:
            with pytest.raises(DBAPIError) as refused:
                await db.execute(text(statement))
            assert getattr(refused.value.orig, "sqlstate", None) == "55000"

    # 10. Staleness is selective: only rows citing the superseded value.
    xmin_sql = "SELECT id, xmin::text FROM appraisal_assessments ORDER BY id"
    async with factory() as db:
        before = (await db.execute(text(xmin_sql))).all()
    async with factory() as db:
        await matrix_routes.accept_extraction_value(
            w.matrix,
            ExtractionAcceptCreate(
                document_id=w.d1,
                field_id=w.fields["Blinding"],
                form_version_id=w.form_version,
                observation_ids=[w.blinding_obs],
                value="not blinded",
                rationale="re-accepted with a corrected note",
                supersedes_accepted_value_id=w.blinding,
                idempotency_key="acc-b1-again",
            ),
            current_user=_user(w.ids[J]),
            db=db,
        )
    listed = await _list(w, "V")
    stale = {r.id: r.stale for result in listed.values() for r in result.rows}
    assert stale[j1.id] and stale[b1.id]
    assert not stale[a_r3.id] and not stale[j_r3.id]
    assert not stale[a2.id]  # cites the verified observation; its source is intact
    assert listed[s_key].stale and not listed[r3_key].stale
    async with factory() as db:
        assert (await db.execute(text(xmin_sql))).all() == before

    # 11. Archived: writes 409, reads 200. Deleted workspace: 404.
    async with factory() as db:
        await db.execute(
            text("UPDATE collections SET research_status = 'archived' WHERE id = :p"),
            {"p": w.p1},
        )
        await db.commit()
    await _refused(_submit(w, REV_B, _body(w, "archived", **r3)), 409, "not writable")
    assert s_key in await _list(w, REV_B)
    async with factory() as db:
        await db.execute(
            text("UPDATE workspaces SET is_deleted = true WHERE id = :w"),
            {"w": w.ids["workspace"]},
        )
        await db.commit()
    await _refused(_list(w, REV_B), 404)

    # 13. Replay: contiguous and valid.
    async with factory() as db:
        events = await replay_decisions(
            db,
            collection_id=w.p1,
            aggregate_type="research_appraisal",
            aggregate_id=w.p1,
        )
    assert [e.seq for e in events] == list(range(1, len(events) + 1))
    assert [str(e.event_type) for e in events] == [
        "appraisal.submitted",
        "appraisal.submitted",
        "appraisal.submitted",
        "appraisal.adjudicated",
        "appraisal.submitted",
        "appraisal.submitted",
        "appraisal.adjudicated",
    ]

    # 14. Downgrade (through GOO-310 first) drops the table and the function.
    await _run_migration(factory, "downgrade")
    async with factory() as db:
        tables = set((await db.execute(text("""SELECT tablename FROM pg_tables
                                WHERE schemaname = current_schema()"""))).scalars())
        functions = set((await db.execute(text("""SELECT p.proname FROM pg_proc p
                        JOIN pg_namespace n ON n.oid = p.pronamespace
                        WHERE n.nspname = current_schema()"""))).scalars())
    assert "appraisal_assessments" not in tables
    assert {
        "draft_releases",
        "research_reports",
        "extraction_accepted_values",
    } <= tables
    assert "prevent_research_insert_only_mutation" not in functions


@pytest.mark.parametrize("order", ["submit-first", "revoke-first"])
async def test_appraisal_role_revocation_race(
    screening_factory: Factory, order: str
) -> None:
    """12. The REVIEWER revocation and the submission serialize on the locks."""
    factory = screening_factory
    w = await _setup(factory)
    body = _body(w, "race", target={"report_id": w.R3})

    async def revoke(db: AsyncSession) -> None:
        await resolve_project(db, w.p1, w.ids["O"], ResearchAction.MANAGE)
        role = (
            await db.execute(
                select(ResearchProjectRoleAssignment).where(
                    ResearchProjectRoleAssignment.collection_id == w.p1,
                    ResearchProjectRoleAssignment.user_id == w.ids[REV_A],
                )
            )
        ).scalar_one()
        role.soft_delete()
        await db.flush()

    async with factory() as submitter, factory() as revoker, factory() as observer:
        if order == "submit-first":
            context = await resolve_project(submitter, w.p1, w.ids[REV_A], REVIEW)
            pid = (await revoker.execute(text("SELECT pg_backend_pid()"))).scalar_one()

            async def revocation() -> None:
                await revoke(revoker)
                await revoker.commit()

            attempt = asyncio.create_task(revocation())
            try:
                await _wait_until_blocked(observer, pid)
                assert not attempt.done()
                await svc.submit(submitter, context, w.ids[REV_A], body)
                await attempt
            finally:
                if not attempt.done():
                    attempt.cancel()
                    await asyncio.gather(attempt, return_exceptions=True)
            expected = 1
        else:
            await revoke(revoker)
            pid = (
                await submitter.execute(text("SELECT pg_backend_pid()"))
            ).scalar_one()

            async def submission() -> Any:
                context = await resolve_project(submitter, w.p1, w.ids[REV_A], REVIEW)
                return await svc.submit(submitter, context, w.ids[REV_A], body)

            attempt = asyncio.create_task(submission())
            try:
                await _wait_until_blocked(observer, pid)
                await revoker.commit()
                with pytest.raises(HTTPException) as denied:
                    await attempt
                assert denied.value.status_code == 403
            finally:
                if not attempt.done():
                    attempt.cancel()
                    await asyncio.gather(attempt, return_exceptions=True)
                await submitter.rollback()
            expected = 0
    assert (
        await _scalar(factory, "SELECT count(*) FROM appraisal_assessments") == expected
    )
    assert (
        await _scalar(
            factory,
            """SELECT count(*) FROM research_decision_events
               WHERE event_type = 'appraisal.submitted'""",
        )
        == expected
    ), "a submission must follow the lock order"
