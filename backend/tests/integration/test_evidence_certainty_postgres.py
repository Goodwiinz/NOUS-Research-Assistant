"""Real PostgreSQL proof for GOO-310 evidence tables, contradictions and certainty.

Runs on GOO-301's ``screening_factory`` schema, whose migration chain now ends
at ``f4b6d8a0c2e3``: the three insert-only tables, their partial unique
indexes, CHECKs and triggers come from the migration. GOO-299's seed gives
owner O (no roles), reviewer R (reviewer A in the plan), adjudicator A (J in
the plan), viewer V and foreign-org F; GOO-309's helpers add reviewer B.

Mutation verification (docs/engineering/testing.md), full record in
``docs/testing/agent-orchestration-mutation-checks.md`` (GOO-310 section):

- ``build_rows`` keyed by report: step 2, study S appears twice;
- the adjudicator-only resolution in ``record_contradiction`` skipped:
  step 4, reviewer A's resolution reaches the insert, where only the
  ``ck_evidence_contradiction_decider`` CHECK refuses it (IntegrityError,
  not the 403);
- the stale-tip filter in ``_tips`` skipped: step 9, the rebuilt T2 still
  carries D3's source-changed ``Mean age`` value;
- the risk-of-bias check in ``check_certainty`` skipped: step 5, a non-null
  rating is accepted while R3's appraisal is awaiting;
- ``graph_part`` connecting every table to every accepted value: step 7, T2
  goes stale when D1's intervention value is superseded;
- the organization filter on the stance query dropped: step 2, the org-B
  row appears in the suggestion group;
- the organization filter on the suggestion snapshot dropped: step 4, citing
  the org-B row is accepted instead of 404.

Command (from ``backend/``): ``RESEARCH_DECISION_DATABASE_URL=... pytest -q
tests/integration/test_evidence_certainty_postgres.py``.
"""

import importlib.util
import io
import json
import zipfile
from types import SimpleNamespace
from typing import Any, Awaitable, Callable, TypeAlias, cast
from uuid import UUID, uuid4

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import text
from sqlalchemy.engine import Connection
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from src.api.research import extraction_matrix as matrix_routes
from src.api.research_engine import journey as journey_routes
from src.models.collection import CollectionDocument
from src.models.evidence import StanceClassificationModel, StanceEnum
from src.models.extraction_matrix import ExtractionMatrix, ExtractionObservation
from src.models.research_project_role import ResearchProjectRole
from src.models.research_report import ResearchReport, ResearchStudy
from src.models.research_run import ResearchRun
from src.models.user import User
from src.schemas.research_engine import (
    CertaintyCreate,
    ContradictionCreate,
    EvidenceTableCreate,
)
from src.services.research import extraction_forms_service as forms
from src.services.research_decisions import replay_decisions
from src.services.research_engine import evidence_service as svc
from src.services.research_engine.project_access import (
    ProjectContext,
    ResearchAction,
    resolve_project,
)
from src.shared.scispace_schemas import (
    ExtractionAcceptCreate,
    ExtractionObservationCreate,
)
from tests.integration.research_engine_postgres_support import (
    seed_approved_protocol_binding,
)
from tests.integration.test_appraisal_postgres import REV_A, REV_B
from tests.integration.test_appraisal_postgres import SNAPSHOT as APPRAISAL_SNAPSHOT
from tests.integration.test_appraisal_postgres import (
    J,
    _add_user,
    _adjudicate,
    _adjudication,
    _as,
    _body,
    _domains,
    _refused,
    _retrieve,
    _scalar,
    _submit,
    _user,
)
from tests.integration.test_draft_release_postgres import _document
from tests.integration.test_report_identity_postgres import _seed
from tests.integration.test_screening_queue_postgres import (  # noqa: F401
    VERSIONS,
    screening_factory,
)

pytestmark = [pytest.mark.integration, pytest.mark.requires_postgres]

