"""Prepare the declared bridge revision and current discovery task for Harbor.

Run Harbor from the printed output directory. The image then copies the pinned
bridge/lockfile rather than whichever source happens to be checked out today.
The destination must be new; no existing checkout or trial data is overwritten.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import shutil
import subprocess
import tarfile
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TASK = ROOT / "evals/harness-mcp-discovery-v1"
SOURCE_PATHS = [
    "package.json",
    "pnpm-workspace.yaml",
    "pnpm-lock.yaml",
    "frontend/package.json",
    "tests/e2e/package.json",
    "terminal/package.json",
    "packages/chat-runtime/package.json",
    "packages/harness-bridge",
    "evals/harbor_common",
]


def prepare(destination: Path) -> Path:
    """Materialize only the image's declared source inputs and current task."""
    metadata = tomllib.loads((TASK / "task.toml").read_text())["metadata"]
    revision = metadata["agent_revision"]
    archive = subprocess.check_output(
        ["git", "archive", revision, "--", *SOURCE_PATHS], cwd=ROOT
    )
    destination = destination.resolve()
    destination.mkdir(mode=0o700, parents=True, exist_ok=False)
    with tarfile.open(fileobj=io.BytesIO(archive)) as source:
        source.extractall(destination, filter="data")
    ignored = [
        line.strip().rstrip("/")
        for line in (TASK / ".gitignore").read_text().splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]
    shutil.copytree(
        TASK,
        destination / "evals/harness-mcp-discovery-v1",
        ignore=shutil.ignore_patterns(*ignored),
    )
    inputs = {
        str(path.relative_to(destination)): hashlib.sha256(
            path.read_bytes()
        ).hexdigest()
        for path in sorted(destination.rglob("*"))
        if path.is_file()
    }
    (destination / "source-inputs.json").write_text(
        json.dumps({"agent_revision": revision, "files": inputs}, indent=2) + "\n"
    )
    return destination


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    print(prepare(args.output))


if __name__ == "__main__":
    main()
