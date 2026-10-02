"""Real PostgreSQL proof for GOO-311's protocol-selected SMD/DL synthesis.

Runs on GOO-301's ``screening_factory`` schema, whose migration chain now ends
at ``a6c8e0b2d4f5``: ``synthesis_results`` (its CHECKs, partial unique index
and insert-only trigger) and the widened claim-link CHECKs come from the
migration. GOO-299's seed gives owner O (no roles), reviewer R, adjudicator
A, viewer V and foreign-org F.

Two amendments to the plan's step list, both forced by GOO-307's own rules:

- Step 5's validation failure is a successor on the selected outcome (the
  protocol selects one outcome, so another outcome key is refused 409); it
  runs after step 7, so the tip chain of steps 3-7 is untouched.
- Step 7 changes the input by adding unit G and rebuilding the table, not by
  superseding an accepted value: a superseded accepted value already stamps
  the release stale through GOO-307's extraction cause, so it could never
  show the ``research_synthesis`` cause or catch a skipped
  ``invalidate_dependents``.

Mutation verification (docs/engineering/testing.md), full record in
``docs/testing/agent-orchestration-mutation-checks.md`` (GOO-311 section):

- the unchanged-input short-circuit in ``execute`` skipped: step 4 writes a
  successor (refused by the replay rule) instead of returning the tip;
- ``invalidate_dependents`` skipped on a successor: step 7, release 1 stays
  unstamped;
- ``graph_part`` linking every result to every claim link: step 7, the
  unrelated draft's release is stamped too.

Command (from ``backend/``): ``RESEARCH_DECISION_DATABASE_URL=... pytest -q
tests/integration/test_synthesis_postgres.py``.
"""

import hashlib
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Awaitable, Callable, TypeAlias, cast
from uuid import UUID

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import text
from sqlalchemy.engine import Connection
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from src.models.collection import CollectionDocument
from src.models.generated_draft import GeneratedDraft
from src.models.research_report import ResearchReport, ResearchStudy
from src.models.research_run import ResearchRun
from src.schemas.research_engine import EvidenceTableCreate, SynthesisExecute
from src.services.research import draft_release_service
from src.services.research import extraction_forms_service as forms
from src.services.research_decisions import replay_decisions
from src.services.research_engine import evidence_service
from src.services.research_engine import synthesis_service as svc
from src.services.research_engine.project_access import (
    ProjectContext,
    ResearchAction,
    resolve_project,
)
from tests.integration.research_engine_postgres_support import (
    seed_approved_protocol_binding,
)
from tests.integration.test_appraisal_postgres import SNAPSHOT as APPRAISAL_SNAPSHOT
from tests.integration.test_appraisal_postgres import _as, _refused, _retrieve, _scalar
from tests.integration.test_draft_release_postgres import (
    _accept,
    _assess,
    _claim,
    _document,
    _link,
    _promote,
)
from tests.integration.test_report_identity_postgres import _seed
from tests.integration.test_screening_queue_postgres import (  # noqa: F401
    VERSIONS,
    screening_factory,
)

pytestmark = [pytest.mark.integration, pytest.mark.requires_postgres]

Factory: TypeAlias = async_sessionmaker[AsyncSession]
VIEW, REVIEW, EDIT = ResearchAction.VIEW, ResearchAction.REVIEW, ResearchAction.EDIT
OUTCOME, TIMEPOINT = "depressive_symptoms", "12 weeks"
ROLES = ("mean_i", "sd_i", "n_i", "mean_c", "sd_c", "n_c")
GOLD = json.loads(
    (
        Path(__file__).resolve().parents[1] / "fixtures/synthesis/smd_dl_gold_v1.json"
    ).read_text(encoding="utf-8")
)
HET = next(c for c in GOLD["cases"] if c["id"] == "heterogeneous")
TOL = GOLD["tolerance"]["abs"]
SNAPSHOT = {
    **APPRAISAL_SNAPSHOT,
    "appraisal_synthesis": {
        **cast(dict[str, Any], APPRAISAL_SNAPSHOT["appraisal_synthesis"]),
        "synthesis": {
            "measure": "smd_hedges_g",
            "model": "random_effects_dl",
            "outcome": OUTCOME,
            "timepoint": TIMEPOINT,
        },
    },
}
ARMS = {s["unit"].split(":")[1]: s["arms"] for s in HET["studies"]}  # A-D
ARMS["E"] = [5.0, 3.0, 40, 5.5, 0.0, 40]  # sd_c = 0
ARMS["F"] = [4.9, 2.8, None, 5.4, 2.9, 44]  # n_i never accepted
ARMS["G"] = [4.8, 2.7, 40, 5.6, 2.9, 41]  # added in step 7
V1 = "## Results\nExercise reduced depressive symptoms (pooled SMD -0.31).\n"
S1 = "Exercise reduced depressive symptoms (pooled SMD -0.31)."
V2 = "## Results\nTrial D enrolled 80 participants.\n"
S2 = "Trial D enrolled 80 participants."