Factory: TypeAlias = async_sessionmaker[AsyncSession]
VIEW, REVIEW, ADJUDICATE, EDIT = (
    ResearchAction.VIEW,
    ResearchAction.REVIEW,
    ResearchAction.ADJUDICATE,
    ResearchAction.EDIT,
)
GDS, AGE = ("depressive_symptoms", "12 weeks"), ("mean_age", "baseline")
GDS_I, GDS_C, MEAN_AGE = (
    "GDS mean (intervention)",
    "GDS mean (control)",
    "Mean age",
)
SNAPSHOT = {
    **APPRAISAL_SNAPSHOT,
    "appraisal_synthesis": {
        **cast(dict[str, Any], APPRAISAL_SNAPSHOT["appraisal_synthesis"]),
        "certainty": {"method": "grade", "version": "handbook-2013"},
    },
    "outcomes": {
        "declared": [
            {"key": GDS[0], "label": "Depressive symptoms", "timepoints": [GDS[1]]},
            {"key": AGE[0], "label": "Mean age", "timepoints": [AGE[1]]},
        ]
    },
}
TEXTS = {
    "d1": "GDS intervention 4.1 at 12 weeks. Mean age 71 at baseline.",
    "d2": "GDS intervention 4.4 at 12 weeks. Mean age 72 at baseline.",
    "d3": "No depression outcome was reported. Mean age 69 at baseline.",
    "d5": "Mean age 70 at baseline.",
}
TABLES = (
    "evidence_table_versions",
    "evidence_contradictions",
    "outcome_certainty_assessments",
)


async def _accept(
    w: Any, field: str, document: UUID, value: Any, quote: str, key: str
) -> tuple[UUID, UUID]:
    """GOO-304: reviewer R observes, adjudicator A accepts: (accepted, obs)."""
    async with w.factory() as db:
        observation = await matrix_routes.create_observation(
            w.matrix,
            ExtractionObservationCreate(
                document_id=document,
                field_id=w.fields[field],
                form_version_id=w.form_version,
                value=value,
                citation=quote,
                idempotency_key=f"obs-{key}",
            ),
            current_user=_user(w.ids["R"]),
            db=db,
        )
    async with w.factory() as db:
        accepted = await matrix_routes.accept_extraction_value(
            w.matrix,
            ExtractionAcceptCreate(
                document_id=document,
                field_id=w.fields[field],
                form_version_id=w.form_version,
                observation_ids=[observation.id],
                value=value,
                rationale="results table",
                idempotency_key=f"acc-{key}",
            ),
            current_user=_user(w.ids["A"]),
            db=db,
        )
    return cast(UUID, accepted.id), cast(UUID, observation.id)


