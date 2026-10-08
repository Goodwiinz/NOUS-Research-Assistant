#!/usr/bin/env python3
"""Keep docs/engineering/feature-map.yaml honest (nous-verify upkeep).

Fails (exit 1) when:

- a mapped scenario id does not exist in tests/e2e/qa/scenarios.mjs;
- a ``covered`` feature maps no scenario, or one of its flow-chart
  checkpoints (``checkpoint: <name>`` node labels in
  docs/engineering/flows/<id>.md) has no ``checkpoint('<name>')`` call in
  scenarios.mjs;
- a frontend/app/**/page.tsx or a backend/src/api/**/*.py file containing
  ``APIRouter(`` is neither claimed by a feature's ``owns`` globs nor listed
  under ``ignore``;
- an ignore entry is stale (file gone) or also claimed by a feature;
- with ``--base REF``: an ignore entry is new relative to the map at
  merge-base(REF, HEAD), so the ignore lists only shrink. The growth check is
  skipped when no ``--base`` is given or the map does not exist at the merge
  base (the change that first introduces it). An unresolvable base is a
  problem, never a silent skip.

Usage:
    python3 scripts/ci/check_feature_map.py [--root DIR] [--base REF]
"""

from __future__ import annotations

import argparse
import fnmatch
import re
import subprocess
import sys
from collections.abc import Iterable
from pathlib import Path
from typing import Any

import yaml  # type: ignore[import-untyped]

FEATURE_MAP = Path("docs/engineering/feature-map.yaml")
FLOWS_DIR = Path("docs/engineering/flows")
SCENARIOS = Path("tests/e2e/qa/scenarios.mjs")
REQUIRED_KEYS = (
    "id",
    "status",
    "surfaces",
    "states",
    "pass_criteria",
    "scenarios",
    "owns",
)
STATUSES = {"planned", "covered"}
SCENARIO_ID = re.compile(
    r"^\s*\{?\s*id:\s*['\"]([a-z]+\.[a-z0-9-]+)['\"]", re.MULTILINE
)
CHECKPOINT_CALL = re.compile(r"checkpoint\(\s*['\"]([a-z0-9][a-z0-9.-]*)['\"]")
FLOW_CHECKPOINT = re.compile(r"checkpoint:\s*([a-z0-9][a-z0-9.-]*)")


def scenario_ids(source: str) -> set[str]:
    return set(SCENARIO_ID.findall(source))


def checkpoint_calls(source: str) -> set[str]:
    return set(CHECKPOINT_CALL.findall(source))


def flow_checkpoints(flow_markdown: str) -> set[str]:
    return set(FLOW_CHECKPOINT.findall(flow_markdown))


def owns(path: str, globs: Iterable[str]) -> bool:
    return any(fnmatch.fnmatchcase(path, pattern) for pattern in globs)


def check_schema(data: dict[str, Any]) -> list[str]:
    problems: list[str] = []
    if data.get("version") != 1:
        problems.append("version must be 1")
    features = data.get("features")
    if not isinstance(features, list) or not features:
        problems.append("features must be a non-empty list")
        features = []
    seen: set[str] = set()
    for feature in features:
        if not isinstance(feature, dict):
            problems.append(f"feature entry must be a mapping: {feature!r}")
            continue
        fid = str(feature.get("id", "<missing id>"))
        for key in REQUIRED_KEYS:
            if key not in feature:
                problems.append(f"{fid}: missing key {key}")
        if feature.get("status") not in STATUSES:
            problems.append(f"{fid}: status must be one of {sorted(STATUSES)}")
        if fid in seen:
            problems.append(f"duplicate feature id {fid}")
        seen.add(fid)
    ignore = data.get("ignore")
    if not isinstance(ignore, dict):
        return problems + ["ignore must be a mapping with pages and routers lists"]
    for key in ("pages", "routers"):
        if not isinstance(ignore.get(key), list):
            problems.append(f"ignore.{key} must be a list")
    return problems


def check_scenarios(features: list[dict[str, Any]], known: set[str]) -> list[str]:
    problems: list[str] = []
    for feature in features:
        ids = list(feature.get("scenarios") or [])
        if feature.get("status") == "covered" and not ids:
            problems.append(f"{feature['id']}: covered feature maps no scenario")
        for sid in ids:
            if sid not in known:
                problems.append(f"{feature['id']}: unknown scenario id {sid}")
    return problems


def check_checkpoints(
    features: list[dict[str, Any]], flows: dict[str, str], calls: set[str]
) -> list[str]:
    problems: list[str] = []
    for feature in features:
        fid = feature["id"]
        flow = flows.get(fid)
        if flow is None:
            problems.append(f"{fid}: missing flow {FLOWS_DIR / (fid + '.md')}")
            continue
        if feature.get("status") != "covered":
            continue
        for name in sorted(flow_checkpoints(flow)):
            if name not in calls:
                problems.append(
                    f"{fid}: flow checkpoint {name} has no checkpoint() call"
                )
    return problems


