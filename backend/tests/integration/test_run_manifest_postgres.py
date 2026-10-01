"""Real PostgreSQL proof for GOO-312's run manifests, artifacts and figures.

Runs on GOO-301's ``screening_factory`` schema, whose migration chain now ends
at ``b8e0c2d4f6a7``: the three insert-only tables, the ``figure_id`` column
and the widened claim-link CHECKs come from the migration. GOO-299's seed
gives owner O, reviewer R, adjudicator A, viewer V and foreign-org F.

The run executes through the real ``POST /blueprints/{id}/runs`` and
``GET /runs/{id}/stream`` routes. Only the sandbox is fake: it writes the
step's files to a temporary directory and runs the plan's exact script with
the local interpreter, so the SVG, ``metrics.json`` and ``table.csv`` bytes
are computed from the input CSV by the same script text.

Amendment to the plan's step 7, forced by the plan itself: the approved plan
pins the input's sha256, so a re-run over the corrected CSV needs a protocol
amendment. The successor ``fig-1`` is therefore registered on run 1's other
figure-eligible output (``table.csv``); it exercises the same
``invalidate_dependents`` call and ``research_experiment`` cause.

Mutation verification (docs/engineering/testing.md), full record in
``docs/testing/agent-orchestration-mutation-checks.md`` (GOO-312 section):

- ``assert_no_secrets`` made a no-op: step 6, the run completes and records
  a manifest instead of failing with ``manifest_secret_detected``;
- ``graph_part`` linking every figure to every claim link: step 7, the
  unrelated draft (it cites a source span) reads ``stale``;
- ``invalidate_dependents`` skipped on a successor: step 7, release 1 stays
  unstamped.

Command (from ``backend/``): ``RESEARCH_DECISION_DATABASE_URL=... pytest -q
tests/integration/test_run_manifest_postgres.py``.
"""

import asyncio
import hashlib
import json
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any, AsyncIterator, Awaitable, TypeAlias, cast
from uuid import UUID

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from src.api.research_engine.experiments import router as experiments_router
from src.api.research_engine.runs import router as runs_router
from src.core.database import get_db
from src.core.dependencies import get_current_user
from src.models.collection import CollectionDocument
from src.models.document import Document, DocumentType
from src.models.generated_draft import GeneratedDraft
from src.models.research_blueprint import ResearchBlueprint
from src.models.research_project import ResearchProject
from src.schemas.research_engine import FigureCreate
from src.services.artifacts.storage import LocalArtifactStorage
from src.services.research import draft_release_service
from src.services.research_decisions import replay_decisions
from src.services.research_engine import experiment_service as svc
from src.services.research_engine import manifest_rules, step_executor
from src.services.research_engine.project_access import ProjectContext, ResearchAction
from src.services.sandbox.e2b_sandbox_manager import IsolatedResult, IsolatedSpec
from tests.integration.research_engine_postgres_support import (
    seed_approved_protocol_binding,
)
from tests.integration.test_appraisal_postgres import _as, _refused, _scalar
from tests.integration.test_draft_release_postgres import (
    _assess,
    _claim,
    _link,
    _promote,
)
from tests.integration.test_report_identity_postgres import _seed
from tests.integration.test_screening_queue_postgres import (  # noqa: F401
    screening_factory,
)

pytestmark = [pytest.mark.integration, pytest.mark.requires_postgres]

Factory: TypeAlias = async_sessionmaker[AsyncSession]
VIEW, EDIT = ResearchAction.VIEW, ResearchAction.EDIT
SEED = 20260930
SECRET = "e2b_live_" + "0123456789abcdef"  # built at runtime so scanners see no token
CSV = b"x,y\n1,2\n2,4\n3,9\n"
CSV2 = b"x,y\n1,2\n2,4\n3,6\n"
CODE = """import json
rows = [line.split(",") for line in open("in/data.csv").read().split()[1:]]
ys = [float(y) for _, y in rows]
mean = sum(ys) / len(ys)
open("out/metrics.json", "w").write(json.dumps({"n": len(ys), "mean_y": mean}))
open("out/table.csv", "w").write("n,mean_y\\n%d,%.6f\\n" % (len(ys), mean))
bars = "".join(
    '<rect x="%d" y="0" width="8" height="%d"/>' % (10 * i, 10 * y)
    for i, y in enumerate(ys)
)
open("out/fig.svg", "w").write('<svg xmlns="http://www.w3.org/2000/svg">%s</svg>' % bars)
"""
LOCK = b"numpy==2.1.0\n"
V1 = "## Results\nMean y rose across the three conditions (Figure 1).\n"
S1 = "Mean y rose across the three conditions (Figure 1)."
V2 = "## Results\nTrial D enrolled 80 participants.\n"
S2 = "Trial D enrolled 80 participants."