async def _setup(factory: Factory) -> Any:
    ids = await _seed(factory)
    await _add_user(factory, ids, REV_B, ResearchProjectRole.REVIEWER)
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
            ("R5", study.id, "disputed"),
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
        documents = {}
        for name, body in TEXTS.items():
            documents[name] = await _document(db, ids["org"], name, body)
            db.add(CollectionDocument(collection_id=p1, document_id=documents[name]))
        await db.commit()
    w = SimpleNamespace(
        factory=factory,
        ids=ids,
        p1=p1,
        version=binding.protocol_version_id,
        study=cast(UUID, study.id),
        docs=documents,
        **reports,
    )
    for label, document in (("R1", "d1"), ("R2", "d2"), ("R3", "d3"), ("R5", "d5")):
        await _retrieve(w, reports[label], documents[document])
    columns = [
        {"name": GDS_I, "type": "number", "unit": "points", "timepoint": GDS[1]},
        {"name": GDS_C, "type": "number", "unit": "points", "timepoint": GDS[1]},
        {"name": MEAN_AGE, "type": "number", "unit": "years", "timepoint": AGE[1]},
    ]
    async with factory() as db:
        context = await resolve_project(db, p1, ids["O"], EDIT)
        matrix = ExtractionMatrix(project_id=p1, name="outcomes", columns=columns)
        db.add(matrix)
        version = await forms.create_version(db, context, matrix, columns, ids["O"])
        w.fields = {f["name"]: UUID(f["field_id"]) for f in version.fields}
        w.matrix, w.form_version = cast(UUID, matrix.id), cast(UUID, version.id)
    d = documents
    w.i1, w.i1_obs = await _accept(
        w, GDS_I, d["d1"], 4.1, "GDS intervention 4.1 at 12 weeks.", "i1"
    )
    w.i2, _ = await _accept(
        w, GDS_I, d["d2"], 4.4, "GDS intervention 4.4 at 12 weeks.", "i2"
    )
    w.age = {}
    for name, age in (("d1", 71), ("d2", 72), ("d3", 69), ("d5", 70)):
        w.age[name], _ = await _accept(
            w, MEAN_AGE, d[name], age, f"Mean age {age} at baseline.", f"age-{name}"
        )
    async with factory() as db:
        db.add(  # a machine cell GOO-304 never reviewed
            ExtractionObservation(
                form_version_id=w.form_version,
                field_id=w.fields[GDS_C],
                document_id=d["d3"],
                kind="machine",
                actor_user_id=ids["O"],
                extractor_run_id="run-1",
                extractor_model="extractor-v1",
                value=3.9,
                validation_state="valid",
                source_hash="0" * 64,
            )
        )
        w.stance = {}
        for key, org, source, stance in (
            ("support", ids["org"], d["d1"], StanceEnum.SUPPORTING),
            ("oppose", ids["org"], d["d3"], StanceEnum.OPPOSING),
            ("foreign", ids["foreign_org"], d["d1"], StanceEnum.OPPOSING),
        ):
            row = StanceClassificationModel(
                claim_hash="c" * 64,
                claim_text="Exercise reduces depressive symptoms",
                source_id=source,
                organization_id=org,
                stance=stance,
                confidence=0.8,
                model_version=f"meter-{key}",
            )
            db.add(row)
            await db.flush()
            w.stance[key] = cast(UUID, row.id)
        await db.commit()
    # GOO-309 appraisals: S adjudicated, R3 awaiting its second reviewer.
    a1, _ = await _submit(w, REV_A, _body(w, "s-a", overall="low"))
    b1, _ = await _submit(w, REV_B, _body(w, "s-b", overall="high"))
    w.s_appraisal, _ = await _adjudicate(
        w,
        J,
        _adjudication(
            w, "s-j", [a1.id, b1.id], domains=_domains(D4="high"), overall="high"
        ),
    )
    await _submit(w, REV_A, _body(w, "r3-a", target={"report_id": w.R3}))
    return w


def _table_body(w: Any, key: str, outcome: tuple[str, str], **fields: Any) -> Any:
    names = fields.pop("names", [GDS_I, GDS_C] if outcome == GDS else [MEAN_AGE])
    return EvidenceTableCreate.model_validate(
        {
            "outcome_key": outcome[0],
            "timepoint": outcome[1],
            "matrix_id": w.matrix,
            "field_ids": [w.fields[n] for n in names],
            "idempotency_key": key,
            **fields,
        }
    )


def _preview(w: Any, outcome: tuple[str, str], names: list[str]) -> Awaitable[Any]:
    return _as(
        w,
        "V",
        VIEW,
        lambda db, ctx: svc.preview(
            db, ctx, outcome[0], outcome[1], w.matrix, [w.fields[n] for n in names]
        ),
    )


def _write(
    w: Any,
    user: str,
    action: ResearchAction,
    call: Callable[[AsyncSession, ProjectContext], Awaitable[Any]],
) -> Awaitable[Any]:
    return _as(w, user, action, call)


def _freeze(w: Any, user: str, body: Any, role: str = "reviewer") -> Awaitable[Any]:
    action = REVIEW if role == "reviewer" else ADJUDICATE
    return _write(
        w,
        user,
        action,
        lambda db, ctx: svc.create_table(db, ctx, w.ids[user], role, body),
    )


def _contradict(w: Any, user: str, role: str, **fields: Any) -> Awaitable[Any]:
    body = ContradictionCreate.model_validate(
        {
            "explanation": f"{fields['kind']} by {user}",
            "idempotency_key": str(uuid4()),
            **fields,
        }
    )
    action = REVIEW if role == "reviewer" else ADJUDICATE
    return _write(
        w,
        user,
        action,
        lambda db, ctx: svc.record_contradiction(db, ctx, w.ids[user], role, body),
    )


def _ratings(**overrides: int | None) -> dict[str, int | None]:
    return {
        "risk_of_bias": None,
        "inconsistency": 0,
        "indirectness": 0,
        "imprecision": 0,
        "publication_bias": 0,
        **overrides,
    }