def _sha(text_: str) -> str:
    return hashlib.sha256(text_.encode("utf-8")).hexdigest()


def _text(label: str) -> str:
    return " ".join(
        f"{role} {value}."
        for role, value in zip(ROLES, ARMS[label])
        if value is not None
    )


async def _setup(factory: Factory) -> Any:
    ids = await _seed(factory)
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
        study = ResearchStudy(collection_id=p1, label="A")
        db.add(study)
        await db.flush()
        reports, documents = {}, {}
        for name in ("A1", "A2", "B", "C", "D", "E", "F", "G"):
            in_a = name.startswith("A")
            report = ResearchReport(
                collection_id=p1,
                title_snapshot=name,
                study_id=study.id if in_a else None,
                study_link_status="confirmed" if in_a else None,
            )
            db.add(report)
            await db.flush()
            reports[name] = cast(UUID, report.id)
            body = _text(name[0])
            documents[name] = await _document(db, ids["org"], f"d{name}", body)
            if name != "G":  # G joins the project in step 7
                db.add(
                    CollectionDocument(collection_id=p1, document_id=documents[name])
                )
        await db.commit()
    w = SimpleNamespace(
        factory=factory,
        ids=ids,
        p1=p1,
        version=binding.protocol_version_id,
        study=cast(UUID, study.id),
        reports=reports,
        docs=documents,
    )
    for name in reports:
        if name != "G":
            await _retrieve(w, reports[name], documents[name])
    columns = [
        {
            "name": role,
            "type": "number",
            "unit": "participants" if role.startswith("n_") else "GDS-15 points",
            "timepoint": TIMEPOINT,
        }
        for role in ROLES
    ] + [{"name": "pct_c", "type": "number", "unit": "%", "timepoint": TIMEPOINT}]
    async with factory() as db:
        from src.models.extraction_matrix import ExtractionMatrix

        context = await resolve_project(db, p1, ids["O"], EDIT)
        matrix = ExtractionMatrix(project_id=p1, name="outcomes", columns=columns)
        db.add(matrix)
        version = await forms.create_version(db, context, matrix, columns, ids["O"])
        w.fields = {f["name"]: UUID(f["field_id"]) for f in version.fields}
        w.matrix, w.form_version = cast(UUID, matrix.id), cast(UUID, version.id)
    w.accepted = {}
    for name in ("A1", "A2", "B", "C", "D", "E", "F"):
        await _accept_unit(w, name)
    return w


async def _accept_unit(w: Any, name: str) -> None:
    for role, value in zip(ROLES, ARMS[name[0]]):
        if value is None:
            continue
        w.accepted[(name, role)], _ = await _accept(
            w, role, w.docs[name], value, f"{role} {value}.", f"{name}-{role}"
        )


def _roles(w: Any, **override: str) -> dict[str, UUID]:
    names = {role: role for role in ROLES} | override
    return {role: w.fields[names[role]] for role in ROLES}


