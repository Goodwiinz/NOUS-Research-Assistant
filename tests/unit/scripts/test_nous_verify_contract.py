"""Static contracts for the user-level verification gate (nous-verify)."""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
CONTRACT = REPO_ROOT / "docs" / "engineering" / "verification.md"
FEATURE_MAP = REPO_ROOT / "docs" / "engineering" / "feature-map.yaml"
FLOWS = REPO_ROOT / "docs" / "engineering" / "flows"


def test_verification_contract_is_canonical_and_discoverable() -> None:
    text = CONTRACT.read_text(encoding="utf-8")
    index = (REPO_ROOT / "docs" / "engineering" / "README.md").read_text(
        encoding="utf-8"
    )

    assert text.startswith("# User-level verification (nous-verify)\n")
    assert "[verification.md](verification.md)" in index
    for result in ("`PASS`", "`FAILED`", "`BLOCKED`", "`NOT RUN`"):
        assert result in text
    assert "docs/engineering/feature-map.yaml" in text
    assert "docs/engineering/flows/" in text
    assert "scripts/ci/check_feature_map.py" in text
    assert "docs/testing/evidence/verify-" in text
    assert ".verify-artifacts/" in text


def test_verification_contract_is_runtime_neutral() -> None:
    text = CONTRACT.read_text(encoding="utf-8")
    for term in ("Claude", "Codex", "Fable", "Opus"):
        assert not re.search(rf"\b{term}\b", text)


def test_feature_map_and_flows_exist_for_v1_features() -> None:
    assert FEATURE_MAP.is_file()
    for feature in (
        "login",
        "chat-send-stream-reload",
        "hitl-approve-deny",
        "document-upload",
        "project-creation-via-chat",
    ):
        assert (FLOWS / f"{feature}.md").is_file(), feature


def test_qa_runner_node_tests_are_wired_into_hosted_and_local_gates() -> None:
    workflow = (REPO_ROOT / ".github" / "workflows" / "test-pipeline.yml").read_text(
        encoding="utf-8"
    )
    local_ci = (REPO_ROOT / "scripts" / "ci" / "run_local_ci.sh").read_text(
        encoding="utf-8"
    )
    assert (
        "node --test ../tests/unit/scripts/nous-qa.test.mjs "
        "../tests/unit/scripts/nous-verify.test.mjs" in workflow
    )
    assert (
        "node --test tests/unit/scripts/nous-qa.test.mjs "
        'tests/unit/scripts/nous-verify.test.mjs; check $? "nous-qa node tests"'
        in local_ci
    )


def test_qa_readme_documents_verification_flags() -> None:
    readme = (REPO_ROOT / "tests" / "e2e" / "qa" / "README.md").read_text(
        encoding="utf-8"
    )
    assert "## Verification selection and evidence" in readme
    for flag in ("--features", "--changed-from", "--evidence-dir", "--evidence-record"):
        assert flag in readme


def test_nous_loop_has_the_user_level_stop_gate() -> None:
    loop = (REPO_ROOT / "docs" / "engineering" / "nous-loop.md").read_text(
        encoding="utf-8"
    )
    normalized = " ".join(loop.split())
    step7 = loop.index("### 7. Run local gates")
    step7b = loop.index("### 7b. Prove it as a user")
    step8 = loop.index("### 8. Publish, recheck, and close")
    assert step7 < step7b < step8
    gate = " ".join(loop[step7b:step8].split())

    assert "[verification.md](verification.md)" in gate
    assert "--changed-from origin/develop" in gate
    assert "cannot be `merged` without a local `PASS`" in gate
    assert "`BLOCKED` or `NOT RUN` ends the tick as `ready-for-human`" in gate
    assert "checkpoints/" in gate
    assert "trace" in gate
    assert "--expected-backend-sha" in normalized
    assert "files a regression" in normalized
    assert "https://goodwiinz.tech" in normalized


def test_verification_contract_marks_the_stop_gate_live() -> None:
    text = " ".join(CONTRACT.read_text(encoding="utf-8").split())
    assert "step 7b stop gate is still planned" not in text
    assert "(planned)" not in text
    assert "Neither step exists" not in text
    assert "step 7b" in text
