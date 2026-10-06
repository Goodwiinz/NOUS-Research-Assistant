"""GOO-312 run-manifest v2 rules (stdlib, no I/O).

Mutation verification (docs/engineering/testing.md), full record in
``docs/testing/agent-orchestration-mutation-checks.md`` (GOO-312 section):

- ``completeness`` returning ``("complete", [])``:
  ``-k completeness`` fails (missing paths not listed);
- ``legacy_view`` copying an ``environment`` default: ``-k legacy`` fails.
"""

import hashlib
from typing import Any

import pytest

from src.services.research_engine import contracts
from src.services.research_engine import manifest_rules as rules

pytestmark = pytest.mark.unit

CODE = "import json\nprint('hi')\n"
DOC = "6f1b7a52-36d7-4a5f-9d3a-3c3b8b0a1e11"


def _sha(data: str | bytes) -> str:
    raw = data.encode("utf-8") if isinstance(data, str) else data
    return hashlib.sha256(raw).hexdigest()


def _params(**override: Any) -> dict[str, Any]:
    params: dict[str, Any] = {
        "code": CODE,
        "code_sha256": _sha(CODE),
        "command": "python main.py",
        "parameters": {"alpha": 0.05},
        "seed": 20260930,
        "inputs": [
            {"name": "data.csv", "kind": "document", "id": DOC, "sha256": "a" * 64}
        ],
        "outputs": [
            {"name": "figure.svg", "media_type": "image/svg+xml", "role": "figure"},
            {
                "name": "metrics.json",
                "media_type": "application/json",
                "role": "metrics",
            },
        ],
        "requirements": ["matplotlib==3.9.2", "numpy==2.1.1"],
        "template": "code-interpreter-v1",
    }
    return params | override


def _manifest(
    output_bytes: bytes = b"<svg/>", status: str = "completed"
) -> dict[str, Any]:
    spec = rules.parse_analyze_step(_params())
    return rules.build(
        run={
            "run_id": "r1",
            "effective_plan_hash": "p" * 64,
            "blueprint_id": "b1",
            "blueprint_version": 1,
            "step_index": 0,
            "started_at": "2026-09-30T10:00:00+00:00",
            "completed_at": "2026-09-30T10:01:00+00:00",
        },
        protocol={"id": "pv1", "content_hash": "c" * 64},
        question={"id": "qv1", "hypothesis": "Exercise lowers GDS-15."},
        spec=spec,
        code={"sha256": _sha(CODE), "artifact_id": "code-1"},
        environment={
            "provider": "e2b",
            "template_id": "tmpl",
            "sandbox_id": "sbx",
            "python": "Python 3.11.9",
            "os_release_sha256": "o" * 64,
            "lock_sha256": "l" * 64,
            "lock_artifact_id": "lock-1",
            "image_digest": rules.NO_DIGEST,
        },
        inputs=[
            {
                "name": "data.csv",
                "kind": "document",
                "ref_id": DOC,
                "sha256": "a" * 64,
                "byte_size": 10,
                "artifact_id": "in-1",
            }
        ],
        outputs=[
            {
                "name": "figure.svg",
                "role": "figure",
                "media_type": "image/svg+xml",
                "sha256": _sha(output_bytes),
                "byte_size": len(output_bytes),
                "artifact_id": "out-1",
            }
        ],
        metrics={"n": 3},
        status=status,
        deviation_ids=[],
    )


def test_parse_rejects_unpinned_requirement() -> None:
    for entry in ("numpy", "numpy>=2", "numpy==", "git+https://x/y.git", "-e ."):
        with pytest.raises(ValueError, match="^unpinned_requirement$"):
            rules.parse_analyze_step(_params(requirements=[entry]))
    spec = rules.parse_analyze_step(_params(requirements=["pandas[excel]==2.2.3"]))
    assert spec.requirements == ("pandas[excel]==2.2.3",)