async def _freeze(w: Any, key: str, supersedes: UUID | None = None) -> Any:
    body = EvidenceTableCreate(
        outcome_key=OUTCOME,
        timepoint=TIMEPOINT,
        matrix_id=w.matrix,
        field_ids=[w.fields[n] for n in (*ROLES, "pct_c")],
        supersedes_table_id=supersedes,
        idempotency_key=key,
    )
    row, _ = await _as(
        w,
        "R",
        REVIEW,
        lambda db, ctx: evidence_service.create_table(
            db, ctx, w.ids["R"], "reviewer", body
        ),
    )
    return row


def _preview(w: Any, table: UUID, roles: dict[str, UUID]) -> Awaitable[Any]:
    return _as(w, "V", VIEW, lambda db, ctx: svc.preview(db, ctx, table, roles))


def _execute(
    w: Any,
    user: str,
    table: UUID,
    roles: dict[str, UUID],
    expected: str,
    key: str,
    supersedes: UUID | None = None,
) -> Awaitable[Any]:
    body = SynthesisExecute(
        table_version_id=table,
        roles=cast(Any, roles),
        expected_input_hash=expected,
        supersedes_result_id=supersedes,
        idempotency_key=key,
    )
    return _as(
        w,
        user,
        REVIEW,
        lambda db, ctx: svc.execute(db, ctx, w.ids[user], body),
    )


async def _counts(factory: Factory) -> tuple[int, int]:
    return (
        await _scalar(factory, "SELECT count(*) FROM synthesis_results"),
        await _scalar(factory, "SELECT count(*) FROM research_decision_events"),
    )


async def _release_cause(factory: Factory, release: UUID) -> str | None:
    async with factory() as db:
        row = (
            await db.execute(
                text("""SELECT e.payload->'cause'->>'family'
                        FROM draft_releases r
                        LEFT JOIN research_decision_events e
                          ON e.id = r.stale_event_id
                        WHERE r.id = :r"""),
                {"r": release},
            )
        ).scalar_one()
    return cast(str | None, row)


async def _draft(w: Any, content: str, title: str) -> UUID:
    async with w.factory() as db:
        draft = GeneratedDraft(
            project_id=w.p1, version=1, title=title, content=content, is_current=True
        )
        db.add(draft)
        await db.commit()
        return cast(UUID, draft.id)


def _check(w: Any, draft: UUID) -> Awaitable[Any]:
    async def call(db: AsyncSession, ctx: ProjectContext) -> Any:
        return await draft_release_service.check(db, ctx, draft, 1)

    return _as(w, "O", VIEW, call)


def _migration(connection: Connection, direction: str) -> None:
    filename = "a6c8e0b2d4f5_create_synthesis_results.py"
    spec = importlib.util.spec_from_file_location(filename[:-3], VERSIONS / filename)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    setattr(module, "op", Operations(MigrationContext.configure(connection)))
    cast(Callable[[], None], getattr(module, direction))()


