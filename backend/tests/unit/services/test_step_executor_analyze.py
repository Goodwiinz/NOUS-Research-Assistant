"""GOO-312 ``analyze`` blueprint step: plan validation, input checksums,
content-addressed blobs and the unchanged dispatch of every other step.

Mutation verification: skipping the input checksum comparison in
``StepExecutor._execute_analyze`` fails ``-k checksum`` (the step completes).
"""

import hashlib
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any, Mapping

import pytest
from pydantic import ValidationError

from src.schemas.research_engine import (
    BlueprintStepDefinition,
    StepType,
    validate_blueprint_runtime,
)
from src.services.artifacts.storage import MemoryArtifactStorage
from src.services.research_engine import step_executor as module
from src.services.research_engine.contracts import canonical_stage_output_hash
from src.services.research_engine.export_service import ExportService
from src.services.research_engine.step_executor import (
    StepExecutionError,
    StepExecutor,
    StepResult,
)
from src.services.sandbox.e2b_sandbox_manager import IsolatedResult, IsolatedSpec

pytestmark = pytest.mark.unit

ORG, RUN, DOC = "org-1", "run-1", "6f1b7a52-36d7-4a5f-9d3a-3c3b8b0a1e11"
CODE = "open('out/figure.svg','w').write('<svg/>')\n"
CSV = b"group,score\na,1\nb,2\n"
NOW = datetime(2026, 9, 30, tzinfo=timezone.utc)


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _params(**override: Any) -> dict[str, Any]:
    return {
        "code": CODE,
        "code_sha256": _sha(CODE.encode()),
        "command": "python main.py",
        "parameters": {},
        "seed": 20260930,
        "inputs": [
            {"name": "data.csv", "kind": "document", "id": DOC, "sha256": _sha(CSV)}
        ],
        "outputs": [
            {"name": "figure.svg", "media_type": "image/svg+xml", "role": "figure"},
            {
                "name": "metrics.json",
                "media_type": "application/json",
                "role": "metrics",
            },
        ],
        "requirements": ["matplotlib==3.9.2"],
        "template": "code-interpreter-v1",
    } | override


class _Sandbox:
    def __init__(self) -> None:
        self.specs: list[IsolatedSpec] = []

    async def run_isolated(self, spec: IsolatedSpec) -> IsolatedResult:
        self.specs.append(spec)
        return IsolatedResult(
            status="completed",
            outputs={"figure.svg": b"<svg/>", "metrics.json": b'{"n": 2}'},
            stdout="",
            stderr="",
            template_id="tid-1",
            sandbox_id="sbx-1",
            lock=b"matplotlib==3.9.2\n",
            python="Python 3.11.9",
            os_release=b'ID="debian"\n',
            started_at=NOW,
            completed_at=NOW,
        )


@pytest.fixture
def world(monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    storage, sandbox = MemoryArtifactStorage(), _Sandbox()
    monkeypatch.setattr(module, "get_artifact_storage", lambda: storage)
    monkeypatch.setattr(module, "get_sandbox_manager", lambda: sandbox)
    w = SimpleNamespace(storage=storage, sandbox=sandbox, data=CSV, recorded=_sha(CSV))

    async def loader(item: Mapping[str, Any]) -> tuple[bytes, Any, str]:
        assert item["id"] == DOC
        return w.data, w.recorded, "text/csv"

    w.executor = StepExecutor(
        connectors={},
        providers={},
        strategy_context={"run_id": RUN, "organization_id": ORG},
        analyze_inputs=loader,
    )
    return w


def _step(**override: Any) -> dict[str, Any]:
    return {"type": "analyze", "name": "Experiment", "parameters": _params(**override)}


def test_analyze_bad_plan_rejected_at_validation() -> None:
    bad = _params(requirements=["numpy"])
    with pytest.raises(ValueError, match="unpinned_requirement"):
        validate_blueprint_runtime({"steps": [_step(requirements=["numpy"])]})
    with pytest.raises(ValidationError, match="unpinned_requirement"):
        BlueprintStepDefinition(type=StepType.ANALYZE, name="x", parameters=bad)
    with pytest.raises(ValueError, match="multiple_analyze_steps"):
        validate_blueprint_runtime({"steps": [_step(), _step()]})
    validate_blueprint_runtime({"steps": [_step()]})
    assert StepType("analyze") == StepType.ANALYZE


async def test_analyze_input_checksum_mismatch_fails_step(
    world: SimpleNamespace,
) -> None:
    world.data = CSV + b"c,3\n"  # stored bytes changed under the plan
    with pytest.raises(StepExecutionError, match="^input_checksum_mismatch$"):
        await world.executor.execute(_step(), {})
    world.data, world.recorded = CSV, "0" * 64  # Document.checksum_sha256 drifted
    with pytest.raises(StepExecutionError, match="^input_checksum_mismatch$"):
        await world.executor.execute(_step(), {})
    assert world.sandbox.specs == [] and world.storage.objects == {}


async def test_analyze_output_stage_hash_joins_provenance(
    world: SimpleNamespace,
) -> None:
    result = await world.executor.execute(_step(), {})
    (spec,) = world.sandbox.specs
    assert spec.files == {"main.py": CODE.encode(), "in/data.csv": CSV}
    assert spec.requirements == ("matplotlib==3.9.2",)
    assert spec.output_names == ("figure.svg", "metrics.json")
    analyze = result.output["analyze"]
    assert analyze["metrics"] == {"n": 2} and result.seed == 20260930
    assert analyze["environment"]["template_id"] == "tid-1"
    assert analyze["environment"]["image_digest"]["value"] is None
    assert [o["sha256"] for o in analyze["outputs"]] == [
        _sha(b"<svg/>"),
        _sha(b'{"n": 2}'),
    ]
    assert "artifacts/" not in str(result.output)  # no storage key in the step
    for blob in (CODE.encode(), CSV, b"<svg/>", b'{"n": 2}', b"matplotlib==3.9.2\n"):
        key = f"artifacts/{ORG}/research-runs/{RUN}/{_sha(blob)}"
        assert world.storage.objects[key] == blob
    step = SimpleNamespace(step_index=0, output=result.output)
    run = SimpleNamespace(
        id="run-1",
        blueprint=SimpleNamespace(id="bp", steps=[_step()], template_source=None),
        blueprint_version=1,
        started_at=None,
        completed_at=None,
        created_at=None,
    )
    provenance = ExportService._provenance(run, [step], {})  # type: ignore[arg-type]
    assert provenance["stage_hashes"] == {
        "0": canonical_stage_output_hash(result.output)
    }
    assert result.outputs_hash == canonical_stage_output_hash(result.output)


async def test_existing_step_types_dispatch_unchanged(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert dict(StepExecutor._LEGACY_HANDLER_NAMES) == {
        "search": "_execute_search",
        "screen": "_execute_screen",
        "extract": "_execute_extract",
        "synthesize": "_execute_synthesize",
        "export": "_execute_export",
        "verify": "_execute_verify",
    }
    executor = StepExecutor(connectors={}, providers={})
    called: list[str] = []
    for step_type, handler in StepExecutor._LEGACY_HANDLER_NAMES.items():

        async def fake(step_def: Any, context: Any, name: str = handler) -> StepResult:
            called.append(name)
            return StepResult(output={})

        monkeypatch.setattr(executor, handler, fake)
        await executor.execute({"type": step_type, "params": {}}, {})
    assert called == list(StepExecutor._LEGACY_HANDLER_NAMES.values())
    with pytest.raises(ValueError, match="Unknown step type"):
        await executor.execute({"type": "analyse"}, {})