def check_coverage(
    features: list[dict[str, Any]],
    pages: set[str],
    routers: set[str],
    ignore: dict[str, list[str]],
) -> list[str]:
    problems: list[str] = []
    claimed_by: dict[str, str] = {}
    for path in sorted(pages | routers):
        for feature in features:
            if owns(path, feature.get("owns") or []):
                claimed_by[path] = feature["id"]
                break
    for kind, universe, key in (
        ("page", pages, "pages"),
        ("router", routers, "routers"),
    ):
        ignored = list(ignore.get(key) or [])
        for path in sorted(universe):
            if path not in claimed_by and path not in ignored:
                problems.append(
                    f"unclaimed {kind} {path} (claim it in feature-map.yaml or add it to ignore.{key})"
                )
        for entry in ignored:
            if entry not in universe:
                problems.append(f"stale ignore entry {entry}")
            elif entry in claimed_by:
                problems.append(
                    f"ignore entry {entry} is also claimed by {claimed_by[entry]} (remove it)"
                )
    return problems


def check_ignore_growth(
    head_ignore: dict[str, list[str]], base_ignore: dict[str, list[str]]
) -> list[str]:
    problems: list[str] = []
    for key in ("pages", "routers"):
        before = set(base_ignore.get(key) or [])
        for entry in head_ignore.get(key) or []:
            if entry not in before:
                problems.append(
                    f"new ignore entry {entry} (claim it in a feature instead)"
                )
    return problems


class BaseRefError(RuntimeError):
    """The --base ref (or its merge-base with HEAD) cannot be resolved."""


def base_ignore(root: Path, base: str) -> dict[str, list[str]] | None:
    """Return the ignore lists at merge-base(base, HEAD), or None if absent."""
    merge_base = subprocess.run(
        ["git", "-C", str(root), "merge-base", base, "HEAD"],
        capture_output=True,
        text=True,
    )
    if merge_base.returncode != 0:
        raise BaseRefError(f"cannot resolve merge-base({base}, HEAD)")
    shown = subprocess.run(
        [
            "git",
            "-C",
            str(root),
            "show",
            f"{merge_base.stdout.strip()}:{FEATURE_MAP.as_posix()}",
        ],
        capture_output=True,
        text=True,
    )
    if shown.returncode != 0:
        return None
    data = yaml.safe_load(shown.stdout) or {}
    ignore = data.get("ignore") if isinstance(data, dict) else None
    if not isinstance(ignore, dict):
        return {"pages": [], "routers": []}
    return {
        "pages": list(ignore.get("pages") or []),
        "routers": list(ignore.get("routers") or []),
    }


def discover_pages(root: Path) -> set[str]:
    base = root / "frontend" / "app"
    return (
        {p.relative_to(root).as_posix() for p in base.rglob("page.tsx")}
        if base.is_dir()
        else set()
    )


def discover_routers(root: Path) -> set[str]:
    base = root / "backend" / "src" / "api"
    found: set[str] = set()
    if not base.is_dir():
        return found
    for path in base.rglob("*.py"):
        if "APIRouter(" in path.read_text(encoding="utf-8", errors="ignore"):
            found.add(path.relative_to(root).as_posix())
    return found


def run(root: Path, base: str | None = None) -> list[str]:
    data = yaml.safe_load((root / FEATURE_MAP).read_text(encoding="utf-8")) or {}
    if not isinstance(data, dict):
        return [f"{FEATURE_MAP} must be a YAML mapping"]
    problems = check_schema(data)
    if problems:
        return problems
    features = list(data["features"])
    source = (root / SCENARIOS).read_text(encoding="utf-8")
    flows = {
        f["id"]: (root / FLOWS_DIR / f"{f['id']}.md").read_text(encoding="utf-8")
        for f in features
        if (root / FLOWS_DIR / f"{f['id']}.md").is_file()
    }
    problems += check_scenarios(features, scenario_ids(source))
    problems += check_checkpoints(features, flows, checkpoint_calls(source))
    problems += check_coverage(
        features, discover_pages(root), discover_routers(root), data["ignore"]
    )
    if base:
        try:
            before = base_ignore(root, base)
        except BaseRefError as exc:
            return problems + [str(exc)]
        if before is None:
            print(f"feature-map: no {FEATURE_MAP} at merge-base; growth check skipped")
        else:
            problems += check_ignore_growth(data["ignore"], before)
    return problems


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--root", type=Path, default=Path(__file__).resolve().parents[2]
    )
    parser.add_argument(
        "--base",
        help="git ref; ignore entries absent at merge-base(REF, HEAD) fail",
    )
    args = parser.parse_args(argv)
    problems = run(args.root, args.base)
    for problem in problems:
        print(f"feature-map: {problem}")
    if problems:
        print(f"feature-map: {len(problems)} problem(s)")
        return 1
    print("feature-map: ok")
    return 0


if __name__ == "__main__":
    sys.exit(main())
