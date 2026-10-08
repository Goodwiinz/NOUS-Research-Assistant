"""Contracts for scripts/ci/check_feature_map.py (nous-verify upkeep)."""

from __future__ import annotations

import importlib.util
import subprocess
from pathlib import Path
from types import ModuleType
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[4]
SCRIPT = REPO_ROOT / "scripts" / "ci" / "check_feature_map.py"


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "check_feature_map_under_test", SCRIPT
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


cfm = _load()

SCENARIOS_SRC = """
const scenarios = [
  { id: 'smoke.login-availability', async run(session, evidence) {
      await evidence.checkpoint('login.form');
  } },
  { id: 'workflow.reload-persistence', async run() {} },
];
"""

FLOW_LOGIN = """# Flow: login
```mermaid
flowchart TD
  A --> B["checkpoint: login.form"]
  B --> C["checkpoint: login.landed"]
```
"""


def _feature(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "id": "login",
        "status": "covered",
        "surfaces": {"web": ["/login"], "api": [], "cli": []},
        "states": ["done"],
        "pass_criteria": ["renders"],
        "scenarios": ["smoke.login-availability"],
        "owns": ["frontend/app/(auth)/login/**"],
    }
    base.update(overrides)
    return base


def test_scenario_ids_are_parsed_from_scenarios_source() -> None:
    assert cfm.scenario_ids(SCENARIOS_SRC) == {
        "smoke.login-availability",
        "workflow.reload-persistence",
    }


def test_checkpoint_calls_and_flow_checkpoints_are_parsed() -> None:
    assert cfm.checkpoint_calls(SCENARIOS_SRC) == {"login.form"}
    assert cfm.flow_checkpoints(FLOW_LOGIN) == {"login.form", "login.landed"}


def test_unknown_scenario_id_is_a_problem() -> None:
    problems = cfm.check_scenarios(
        [_feature(scenarios=["workflow.does-not-exist"])], {"smoke.login-availability"}
    )
    assert problems == ["login: unknown scenario id workflow.does-not-exist"]


def test_covered_feature_requires_every_flow_checkpoint_to_be_called() -> None:
    problems = cfm.check_checkpoints(
        [_feature()], {"login": FLOW_LOGIN}, {"login.form"}
    )
    assert problems == ["login: flow checkpoint login.landed has no checkpoint() call"]


def test_planned_feature_skips_checkpoint_rule_but_needs_a_flow() -> None:
    assert (
        cfm.check_checkpoints(
            [_feature(status="planned")], {"login": FLOW_LOGIN}, set()
        )
        == []
    )
    assert cfm.check_checkpoints([_feature(status="planned")], {}, set()) == [
        "login: missing flow docs/engineering/flows/login.md"
    ]


def test_covered_feature_must_map_at_least_one_scenario() -> None:
    assert cfm.check_scenarios([_feature(scenarios=[])], set()) == [
        "login: covered feature maps no scenario"
    ]


def test_glob_matches_with_fnmatch_semantics() -> None:
    assert cfm.owns(
        "frontend/app/(auth)/login/page.tsx", ["frontend/app/(auth)/login/**"]
    )
    assert cfm.owns("backend/src/api/agent/execute.py", ["backend/src/api/agent/**"])
    assert not cfm.owns("frontend/app/page.tsx", ["frontend/app/(auth)/login/**"])


def test_unclaimed_page_or_router_must_be_ignored_and_ignores_stay_current() -> None:
    features = [_feature()]
    pages = {"frontend/app/(auth)/login/page.tsx", "frontend/app/page.tsx"}
    routers = {"backend/src/api/agent/execute.py"}
    ignore = {"pages": ["frontend/app/page.tsx"], "routers": []}
    assert cfm.check_coverage(features, pages, routers, ignore) == [
        "unclaimed router backend/src/api/agent/execute.py (claim it in feature-map.yaml or add it to ignore.routers)"
    ]
    stale = {
        "pages": ["frontend/app/page.tsx", "frontend/app/gone/page.tsx"],
        "routers": [],
    }
    assert "stale ignore entry frontend/app/gone/page.tsx" in cfm.check_coverage(
        features, pages, set(), stale
    )
    claimed = {
        "pages": ["frontend/app/(auth)/login/page.tsx", "frontend/app/page.tsx"],
        "routers": [],
    }
    assert (
        "ignore entry frontend/app/(auth)/login/page.tsx is also claimed by login (remove it)"
        in cfm.check_coverage(features, pages, set(), claimed)
    )