def _assess(w: Any, user: str, **fields: Any) -> Awaitable[Any]:
    body = CertaintyCreate.model_validate(
        {
            "starting_level": "high",
            "rationale": "GRADE per protocol",
            "idempotency_key": str(uuid4()),
            **fields,
        }
    )
    return _write(
        w,
        user,
        REVIEW,
        lambda db, ctx: svc.assess_certainty(db, ctx, w.ids[user], body),
    )


async def _outcomes(w: Any, user: str = "V") -> dict[tuple[str, str], Any]:
    listing = await _as(w, user, VIEW, lambda db, ctx: svc.list_outcomes(db, ctx))
    return {(o.outcome_key, o.timepoint): o for o in listing.outcomes}


def _migration(connection: Connection, direction: str) -> None:
    filename = "f4b6d8a0c2e3_create_evidence_certainty.py"
    spec = importlib.util.spec_from_file_location(filename[:-3], VERSIONS / filename)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    setattr(module, "op", Operations(MigrationContext.configure(connection)))
    cast(Callable[[], None], getattr(module, direction))()


async def test_evidence_table_contradiction_certainty_and_selective_staleness(
    screening_factory: Factory,
) -> None:
    factory = screening_factory
    # 1. Seed.
    w = await _setup(factory)
    s_unit, r3_unit = f"study:{w.study}", f"report:{w.R3}"
    gi, gc, ma = (str(w.fields[n]) for n in (GDS_I, GDS_C, MEAN_AGE))

    # 2. Preview: one row per unit, missing is missing, D5 excluded.
    preview = await _preview(w, GDS, [GDS_I, GDS_C])
    rows = {row.unit: row for row in preview.rows}
    assert [row.unit for row in preview.rows].count(s_unit) == 1
    assert set(rows) == {s_unit, r3_unit}
    s_cell = rows[s_unit].cells[gi]
    assert s_cell.state == "conflict"
    assert {t.accepted_value_id for t in s_cell.tips} == {w.i1, w.i2}
    assert set(rows[s_unit].report_ids) == {w.R1, w.R2}
    assert rows[s_unit].cells[gc].state == "missing"
    assert {c.state for c in rows[r3_unit].cells.values()} == {"missing"}
    assert all(c.value is None and c.tips == [] for c in rows[r3_unit].cells.values())
    excluded = {(e.document_id, e.reason) for e in preview.excluded}
    assert (w.docs["d5"], "study_link_unresolved") in excluded
    assert preview.differs_from_tip is False and preview.tip_id is None
    assert [
        (c.document_id, str(c.field_id), c.source, c.review_state)
        for c in preview.unreviewed_cells
    ] == [(w.docs["d3"], gc, "machine", "unreviewed")]
    (group,) = preview.stance_suggestions
    assert group.review_state == "unreviewed_model_suggestion"
    assert {s.id for s in group.suggestions} == {
        w.stance["support"],
        w.stance["oppose"],
    }
    assert str(w.stance["foreign"]) not in preview.model_dump_json()
    await _refused(
        _preview(w, ("depressive_symptoms", "6 weeks"), [GDS_I]), 422, svc.UNDECLARED
    )
    await _refused(_preview(w, GDS, [MEAN_AGE]), 422, "is not at 12 weeks")

    # 3. Freeze T1 (reviewer A); an unchanged rebuild writes nothing.
    await _refused(_freeze(w, "O", _table_body(w, "o-t1", GDS)), 403, "reviewer")
    await _refused(_freeze(w, "F", _table_body(w, "f-t1", GDS)), 404)
    t1, replayed = await _freeze(w, REV_A, _table_body(w, "t1", GDS))
    assert not replayed and t1.content_hash == preview.content_hash
    assert [r.model_dump(mode="json") for r in t1.rows] == [
        r.model_dump(mode="json") for r in preview.rows
    ]
    again, replayed = await _freeze(w, REV_B, _table_body(w, "t1-again", GDS))
    assert replayed and again.id == t1.id
    assert await _scalar(factory, "SELECT count(*) FROM evidence_table_versions") == 1
    async with factory() as db:
        joined = (
            (
                await db.execute(
                    text("""
                    SELECT tip->>'accepted_value_id' AS id,
                           tip->>'source_hash' = a.source_hash AS pinned,
                           r.id IS NOT NULL AS report,
                           d.id IS NOT NULL AS document
                    FROM evidence_table_versions t,
                         jsonb_array_elements(t.rows) row,
                         jsonb_each(row->'cells') cell,
                         jsonb_array_elements(cell.value->'tips') tip
                    JOIN extraction_accepted_values a
                      ON a.id = (tip->>'accepted_value_id')::uuid
                    LEFT JOIN research_reports r
                      ON r.id = (tip->>'report_id')::uuid
                    LEFT JOIN documents d
                      ON d.id = (tip->>'document_id')::uuid
                     AND d.id = a.document_id
                    WHERE t.id = :t
                    """),
                    {"t": t1.id},
                )
            )
            .mappings()
            .all()
        )
    assert {row["id"] for row in joined} == {str(w.i1), str(w.i2)}
    assert all(row["pinned"] and row["report"] and row["document"] for row in joined)

    # 4. Contradiction: opened by A from the suggestion, resolved by J, dissent B.
    await _refused(
        _contradict(
            w,
            REV_A,
            "reviewer",
            kind="opened",
            table_version_id=t1.id,
            field_id=w.fields[GDS_I],
            accepted_value_ids=[w.i1, w.age["d1"]],
        ),
        422,
        svc.MEMBERS,
    )
    await _refused(
        _contradict(
            w,
            REV_A,
            "reviewer",
            kind="opened",
            table_version_id=t1.id,
            field_id=w.fields[GDS_I],
            accepted_value_ids=[w.i1, w.i2],
            stance_classification_ids=[w.stance["foreign"]],
        ),
        404,
        svc.SUGGESTION_NOT_FOUND,
    )
    group, _ = await _contradict(
        w,
        REV_A,
        "reviewer",
        kind="opened",
        table_version_id=t1.id,
        field_id=w.fields[GDS_I],
        accepted_value_ids=[w.i1, w.i2],
        stance_classification_ids=[w.stance["support"], w.stance["oppose"]],
        idempotency_key="open-1",
    )
    assert group.status == "unresolved"
    suggestion = group.rows[0].suggestion
    assert suggestion is not None and suggestion["origin"] == "model_suggestion"
    assert {r["id"] for r in suggestion["rows"]} == {
        str(w.stance["support"]),
        str(w.stance["oppose"]),
    }
    opened = group.rows[0].id
    await _refused(
        _contradict(
            w,
            REV_A,
            "reviewer",
            kind="resolved",
            contradiction_id=group.contradiction_id,
            previous_id=opened,
            idempotency_key="a-resolve",
        ),
        403,
        "adjudicator",
    )
    resolved, _ = await _contradict(
        w,
        J,
        "adjudicator",
        kind="resolved",
        contradiction_id=group.contradiction_id,
        previous_id=opened,
        idempotency_key="j-resolve",
    )
    await _refused(
        _contradict(
            w,
            REV_B,
            "reviewer",
            kind="dissent",
            contradiction_id=group.contradiction_id,
            previous_id=opened,
            idempotency_key="b-stale",
        ),
        409,
        svc.CONTRADICTION_STALE,
    )
    final, _ = await _contradict(
        w,
        REV_B,
        "reviewer",
        kind="dissent",
        contradiction_id=group.contradiction_id,
        previous_id=resolved.rows[-1].id,
        idempotency_key="b-dissent",
    )
    assert final.status == "resolved"
    assert [r.kind for r in final.rows] == ["opened", "resolved", "dissent"]
    assert [(d.kind, d.actor_id) for d in final.dissent] == [("dissent", w.ids[REV_B])]

    # 5. Certainty on T1: R3's appraisal is awaiting, so RoB stays unknown.
    await _refused(
        _assess(
            w,
            REV_A,
            table_version_id=t1.id,
            ratings=_ratings(risk_of_bias=-1, inconsistency=-1),
            level="low",
            appraisal_assessment_ids=[w.s_appraisal.id],
            contradiction_ids=[group.contradiction_id],
        ),
        422,
        f"Risk of bias unresolved for {r3_unit}",
    )
    await _refused(
        _assess(
            w,
            REV_A,
            table_version_id=t1.id,
            ratings=_ratings(inconsistency=-1),
            level="moderate",
            contradiction_ids=[group.contradiction_id],
        ),
        422,
        "not derived",
    )
    for user, status in (("O", 403), ("F", 404)):
        await _refused(
            _assess(
                w,
                user,
                table_version_id=t1.id,
                ratings=_ratings(),
                idempotency_key=f"{user}-c",
            ),
            status,
        )
    c1, _ = await _assess(
        w,
        REV_A,
        table_version_id=t1.id,
        ratings=_ratings(inconsistency=-1),
        level=None,
        contradiction_ids=[group.contradiction_id],
        idempotency_key="c1",
    )
    assert c1.level is None and c1.domains["risk_of_bias"] is None
    assert c1.unresolved_contradictions == []
    assert [d.id for d in c1.dissent] == [final.rows[-1].id]

    # 6. A separate outcome: T2 (mean age at baseline) and its certainty.
    t2, _ = await _freeze(w, J, _table_body(w, "t2", AGE), role="adjudicator")
    assert {r.unit for r in t2.rows} == {s_unit, r3_unit}
    c2, _ = await _assess(
        w, REV_B, table_version_id=t2.id, ratings=_ratings(), idempotency_key="c2"
    )

    # 7. Selective staleness: supersede D1's intervention value.
    xmin_sql = " UNION ALL ".join(f"SELECT id, xmin::text FROM {t}" for t in TABLES)
    async with factory() as db:
        before = sorted((await db.execute(text(xmin_sql))).all())
    async with factory() as db:
        await matrix_routes.accept_extraction_value(
            w.matrix,
            ExtractionAcceptCreate(
                document_id=w.docs["d1"],
                field_id=w.fields[GDS_I],
                form_version_id=w.form_version,
                observation_ids=[w.i1_obs],
                value=4.1,
                rationale="re-accepted after checking the results table",
                supersedes_accepted_value_id=w.i1,
                idempotency_key="acc-i1-again",
            ),
            current_user=_user(w.ids[J]),
            db=db,
        )
    listed = await _outcomes(w)
    gds, age = listed[GDS], listed[AGE]
    assert [t.stale for t in gds.tables] == [True]
    assert [c.stale for c in gds.certainty] == [True]
    assert [g.stale for g in gds.contradictions] == [True]
    assert [t.stale for t in age.tables] == [False]
    assert [c.stale for c in age.certainty] == [False]
    async with factory() as db:
        assert sorted((await db.execute(text(xmin_sql))).all()) == before
        stored_rows = (
            await db.execute(
                text("SELECT rows FROM evidence_table_versions WHERE id = :t"),
                {"t": t1.id},
            )
        ).scalar_one()
    package = await _as(w, "V", VIEW, lambda db, ctx: svc.export_package(db, ctx))
    exported = {
        t["id"]: t for outcome in package["body"]["outcomes"] for t in outcome["tables"]
    }
    assert exported[str(t1.id)]["stale"] is True
    assert json.dumps(exported[str(t1.id)]["rows"], sort_keys=True) == json.dumps(
        stored_rows, sort_keys=True
    )

    # 8. Changed source: D3's text changes with no writer call; T2 stales.
    async with factory() as db:
        await db.execute(
            text(
                "UPDATE documents SET content_text = content_text || ' Erratum.'"
                " WHERE id = :d"
            ),
            {"d": w.docs["d3"]},
        )
        await db.commit()
    listed = await _outcomes(w)
    assert [t.stale for t in listed[AGE].tables] == [True]
    assert [c.stale for c in listed[AGE].certainty] == [True]

    # 9. Successors: the rebuilt tables drop stale tips; old versions stay.
    await _refused(
        _freeze(w, REV_A, _table_body(w, "t1-b-stale", GDS)), 409, svc.TABLE_STALE
    )
    t1b, replayed = await _freeze(
        w, REV_A, _table_body(w, "t1-b", GDS, supersedes_table_id=t1.id)
    )
    assert not replayed and t1b.supersedes_table_id == t1.id
    s_tips = {
        t.accepted_value_id
        for row in t1b.rows
        if row.unit == s_unit
        for t in row.cells[gi].tips
    }
    assert w.i1 not in s_tips and w.i2 in s_tips and len(s_tips) == 2
    t2b, _ = await _freeze(
        w, REV_A, _table_body(w, "t2-b", AGE, supersedes_table_id=t2.id)
    )
    r3_age = next(r for r in t2b.rows if r.unit == r3_unit).cells[ma]
    assert r3_age.state == "missing" and r3_age.tips == []
    listed = await _outcomes(w)
    assert [(t.id, t.superseded, t.stale) for t in listed[GDS].tables] == [
        (t1.id, True, True),
        (t1b.id, False, False),
    ]
    await _refused(
        _assess(
            w,
            REV_A,
            table_version_id=t1.id,
            ratings=_ratings(),
            supersedes_certainty_id=c1.id,
            idempotency_key="c1-old",
        ),
        409,
        svc.TABLE_STALE,
    )
    c1b, _ = await _assess(
        w,
        REV_B,
        table_version_id=t1b.id,
        ratings=_ratings(),
        supersedes_certainty_id=c1.id,
        idempotency_key="c1-b",
    )
    listed = await _outcomes(w)
    assert [(c.id, c.superseded) for c in listed[GDS].certainty] == [
        (c1.id, True),
        (c1b.id, False),
    ]

    # The GOO-308 audit bundle carries the same package, stale versions too.
    package = await _as(w, "V", VIEW, lambda db, ctx: svc.export_package(db, ctx))
    async with factory() as db:
        bundle = await journey_routes.audit_bundle_route(
            w.p1, current_user=cast(User, _user(w.ids["V"])), db=db
        )
    with zipfile.ZipFile(io.BytesIO(cast(bytes, bundle.body))) as archive:
        sealed = json.loads(archive.read("evidence.json"))
    assert sealed["schema"] == svc.EXPORT_SCHEMA
    assert sealed["body_sha256"] == package["body_sha256"]
    assert str(t1.id) in json.dumps(sealed["body"])

    # 10. Insert-only.
    for table in TABLES:
        for statement in (
            f"UPDATE {table} SET created_at = now()",
            f"DELETE FROM {table}",
        ):
            async with factory() as db:
                with pytest.raises(DBAPIError) as refused:
                    await db.execute(text(statement))
                assert getattr(refused.value.orig, "sqlstate", None) == "55000"

    # 11. Archived: writes 409, reads 200. Deleted workspace: 404.
    async with factory() as db:
        await db.execute(
            text("UPDATE collections SET research_status = 'archived' WHERE id = :p"),
            {"p": w.p1},
        )
        await db.commit()
    await _refused(
        _freeze(w, REV_A, _table_body(w, "archived", GDS)), 409, "not writable"
    )
    assert GDS in await _outcomes(w, REV_A)
    async with factory() as db:
        await db.execute(
            text("UPDATE workspaces SET is_deleted = true WHERE id = :w"),
            {"w": w.ids["workspace"]},
        )
        await db.commit()
    await _refused(_outcomes(w, REV_A), 404)

    # 12. Replay: contiguous and valid.
    async with factory() as db:
        events = await replay_decisions(
            db,
            collection_id=w.p1,
            aggregate_type="research_evidence",
            aggregate_id=w.p1,
        )
    assert [e.seq for e in events] == list(range(1, len(events) + 1))
    assert [str(e.event_type) for e in events] == [
        "evidence.table_versioned",
        "evidence.contradiction_recorded",
        "evidence.contradiction_recorded",
        "evidence.contradiction_recorded",
        "evidence.certainty_assessed",
        "evidence.table_versioned",
        "evidence.certainty_assessed",
        "evidence.table_versioned",
        "evidence.table_versioned",
        "evidence.certainty_assessed",
    ]

    # 13. Downgrade drops only the three tables; GOO-309's function stays.
    async with factory() as db:
        connection = await db.connection()
        await connection.run_sync(lambda sync: _migration(sync, "downgrade"))
        await db.commit()
    async with factory() as db:
        tables = set((await db.execute(text("""SELECT tablename FROM pg_tables
                                WHERE schemaname = current_schema()"""))).scalars())
        functions = set((await db.execute(text("""SELECT p.proname FROM pg_proc p
                        JOIN pg_namespace n ON n.oid = p.pronamespace
                        WHERE n.nspname = current_schema()"""))).scalars())
    assert not set(TABLES) & tables
    assert {"appraisal_assessments", "extraction_accepted_values"} <= tables
    assert "prevent_research_insert_only_mutation" in functions
