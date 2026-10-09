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