def test_schema_rejects_missing_keys_and_duplicate_ids() -> None:
    bad = {
        "version": 1,
        "features": [_feature(), _feature()],
        "ignore": {"pages": [], "routers": []},
    }
    assert "duplicate feature id login" in cfm.check_schema(bad)
    missing = {
        "version": 1,
        "features": [{"id": "x"}],
        "ignore": {"pages": [], "routers": []},
    }
    assert any(p.startswith("x: missing key") for p in cfm.check_schema(missing))


def test_real_feature_map_is_clean() -> None:
    assert cfm.main([]) == 0


def test_router_discovery_only_counts_python_files_with_apirouter(
    tmp_path: Path,
) -> None:
    api = tmp_path / "backend" / "src" / "api"
    (api / "x").mkdir(parents=True)
    (api / "x" / "r.py").write_text("router = APIRouter()\n", encoding="utf-8")
    (api / "x" / "README.md").write_text("APIRouter(\n", encoding="utf-8")
    (api / "x" / "plain.py").write_text("x = 1\n", encoding="utf-8")
    assert cfm.discover_routers(tmp_path) == {"backend/src/api/x/r.py"}


def test_feature_map_check_is_wired_into_hosted_and_local_gates() -> None:
    workflow = (REPO_ROOT / ".github" / "workflows" / "test-pipeline.yml").read_text(
        encoding="utf-8"
    )
    local_ci = (REPO_ROOT / "scripts" / "ci" / "run_local_ci.sh").read_text(
        encoding="utf-8"
    )
    lightweight = workflow[
        workflow.index("  lightweight-checks:") : workflow.index("  lint-backend:")
    ]
    assert "python3 scripts/ci/check_feature_map.py" in lightweight
    assert "backend/tests/unit/ci/test_check_feature_map.py" in lightweight
    assert 'python3 scripts/ci/check_feature_map.py --base "$BASE"' in lightweight
    assert "fetch-depth: 0" in lightweight
    assert 'step "Feature map (blocking)"' in local_ci
    assert (
        '"$PY" scripts/ci/check_feature_map.py --base "$BASE"; '
        'check $? "check_feature_map"'
    ) in local_ci


def test_ignore_lists_only_shrink_against_the_base_map() -> None:
    base = {"pages": ["frontend/app/a/page.tsx"], "routers": ["backend/src/api/r.py"]}
    head = {
        "pages": ["frontend/app/a/page.tsx", "frontend/app/b/page.tsx"],
        "routers": [],
    }
    assert cfm.check_ignore_growth(head, base) == [
        "new ignore entry frontend/app/b/page.tsx (claim it in a feature instead)"
    ]
    assert cfm.check_ignore_growth(base, base) == []
    assert cfm.check_ignore_growth({"pages": [], "routers": []}, base) == []


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args], check=True, capture_output=True, text=True
    ).stdout.strip()


def test_base_ignore_is_read_from_merge_base_and_absent_map_skips(
    tmp_path: Path,
) -> None:
    _git(tmp_path, "init", "-q", "-b", "main")
    _git(tmp_path, "config", "user.email", "t@example.com")
    _git(tmp_path, "config", "user.name", "t")
    (tmp_path / "README").write_text("x\n", encoding="utf-8")
    _git(tmp_path, "add", "README")
    _git(tmp_path, "commit", "-q", "-m", "init")
    assert cfm.base_ignore(tmp_path, "main") is None

    target = tmp_path / "docs" / "engineering" / "feature-map.yaml"
    target.parent.mkdir(parents=True)
    target.write_text(
        "version: 1\nignore:\n  pages: [frontend/app/a/page.tsx]\n  routers: []\n",
        encoding="utf-8",
    )
    _git(tmp_path, "add", "docs")
    _git(tmp_path, "commit", "-q", "-m", "map")
    _git(tmp_path, "checkout", "-q", "-b", "feature")
    target.write_text(
        "version: 1\nignore:\n  pages: []\n  routers: []\n", encoding="utf-8"
    )
    _git(tmp_path, "commit", "-qam", "shrink")
    assert cfm.base_ignore(tmp_path, "main") == {
        "pages": ["frontend/app/a/page.tsx"],
        "routers": [],
    }


def test_unresolvable_base_is_a_problem_not_a_skip(tmp_path: Path) -> None:
    _git(tmp_path, "init", "-q")
    try:
        cfm.base_ignore(tmp_path, "no-such-ref")
    except cfm.BaseRefError as exc:
        assert "no-such-ref" in str(exc)
    else:  # pragma: no cover - the assertion below documents the contract
        raise AssertionError("expected BaseRefError")


def _write(root: Path, rel: str, text: str) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _mini_map(ignored_pages: list[str]) -> str:
    pages = "".join(f'    - "{p}"\n' for p in ignored_pages)
    return (
        "version: 1\n"
        "features:\n"
        "  - id: login\n"
        "    status: planned\n"
        "    surfaces: {web: [/login], api: [], cli: []}\n"
        "    states: [done]\n"
        "    pass_criteria: [renders]\n"
        "    scenarios: [smoke.login-availability]\n"
        '    owns: ["frontend/app/login/**"]\n'
        "ignore:\n"
        "  pages:\n" + pages + "  routers: []\n"
    )