def test_parse_rejects_code_hash_mismatch_and_unsafe_names() -> None:
    with pytest.raises(ValueError, match="^code_hash_mismatch$"):
        rules.parse_analyze_step(_params(code_sha256="0" * 64))
    for name in ("../x", "a/b", ".hidden", "", "a\\b"):
        outputs = [{"name": name, "media_type": "text/plain", "role": "data"}]
        with pytest.raises(ValueError, match="^unsafe_path$"):
            rules.parse_analyze_step(_params(outputs=outputs))
    twice = [{"name": "t.csv", "media_type": "text/csv", "role": "table"}] * 2
    with pytest.raises(ValueError, match="^duplicate_output_name$"):
        rules.parse_analyze_step(_params(outputs=twice))
    url = [{"name": "d", "kind": "url", "id": DOC, "sha256": "a" * 64}]
    with pytest.raises(ValueError, match="^unsupported_input_kind$"):
        rules.parse_analyze_step(_params(inputs=url))
    with pytest.raises(ValueError, match="^missing_seed$"):
        rules.parse_analyze_step(_params(seed=None, parameters={"method": "bootstrap"}))


def test_build_is_canonical_and_hash_stable() -> None:
    first, second = _manifest(), _manifest()
    assert rules.manifest_hash(first) == rules.manifest_hash(second)
    assert rules.canonical_bytes(first) == contracts.canonical_json_bytes(first)
    assert rules.manifest_hash(first) == contracts.canonical_json_sha256(first)
    assert rules.manifest_hash(_manifest(b"<svg />")) != rules.manifest_hash(first)
    assert first["schema"] == rules.SCHEMA
    assert first["hypothesis_sha256"] == _sha("Exercise lowers GDS-15.")
    assert first["code"]["commit_verified"] is False


def test_completeness_lists_every_missing_path() -> None:
    assert rules.completeness(_manifest()) == ("complete", [])
    manifest = _manifest()
    manifest["seed"] = None
    manifest["question_version_id"] = None
    manifest["environment"] = None
    manifest["inputs"][0]["artifact_id"] = None
    manifest["outputs"][0]["sha256"] = None
    state, missing = rules.completeness(manifest)
    assert state == "incomplete"
    assert missing == [
        "environment.lock_sha256",
        "environment.template_id",
        "inputs[0].artifact_id",
        "outputs[0].sha256",
        "question_version_id",
        "seed",
    ]


def test_failed_run_is_incomplete_even_with_all_hashes() -> None:
    assert rules.completeness(_manifest(status="failed")) == ("incomplete", ["status"])


def test_legacy_view_invents_nothing() -> None:
    view = rules.legacy_view({"run_status": "completed", "parameters": {}})
    assert view["schema"] == "nous.run-manifest/1"
    assert view["completeness"] == "incomplete"
    assert view["missing"] == ["schema_version<2"]
    assert not {"code", "environment", "inputs", "outputs"} & set(view)
    assert view["legacy"] == {"run_status": "completed", "parameters": {}}


def test_image_digest_null_does_not_block_completeness() -> None:
    manifest = _manifest()
    assert manifest["environment"]["image_digest"]["value"] is None
    assert rules.completeness(manifest)[0] == "complete"


def test_assert_no_secrets_rejects_key_value_and_patterns() -> None:
    manifest = _manifest()
    rules.assert_no_secrets(manifest, secret_values=["e2b_live_0123456789"])
    rules.assert_no_secrets(
        {"lock": "tiktoken==0.7.0\npython-secret==1.0\n"}, secret_values=[]
    )
    for leak in (
        "E2B_API_KEY=" + "e2b_live_" + "0123456789",  # split so scanners see no token
        "x e2b_live_0123456789 y",
        "password = hunter2",
        "API-KEY=abc",
    ):
        with pytest.raises(ValueError, match="^manifest_secret_detected$"):
            rules.assert_no_secrets(
                {"environment": {"python": leak}},
                secret_values=["e2b_live_0123456789"],
            )
