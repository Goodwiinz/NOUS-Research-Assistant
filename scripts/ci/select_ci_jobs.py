#!/usr/bin/env python3
"""Select PR checks from the complete Git diff; other events keep full CI.

Unknown paths, shared configuration and empty diffs choose full verification.
Git failures block selection. NUL-separated names and disabled rename detection
preserve deleted paths and both sides of a rename without API file-count limits.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path, PurePosixPath

from assert_required_jobs import profile_outputs

ROOT_DOCS = {
    "README.md",
    "AGENTS.md",
    "CLAUDE.md",
    "CONTRIBUTING.md",
    "CHANGELOG.md",
    "CODE_OF_CONDUCT.md",
    "SECURITY.md",
}
DOC_TREES = ("docs/", ".claude/commands/")
DOC_OWNERS = (
    "backend/",
    "frontend/",
    "scripts/",
    "tests/",
    ".github/",
    "infrastructure/",
    "terminal/",
    "packages/",
)
DIRECTORY_DOCS = {"README.md", "doc.md", "AGENTS.md"}
# Dependencies/build/test configuration can affect consumers beyond one surface.
SHARED_NAMES = {
    "package.json",
    "pyproject.toml",
    "setup.py",
    "setup.cfg",
    "tox.ini",
    "pytest.ini",
    "pnpm-workspace.yaml",
    ".npmrc",
    ".nvmrc",
    ".python-version",
    ".pre-commit-config.yaml",
}


def path_scope(name: str) -> str:
    """Use known ownership, never diff size or a blanket Markdown exclusion."""
    path = PurePosixPath(name)
    if path.is_absolute() or ".." in path.parts:
        return "full"
    # Both generated halves share the OpenAPI regeneration/freshness gate.
    if name == "backend/openapi.json" or name.startswith(
        "frontend/src/types/generated/"
    ):
        return "full"
    if (
        path.name in SHARED_NAMES
        or path.name.startswith(("requirements", "constraints", "Dockerfile"))
        or path.name.endswith(
            (".lock", ".lockb", "-lock.yaml", "-lock.yml", "-lock.json")
        )
        or path.name.startswith(("docker-compose", "compose."))
    ):
        return "full"
    # Runtime Markdown (including README-shaped assets) stays code-scoped.
    if name.startswith(("backend/src/", "frontend/src/")):
        return name.split("/", 1)[0]
    # Agent backend tests assert this guide's supported limits and relative links.
    if name == "docs/operations/agent-supported-workflows.md":
        return "backend"
    if (
        name in ROOT_DOCS
        or (name.endswith(".md") and name.startswith(DOC_TREES))
        or (path.name in DIRECTORY_DOCS and name.startswith(DOC_OWNERS))
    ):
        return "docs"
    # Configuration, infrastructure and unknown directories retain the full gate.
    if name.startswith(("backend/docker/", "backend/config/")):
        return "full"
    if name.startswith(("backend/", "frontend/")):
        return name.split("/", 1)[0]
    return "full"


def select_profile(paths: Sequence[str]) -> str:
    scopes = {path_scope(path) for path in paths} - {"docs"}
    if not paths or "full" in scopes or len(scopes) > 1:
        return "full"
    return next(iter(scopes)) if scopes else "docs"


def _git(*args: str) -> bytes:
    return subprocess.run(
        ["git", *args], check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE
    ).stdout


def changed_paths(base: str, head: str) -> list[str]:
    # Resolve caller-controlled refs before diffing; they can never become options.
    base_sha = (
        _git("rev-parse", "--verify", "--end-of-options", f"{base}^{{commit}}")
        .decode()
        .strip()
    )
    head_sha = (
        _git("rev-parse", "--verify", "--end-of-options", f"{head}^{{commit}}")
        .decode()
        .strip()
    )
    merge_base = _git("merge-base", base_sha, head_sha).decode().strip()
    diff = _git("diff", "--name-only", "-z", "--no-renames", merge_base, head_sha, "--")
    return [os.fsdecode(path) for path in diff.split(b"\0") if path]


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--event-name", required=True)
    parser.add_argument("--base")
    parser.add_argument("--head", default="HEAD")
    parser.add_argument("--head-branch", default="")
    parser.add_argument("--output-file", type=Path)
    args = parser.parse_args(argv)

    profile = "full"
    paths: list[str] = []
    release_proposal = re.fullmatch(r"codex/release-dev-[0-9a-f]{40}", args.head_branch)
    if args.event_name == "pull_request" and not release_proposal:
        if not args.base:
            print(
                "CI selection blocked: pull requests require a base ref.",
                file=sys.stderr,
            )
            return 1
        try:
            paths = changed_paths(args.base, args.head)
        except (OSError, subprocess.CalledProcessError):
            print(
                "CI selection blocked: the complete PR diff could not be read.",
                file=sys.stderr,
            )
            return 1
        profile = select_profile(paths)

    outputs = profile_outputs(profile)
    if args.output_file is not None:
        with args.output_file.open("a", encoding="utf-8") as handle:
            for key, value in outputs.items():
                handle.write(f"{key}={value}\n")
    print(json.dumps({**outputs, "changed_files": len(paths)}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