def test_main_fails_when_head_grows_the_ignore_list_against_base(
    tmp_path: Path,
) -> None:
    _git(tmp_path, "init", "-q", "-b", "main")
    _git(tmp_path, "config", "user.email", "t@example.com")
    _git(tmp_path, "config", "user.name", "t")
    _write(tmp_path, "tests/e2e/qa/scenarios.mjs", SCENARIOS_SRC)
    _write(tmp_path, "docs/engineering/flows/login.md", FLOW_LOGIN)
    _write(tmp_path, "frontend/app/login/page.tsx", "export {}\n")
    _write(tmp_path, "frontend/app/a/page.tsx", "export {}\n")
    _write(
        tmp_path,
        "docs/engineering/feature-map.yaml",
        _mini_map(["frontend/app/a/page.tsx"]),
    )
    _git(tmp_path, "add", ".")
    _git(tmp_path, "commit", "-q", "-m", "base map")
    assert cfm.main(["--root", str(tmp_path), "--base", "main"]) == 0

    _git(tmp_path, "checkout", "-q", "-b", "feature")
    _write(tmp_path, "frontend/app/b/page.tsx", "export {}\n")
    _write(
        tmp_path,
        "docs/engineering/feature-map.yaml",
        _mini_map(["frontend/app/a/page.tsx", "frontend/app/b/page.tsx"]),
    )
    _git(tmp_path, "add", ".")
    _git(tmp_path, "commit", "-q", "-m", "grow ignore")
    assert cfm.main(["--root", str(tmp_path)]) == 0
    assert cfm.main(["--root", str(tmp_path), "--base", "main"]) == 1


def _schema(feature: dict[str, Any]) -> list[str]:
    return cfm.check_schema(
        {
            "version": 1,
            "features": [feature],
            "ignore": {"pages": [], "routers": []},
        }
    )


def test_schema_requires_list_of_string_fields_and_mapping_surfaces() -> None:
    assert _schema(_feature()) == []
    for key in ("scenarios", "owns", "states", "pass_criteria"):
        assert f"login: {key} must be a list of strings" in _schema(
            _feature(**{key: "smoke.login-availability"})
        ), key
        assert f"login: {key} must be a list of strings" in _schema(
            _feature(**{key: [1]})
        ), key
    assert "login: surfaces must be a mapping" in _schema(_feature(surfaces=["/x"]))


def test_schema_rejects_feature_ids_that_are_not_safe_file_stems() -> None:
    for bad in ("../etc", "Login", "a/b", "", "a.b"):
        assert any(
            p.endswith("id must match ^[a-z0-9-]+$") for p in _schema(_feature(id=bad))
        ), bad


def test_schema_requires_ignore_entries_to_be_strings() -> None:
    problems = cfm.check_schema(
        {
            "version": 1,
            "features": [_feature()],
            "ignore": {"pages": [1], "routers": []},
        }
    )
    assert "ignore.pages must be a list of strings" in problems


def test_base_starting_with_a_dash_is_rejected_before_git_runs(
    tmp_path: Path, monkeypatch: Any
) -> None:
    def boom(*_args: Any, **_kwargs: Any) -> None:
        raise AssertionError("git must not run for an option-like base")

    monkeypatch.setattr(cfm.subprocess, "run", boom)
    try:
        cfm.base_ignore(tmp_path, "--output=/tmp/x")
    except cfm.BaseRefError as exc:
        assert "--output=/tmp/x" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("expected BaseRefError")


def test_unreadable_map_at_base_fails_closed(tmp_path: Path, monkeypatch: Any) -> None:
    def fake_run(cmd: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        if "show" in cmd:
            return subprocess.CompletedProcess(cmd, 128, "", "fatal: boom")
        return subprocess.CompletedProcess(cmd, 0, "abc123\n", "")

    monkeypatch.setattr(cfm.subprocess, "run", fake_run)
    try:
        cfm.base_ignore(tmp_path, "main")
    except cfm.BaseRefError as exc:
        assert "cannot read" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("expected BaseRefError")


def test_missing_map_or_scenarios_is_a_problem_not_a_traceback(
    tmp_path: Path,
) -> None:
    assert cfm.run(tmp_path) == ["missing docs/engineering/feature-map.yaml"]
    _write(tmp_path, "docs/engineering/feature-map.yaml", _mini_map([]))
    assert cfm.run(tmp_path) == ["missing tests/e2e/qa/scenarios.mjs"]