async def test_synthesis_gold_duplicates_validation_successor_and_stale_release(
    screening_factory: Factory,
) -> None:
    factory = screening_factory
    # 1. Seed: A (two reports, one study), B-D from the gold fixture, E with
    # sd_c = 0, F without n_i; freeze a GOO-310 table.
    w = await _setup(factory)
    t1 = await _freeze(w, "t1")
    a_unit = f"study:{w.study}"
    unit_of = {a_unit: "A"} | {
        f"report:{w.reports[n]}": n for n in ("B", "C", "D", "E", "F")
    }
    roles = _roles(w)

    # 2. Preview: A once with both reports, E and F excluded; no writes.
    before = await _counts(factory)
    preview = await _preview(w, t1.id, roles)
    assert await _counts(factory) == before
    assert sorted(unit_of[u.unit] for u in preview.included) == ["A", "B", "C", "D"]
    (a_row,) = [u for u in preview.included if u.unit == a_unit]
    assert set(a_row.report_ids) == {w.reports["A1"], w.reports["A2"]}
    assert len(a_row.accepted_value_ids) == 12
    assert {(unit_of[e.unit], e.reason) for e in preview.excluded} == {
        ("E", "invalid_variance:c"),
        ("F", "missing_input:n_i"),
    }
    assert preview.run_failures == []

    # 3. Execute as reviewer R: numbers match the gold within 1e-8.
    await _refused(_execute(w, "O", t1.id, roles, preview.input_hash, "o"), 403)
    await _refused(_execute(w, "F", t1.id, roles, preview.input_hash, "f"), 404)
    await _refused(
        _execute(w, "R", t1.id, roles, "0" * 64, "changed"), 409, svc.INPUTS_CHANGED
    )
    r1, replayed = await _execute(w, "R", t1.id, roles, preview.input_hash, "r1")
    assert not replayed and r1.status == "computed"
    expected = HET["expected"]
    by_label = {unit_of[u.unit]: u for u in r1.included}
    for index, label in enumerate("ABCD"):
        assert by_label[label].g == pytest.approx(expected["g"][index], abs=TOL)
        assert by_label[label].v == pytest.approx(expected["v"][index], abs=TOL)
    for key in ("estimate", "se", "ci_low", "ci_high", "q", "tau2", "i2"):
        assert getattr(r1, key) == pytest.approx(expected[key], abs=TOL), key
    assert r1.df == expected["df"] == 3
    assert (r1.protocol_version_id, r1.table_version_id) == (w.version, t1.id)
    assert r1.estimator_version == "nous.smd-hedges-g.dl/1"
    assert r1.config_hash == preview.config_hash and r1.input_hash == preview.input_hash
    assert {(unit_of[e.unit], e.reason) for e in r1.excluded} == {
        ("E", "invalid_variance:c"),
        ("F", "missing_input:n_i"),
    }

    # 4. Stable: a new idempotency key over unchanged inputs writes nothing.
    counts = await _counts(factory)
    again, replayed = await _execute(w, "R", t1.id, roles, preview.input_hash, "r1b")
    assert replayed and (again.id, again.result_hash) == (r1.id, r1.result_hash)
    assert await _counts(factory) == counts

    # 6. Claim link to the pooled SMD; promote the draft (GOO-307).
    draft1 = await _draft(w, V1, "pooled")
    claim1 = await _claim(w, draft1, V1, S1, "c1")
    link1 = await _link(
        w, claim1, "l1", kind="synthesis_result", synthesis_result_id=r1.id
    )
    await _assess(w, claim1, "a1", [link1])
    release1, _ = await _promote(w, "A", draft1, 1, "p1", _sha(V1))
    draft2 = await _draft(w, V2, "unrelated")
    claim2 = await _claim(w, draft2, V2, S2, "c2")
    link2 = await _link(
        w,
        claim2,
        "l2",
        kind="extraction",
        accepted_value_id=w.accepted[("D", "n_i")],
    )
    await _assess(w, claim2, "a2", [link2])
    release2, _ = await _promote(w, "A", draft2, 1, "p2", _sha(V2))
    for draft in (draft1, draft2):
        check = await _check(w, draft)
        assert check.release_status == "verified", check.blockers

    # 7. Changed input: unit G joins and the table is rebuilt.
    async with factory() as db:
        db.add(CollectionDocument(collection_id=w.p1, document_id=w.docs["G"]))
        await db.commit()
    await _retrieve(w, w.reports["G"], w.docs["G"])
    await _accept_unit(w, "G")
    t2 = await _freeze(w, "t2", supersedes=t1.id)
    listed = await _as(w, "V", VIEW, lambda db, ctx: svc.list_results(db, ctx))
    assert [(r.id, r.stale) for r in listed.results] == [(r1.id, True)]
    await _refused(
        _link(
            w, claim1, "l1-stale", kind="synthesis_result", synthesis_result_id=r1.id
        ),
        409,
        svc.RESULT_NOT_CURRENT,
    )
    preview2 = await _preview(w, t2.id, roles)
    assert preview2.input_hash != preview.input_hash and preview2.tip_id == r1.id
    await _refused(
        _execute(w, "R", t2.id, roles, preview2.input_hash, "r2-fork"),
        409,
        svc.RESULT_STALE,
    )
    r2, replayed = await _execute(
        w, "R", t2.id, roles, preview2.input_hash, "r2", supersedes=r1.id
    )
    assert not replayed and r2.supersedes_result_id == r1.id
    assert len(r2.included) == 5
    assert await _release_cause(factory, release1.id) == "research_synthesis"
    assert await _release_cause(factory, release2.id) is None
    assert (
        await _scalar(
            factory,
            "SELECT stale_at IS NULL FROM draft_releases WHERE id = :r",
            r=release2.id,
        )
    ) is True

    # 5. Validation failure (after the chain above): mean_c mapped to a "%"
    # field persists a validation_failed successor with NULL numbers.
    bad_roles = _roles(w, mean_c="pct_c")
    bad_preview = await _preview(w, t2.id, bad_roles)
    assert [f.reason for f in bad_preview.run_failures] == ["unit_mismatch"]
    r3, _ = await _execute(
        w, "R", t2.id, bad_roles, bad_preview.input_hash, "r3", supersedes=r2.id
    )
    assert r3.status == "validation_failed" and r3.estimate is None
    assert r3.q is None and r3.tau2 is None and r3.df is None
    assert any(e.reason == "unit_mismatch" and e.unit is None for e in r3.excluded)
    claim3 = await _claim(w, draft1, V1, S1, "c3")
    await _refused(
        _link(w, claim3, "l3", kind="synthesis_result", synthesis_result_id=r3.id),
        409,
        svc.RESULT_NOT_CURRENT,
    )

    # 8. Insert-only.
    for statement in (
        "UPDATE synthesis_results SET created_at = now()",
        "DELETE FROM synthesis_results",
    ):
        async with factory() as db:
            with pytest.raises(DBAPIError) as refused:
                await db.execute(text(statement))
            assert getattr(refused.value.orig, "sqlstate", None) == "55000"

    # 9. Archived: execute 409, list 200.
    async with factory() as db:
        await db.execute(
            text("UPDATE collections SET research_status = 'archived' WHERE id = :p"),
            {"p": w.p1},
        )
        await db.commit()
    await _refused(
        _execute(w, "R", t2.id, roles, preview2.input_hash, "archived"),
        409,
        "not writable",
    )
    listed = await _as(w, "V", VIEW, lambda db, ctx: svc.list_results(db, ctx))
    assert [r.id for r in listed.results] == [r1.id, r2.id, r3.id]

    # 10. Replay: every touched stream replays.
    async with factory() as db:
        for aggregate in ("research_synthesis", "research_claims", "research_release"):
            events = await replay_decisions(
                db, collection_id=w.p1, aggregate_type=aggregate, aggregate_id=w.p1
            )
            assert events, aggregate
            if aggregate == "research_synthesis":
                assert [e.event_type for e in events] == ["synthesis.executed"] * 3
            if aggregate == "research_release":
                causes = [
                    e.payload["cause"]["family"]
                    for e in events
                    if e.event_type == "release.staled"
                ]
                assert causes == ["research_synthesis"]

    # 11. Downgrade refuses while a synthesis link exists, then restores.
    async with factory() as db:
        connection = await db.connection()
        with pytest.raises(RuntimeError, match="synthesis_result claim links"):
            await connection.run_sync(lambda sync: _migration(sync, "downgrade"))
    async with factory() as db:
        await db.execute(
            text("DELETE FROM research_claim_evidence_links WHERE kind = :k"),
            {"k": "synthesis_result"},
        )
        await db.commit()
    async with factory() as db:
        connection = await db.connection()
        await connection.run_sync(lambda sync: _migration(sync, "downgrade"))
        await db.commit()
    async with factory() as db:
        definition = (
            await db.execute(
                text("""SELECT pg_get_constraintdef(c.oid) FROM pg_constraint c
                        JOIN pg_class t ON t.oid = c.conrelid
                        JOIN pg_namespace n ON n.oid = t.relnamespace
                        WHERE n.nspname = current_schema()
                          AND c.conname = 'ck_research_claim_links_kind'""")
            )
        ).scalar_one()
        tables = set((await db.execute(text("""SELECT tablename FROM pg_tables
                            WHERE schemaname = current_schema()"""))).scalars())
    assert "synthesis_result" not in definition
    assert "synthesis_results" not in tables