def _sha(data: bytes | str) -> str:
    raw = data.encode("utf-8") if isinstance(data, str) else data
    return hashlib.sha256(raw).hexdigest()


class _FakeSandbox:
    """``run_isolated`` over a local temp dir; never a thread cache."""

    def __init__(self) -> None:
        self.lock = LOCK
        self.calls: list[IsolatedSpec] = []

    async def run_isolated(self, spec: IsolatedSpec) -> IsolatedResult:
        self.calls.append(spec)
        started = datetime.now(timezone.utc)
        with tempfile.TemporaryDirectory() as work:
            root = Path(work)
            (root / "out").mkdir()
            for name, data in spec.files.items():
                (root / name).parent.mkdir(parents=True, exist_ok=True)
                (root / name).write_bytes(data)
            done = await asyncio.to_thread(
                subprocess.run,
                [sys.executable, "main.py"],
                cwd=root,
                capture_output=True,
                text=True,
            )
            assert done.returncode == 0, done.stderr
            outputs = {n: (root / "out" / n).read_bytes() for n in spec.output_names}
        return IsolatedResult(
            status="completed",
            outputs=outputs,
            stdout=done.stdout,
            stderr="",
            template_id=spec.template,
            sandbox_id="fake-sandbox",
            lock=self.lock,
            python="Python 3.11.9",
            os_release=b'ID="fixture"\n',
            started_at=started,
            completed_at=datetime.now(timezone.utc),
        )


def _steps(document_id: UUID) -> list[dict[str, Any]]:
    return [
        {
            "type": "analyze",
            "name": "Analyze",
            "params": {
                "code": CODE,
                "code_sha256": _sha(CODE),
                "command": "python main.py",
                "parameters": {"statistic": "mean"},
                "seed": SEED,
                "inputs": [
                    {
                        "name": "data.csv",
                        "kind": "document",
                        "id": str(document_id),
                        "sha256": _sha(CSV),
                    }
                ],
                "outputs": [
                    {
                        "name": "fig.svg",
                        "media_type": "image/svg+xml",
                        "role": "figure",
                    },
                    {
                        "name": "metrics.json",
                        "media_type": "application/json",
                        "role": "metrics",
                    },
                    {"name": "table.csv", "media_type": "text/csv", "role": "table"},
                ],
                "requirements": ["numpy==2.1.0"],
                "template": "code-interpreter-v1",
            },
        }
    ]


async def _setup(factory: Factory, tmp_path: Path) -> Any:
    ids = await _seed(factory)
    p1 = ids["collection"]
    data_path = tmp_path / "data.csv"
    data_path.write_bytes(CSV)
    async with factory() as db:
        document = Document(
            title="data",
            filename="data.csv",
            file_path=str(data_path),
            file_size_bytes=len(CSV),
            mime_type="text/csv",
            document_type=DocumentType.SPREADSHEET,
            organization_id=ids["org"],
            checksum_sha256=_sha(CSV),
            content_text=CSV.decode(),
        )
        unrelated = Document(
            title="trial",
            filename="trial.txt",
            file_path="local:///trial.txt",
            file_size_bytes=len(S2),
            mime_type="text/plain",
            document_type=DocumentType.TEXT,
            organization_id=ids["org"],
            checksum_sha256=_sha(S2),
            content_text=S2,
        )
        db.add_all([document, unrelated])
        await db.flush()
        for doc in (document, unrelated):
            db.add(CollectionDocument(collection_id=p1, document_id=doc.id))
        engine = (
            await db.execute(
                select(ResearchProject).where(ResearchProject.collection_id == p1)
            )
        ).scalar_one()
        steps = _steps(cast(UUID, document.id))
        blueprint = ResearchBlueprint(
            project_id=engine.id, name="experiment", steps=steps, parameters={}
        )
        db.add(blueprint)
        await db.flush()
        binding = await seed_approved_protocol_binding(
            db,
            blueprint_id=cast(UUID, blueprint.id),
            collection_id=p1,
            author_id=ids["O"],
            steps=steps,
            parameters={},
            hypothesis="Mean y rises with x",
        )
        await db.commit()
    return SimpleNamespace(
        factory=factory,
        ids=ids,
        p1=p1,
        doc=cast(UUID, document.id),
        unrelated=cast(UUID, unrelated.id),
        data_path=data_path,
        blueprint=cast(UUID, blueprint.id),
        version=binding.protocol_version_id,
    )


