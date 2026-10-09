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


def _loop_text() -> str:
    return (REPO_ROOT / "docs" / "engineering" / "nous-loop.md").read_text(
        encoding="utf-8"
    )


def _section(text: str, start: str, end: str) -> str:
    return text[text.index(start) : text.index(end)]


def _bash_block(text: str) -> str:
    match = re.search(r"```bash\n(.*?)```", text, re.S)
    assert match, "no bash block"
    return match.group(1)


def test_nous_loop_has_the_user_level_stop_gate() -> None:
    loop = _loop_text()
    step7 = loop.index("### 7. Run local gates")
    step7b = loop.index("### 7b. Prove it as a user")
    step8 = loop.index("### 8. Publish, recheck, and close")
    assert step7 < step7b < step8
    raw_gate = loop[step7b:step8]
    gate = " ".join(raw_gate.split())

    assert "[verification.md](verification.md)" in gate
    assert "--changed-from origin/develop" in gate
    assert "--allow-writes" in _bash_block(raw_gate)
    # Real agent turns outlast the runner's 30 s default.
    assert "--timeout-ms 180000" in _bash_block(raw_gate)
    assert "cannot be `merged` without a local `PASS`" in gate
    assert "`BLOCKED` or `NOT RUN` ends the tick as `ready-for-human`" in gate
    assert "checkpoints/" in gate
    assert "`trace*.zip`" in gate
    assert "is not a local `PASS`" in gate
    assert "verify-<first-feature>-<YYYYMMDD>" in gate
    assert "lists every selected feature" in gate


def test_nous_loop_post_deploy_rerun_has_an_owner() -> None:
    loop = _loop_text()
    step8 = " ".join(
        _section(loop, "### 8. Publish, recheck, and close", "## Guardrails").split()
    )
    assert "--expected-backend-sha" in step8
    assert "https://goodwiinz.tech" in step8
    assert "files a regression" in step8
    assert "step 1" in step8
    step1 = " ".join(
        _section(
            loop, "### 1. Reconcile prior work", "### 2. Pick one candidate"
        ).split()
    )
    assert "post-deploy rerun" in step1
    assert "merged-but-unverified" in step1


def test_nous_verify_command_is_a_thin_adapter() -> None:
    command = (REPO_ROOT / ".claude" / "commands" / "nous-verify.md").read_text(
        encoding="utf-8"
    )
    assert command.startswith("---\ndescription: ")
    assert "docs/engineering/verification.md" in command
    assert re.search(
        r"read .*docs/engineering/verification\.md.* completely", command, re.I
    )
    assert re.search(r"canonical", command, re.I)
    assert re.search(r"(?:may|must) not weaken|cannot weaken", command, re.I)
    assert "`trace*.zip`" in command
    assert len(command.splitlines()) <= 60
    for term in ("Fable", "Opus", "Sonnet", "Haiku"):
        assert not re.search(rf"\b{term}\b", command)
    assert not re.search(r"\b20\d{2}-\d{2}-\d{2}\b", command)

    step7b = _section(
        _loop_text(), "### 7b. Prove it as a user", "### 8. Publish, recheck, and close"
    )
    assert _bash_block(command) == _bash_block(step7b)

    loop_adapter = (REPO_ROOT / ".claude" / "commands" / "nous-loop.md").read_text(
        encoding="utf-8"
    )
    assert "/nous-verify" in loop_adapter
    assert len(loop_adapter.splitlines()) <= 60


def test_verification_contract_marks_the_stop_gate_live() -> None:
    text = " ".join(CONTRACT.read_text(encoding="utf-8").split())
    assert "step 7b stop gate is still planned" not in text
    assert "(planned)" not in text
    assert "Neither step exists" not in text
    assert "step 7b" in text