def _app(w: Any, user: str) -> FastAPI:
    app = FastAPI()
    app.include_router(runs_router)
    app.include_router(experiments_router)

    async def override_db() -> AsyncIterator[AsyncSession]:
        async with w.factory() as db:
            yield db

    app.dependency_overrides[get_db] = override_db
    org = w.ids["foreign_org" if user == "F" else "org"]
    app.dependency_overrides[get_current_user] = lambda: SimpleNamespace(
        id=w.ids[user], organization_id=org
    )
    return app


def _client(w: Any, user: str = "O") -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=_app(w, user)), base_url="http://t")


async def _run(w: Any) -> tuple[UUID, str]:
    """Start and stream one run as O: (run id, the stream text)."""
    async with _client(w) as client:
        started = await client.post(
            f"/research-engine/blueprints/{w.blueprint}/runs",
            json={"protocol_version_id": str(w.version)},
        )
        assert started.status_code == 201, started.text
        run_id = UUID(started.json()["id"])
        streamed = await client.get(f"/research-engine/runs/{run_id}/stream")
        assert streamed.status_code == 200, streamed.text
    return run_id, streamed.text


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


def _register(
    w: Any, key: str, output: UUID, supersedes: UUID | None = None, kind: str = "figure"
) -> Awaitable[Any]:
    body = FigureCreate(
        figure_key="fig-1",
        kind=cast(Any, kind),
        caption="Mean y by condition",
        output_artifact_id=output,
        supersedes_figure_id=supersedes,
        idempotency_key=key,
    )
    return _as(
        w, "O", EDIT, lambda db, ctx: svc.register_figure(db, ctx, w.ids["O"], body)
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


async def test_manifest_lineage_legacy_and_source_change(
    screening_factory: Factory,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    factory = screening_factory
    storage = LocalArtifactStorage(tmp_path / "artifacts")
    sandbox = _FakeSandbox()
    monkeypatch.setattr(step_executor, "get_sandbox_manager", lambda: sandbox)
    monkeypatch.setattr(step_executor, "get_artifact_storage", lambda: storage)
    monkeypatch.setattr(svc, "get_artifact_storage", lambda: storage)
    monkeypatch.setattr(
        "src.api.research_engine.runs._build_connectors", lambda **_: object()
    )

    async def admitted(**_: Any) -> bool:
        return True

    monkeypatch.setattr("src.api.research_engine.runs.admit_expensive_work", admitted)
    monkeypatch.setenv("E2B_API_KEY", SECRET)

    # 1. Seed: an approved protocol with a hypothesis, one analyze step.
    w = await _setup(factory, tmp_path)

    # 2. Execute through the engine: one complete manifest, bytes re-hash.
    run1, stream = await _run(w)
    assert "event: run_complete" in stream, stream
    assert len(sandbox.calls) == 1 and sandbox.calls[0].files["in/data.csv"] == CSV
    assert (
        await _scalar(
            factory,
            "SELECT count(*) FROM research_run_manifests WHERE run_id = :r",
            r=run1,
        )
        == 1
    )
    async with factory() as db:
        artifacts = (
            await db.execute(
                text("""SELECT id, role, name, sha256, storage_key
                        FROM research_run_artifacts WHERE run_id = :r"""),
                {"r": run1},
            )
        ).all()
    assert sorted((a.role, a.name) for a in artifacts) == [
        ("code", "main.py"),
        ("environment", "pip-freeze.txt"),
        ("input", "data.csv"),
        ("output", "fig.svg"),
        ("output", "metrics.json"),
        ("output", "table.csv"),
    ]
    for artifact in artifacts:
        assert _sha(await storage.get(artifact.storage_key) or b"") == artifact.sha256
    output_ids = {a.name: cast(UUID, a.id) for a in artifacts if a.role == "output"}

    # 3. Fresh session: the download and every output re-hash.
    async with _client(w, "V") as client:
        body = (await client.get(f"/research-engine/runs/{run1}/manifest/v2")).json()
        assert body["schema"] == manifest_rules.SCHEMA
        assert (body["completeness"], body["missing"]) == ("complete", [])
        manifest = body["manifest"]
        assert manifest["seed"] == SEED and manifest["metrics"]["n"] == 3
        assert manifest["code"]["sha256"] == _sha(CODE)
        assert manifest["environment"]["lock_sha256"] == _sha(LOCK)
        assert manifest["environment"]["image_digest"]["value"] is None
        download = await client.get(
            f"/research-engine/runs/{run1}/manifest/v2/download"
        )
        assert _sha(download.content) == body["manifest_hash"]
        assert download.headers["X-Content-SHA256"] == body["manifest_hash"]
        assert "storage_key" not in download.text and "artifacts/" not in download.text
        for item in manifest["outputs"]:
            got = await client.get(
                f"/research-engine/runs/{run1}/artifacts/{item['artifact_id']}"
            )
            assert got.status_code == 200 and _sha(got.content) == item["sha256"]

    # 4. Figure: register, cite from a claim, assess, promote; lineage resolves.
    fig1, replayed = await _register(w, "fig1", output_ids["fig.svg"])
    assert not replayed and not fig1.stale
    await _refused(_register(w, "metrics", output_ids["metrics.json"]), 422)
    draft1 = await _draft(w, V1, "figure")
    claim1 = await _claim(w, draft1, V1, S1, "c1")
    link1 = await _link(w, claim1, "l1", kind="figure", figure_id=fig1.id)
    await _assess(w, claim1, "a1", [link1])
    release1, _ = await _promote(w, "A", draft1, 1, "p1", _sha(V1))
    draft2 = await _draft(w, V2, "unrelated")
    claim2 = await _claim(w, draft2, V2, S2, "c2")
    link2 = await _link(
        w,
        claim2,
        "l2",
        kind="source_span",
        document_id=w.unrelated,
        start_char=0,
        end_char=len(S2),
        quote=S2,
    )
    await _assess(w, claim2, "a2", [link2])
    release2, _ = await _promote(w, "A", draft2, 1, "p2", _sha(V2))
    for draft in (draft1, draft2):
        check = await _check(w, draft)
        assert check.release_status == "verified", check.blockers
    lineage = await _as(w, "V", VIEW, lambda db, ctx: svc.lineage(db, ctx, fig1.id))
    assert lineage.output.sha256 == manifest["outputs"][0]["sha256"]
    assert lineage.run_id == run1 and lineage.completeness == "complete"
    assert lineage.code["sha256"] == _sha(CODE)
    assert lineage.environment is not None
    assert lineage.environment["template_id"] == "code-interpreter-v1"
    assert lineage.environment["lock_sha256"] == _sha(LOCK)
    assert [i["sha256"] for i in lineage.inputs] == [_sha(CSV)]
    assert lineage.hypothesis_sha256 == _sha("Mean y rises with x")
    assert lineage.protocol_version_id == str(w.version)
    assert lineage.question_version_id is not None

    # 5. Legacy: a pre-R6 run reads incomplete and invents nothing.
    async with factory() as db:
        await db.execute(
            text("""UPDATE research_runs SET reproducibility_manifest =
                    CAST(:m AS jsonb) WHERE id = :r"""),
            {"m": json.dumps({"total_tokens": 5}), "r": w.ids["run"]},
        )
        await db.commit()
    async with _client(w, "V") as client:
        legacy = (
            await client.get(f"/research-engine/runs/{w.ids['run']}/manifest/v2")
        ).json()
        old = (
            await client.get(f"/research-engine/runs/{w.ids['run']}/manifest")
        ).json()
    assert legacy["schema"] == manifest_rules.LEGACY_SCHEMA
    assert legacy["completeness"] == "incomplete" and legacy["manifest"] is None
    assert legacy["legacy"] == old == {"total_tokens": 5, "run_status": "pending"}
    for key in ("code", "environment", "inputs", "outputs"):
        assert key not in legacy and key not in legacy["legacy"]

    # 6. Secrets: a lock carrying the key fails the run; nothing records it.
    sandbox.lock = LOCK + f"E2B_API_KEY={SECRET}\n".encode()
    run_secret, stream = await _run(w)
    assert "event: run_failed" in stream and "manifest_secret_detected" in stream
    assert (
        await _scalar(
            factory, "SELECT status FROM research_runs WHERE id = :r", r=run_secret
        )
        == "failed"
    )
    assert (
        await _scalar(
            factory,
            "SELECT count(*) FROM research_run_manifests WHERE run_id = :r",
            r=run_secret,
        )
        == 0
    )
    for table in ("research_steps", "research_decision_events", "research_runs"):
        assert (
            await _scalar(
                factory,
                f"SELECT count(*) FROM {table} WHERE CAST({table} AS text) LIKE :s",
                s=f"%{SECRET}%",
            )
            == 0
        ), table
    assert not await storage.exists(
        manifest_rules.artifact_key(
            str(w.ids["org"]), str(run_secret), _sha(sandbox.lock)
        )
    )
    sandbox.lock = LOCK

    # 7. Source change: the corrected CSV stales fig-1 and release 1 only.
    w.data_path.write_bytes(CSV2)
    async with factory() as db:
        await db.execute(
            text("UPDATE documents SET checksum_sha256 = :c WHERE id = :d"),
            {"c": _sha(CSV2), "d": w.doc},
        )
        await db.commit()
    listed = await _as(w, "V", VIEW, lambda db, ctx: svc.list_figures(db, ctx))
    assert [(f.id, f.stale) for f in listed.figures] == [(fig1.id, True)]
    assert (await _check(w, draft1)).release_status == "stale"
    assert (await _check(w, draft2)).release_status == "verified"
    await _refused(
        _link(w, claim1, "l1-stale", kind="figure", figure_id=fig1.id),
        409,
        svc.FIGURE_NOT_CURRENT,
    )
    await _refused(_register(w, "fork", output_ids["table.csv"]), 409, svc.FIGURE_STALE)
    fig2, _ = await _register(w, "fig2", output_ids["table.csv"], supersedes=fig1.id)
    assert fig2.supersedes_figure_id == fig1.id
    assert await _release_cause(factory, release1.id) == "research_experiment"
    assert await _release_cause(factory, release2.id) is None
    async with _client(w, "V") as client:
        old_bytes = await client.get(
            f"/research-engine/runs/{run1}/artifacts/{output_ids['fig.svg']}"
        )
    assert old_bytes.status_code == 200
    assert _sha(old_bytes.content) == manifest["outputs"][0]["sha256"]

    # 8. Override rejection is unchanged.
    async with _client(w) as client:
        refused = await client.post(
            f"/research-engine/blueprints/{w.blueprint}/runs",
            json={
                "protocol_version_id": str(w.version),
                "parameters_override": {"seed": 1},
            },
        )
    assert refused.status_code == 409
    assert refused.json()["detail"] == "Method override requires a protocol amendment"

    # 9. Insert-only.
    for table in (
        "research_run_manifests",
        "research_run_artifacts",
        "research_figures",
    ):
        for statement in (
            f"UPDATE {table} SET created_at = now()",
            f"DELETE FROM {table}",
        ):
            async with factory() as db:
                with pytest.raises(DBAPIError) as blocked:
                    await db.execute(text(statement))
                assert getattr(blocked.value.orig, "sqlstate", None) == "55000"

    # 10. Roles and tenancy: foreign 404; archived refuses writes, reads.
    async with _client(w, "F") as client:
        artifact_url = f"/research-engine/runs/{run1}/artifacts/{output_ids['fig.svg']}"
        assert (await client.get(artifact_url)).status_code == 404
        assert (
            await client.get(f"/research-engine/runs/{run1}/manifest/v2")
        ).status_code == 404
    async with factory() as db:
        await db.execute(
            text("UPDATE collections SET research_status = 'archived' WHERE id = :p"),
            {"p": w.p1},
        )
        await db.commit()
    await _refused(
        _register(w, "archived", output_ids["fig.svg"], supersedes=fig2.id),
        409,
        "not writable",
    )
    lineage = await _as(w, "V", VIEW, lambda db, ctx: svc.lineage(db, ctx, fig2.id))
    assert lineage.output.id == output_ids["table.csv"]

    # 11. Replay: every touched stream replays.
    async with factory() as db:
        for aggregate in ("research_experiment", "research_claims", "research_release"):
            events = await replay_decisions(
                db, collection_id=w.p1, aggregate_type=aggregate, aggregate_id=w.p1
            )
            assert events, aggregate
            if aggregate == "research_experiment":
                assert [e.event_type for e in events] == [
                    "run.manifest_recorded",
                    "figure.registered",
                    "figure.registered",
                ]
