"""Authoritative host-side discovery check against the isolated mock container.

Run on the host from Harbor's VERIFICATION_START hook, before verifier upload
and environment cleanup. Copy answer.md into a private host snapshot first.
Agent-container reward/state files are secondary. --no-delete does not keep
Harbor 0.6.6 containers running and is not a substitute for this hook.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import re
import stat
import subprocess
from pathlib import Path
from types import ModuleType
from typing import Any

TESTS = Path(__file__).parent / "harness-mcp-discovery-v1/tests"
STATE_READER = (
    "import json,urllib.request; "
    "o=urllib.request.build_opener(urllib.request.ProxyHandler({})); "
    "r=o.open('http://127.0.0.1:8080/events',timeout=10); "
    "print(json.dumps(json.loads(r.read())))"
)


class HostVerificationUnavailable(RuntimeError):
    """Trusted host/container state was unavailable; never an objective zero."""


def verifier() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "trusted_discovery_verifier", TESTS / "verify.py"
    )
    if spec is None or spec.loader is None:
        raise HostVerificationUnavailable("Verifier source unavailable")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def private_state(container: str, project: str) -> list[dict[str, Any]]:
    if not re.fullmatch(r"[0-9a-f]{64}", container):
        raise HostVerificationUnavailable("A full mock container identity is required")
    inspected = subprocess.run(
        ["docker", "inspect", container],
        check=True,
        capture_output=True,
        text=True,
        timeout=15,
    )
    metadata = json.loads(inspected.stdout)
    if len(metadata) != 1:
        raise HostVerificationUnavailable("Ambiguous mock container identity")
    labels = metadata[0]["Config"].get("Labels") or {}
    if (
        metadata[0]["Id"] != container
        or labels.get("com.docker.compose.service") != "mock-nous"
        or labels.get("com.docker.compose.project") != project
        or not metadata[0]["State"]["Running"]
    ):
        raise HostVerificationUnavailable(
            "Container is not this trial's running isolated mock"
        )
    response = subprocess.run(
        [
            "docker",
            "exec",
            "--user",
            "10001",
            container,
            "/usr/local/bin/python",
            "-I",
            "-c",
            STATE_READER,
        ],
        check=True,
        capture_output=True,
        text=True,
        timeout=20,
    )
    if len(response.stdout) > 10_000_000:
        raise HostVerificationUnavailable(
            "Mock state exceeds the declared capture limit"
        )
    events = json.loads(response.stdout)
    if not isinstance(events, list) or not all(
        isinstance(event, dict) for event in events
    ):
        raise HostVerificationUnavailable("Isolated mock returned malformed state")
    return events


def capture_answer(artifacts: Path, limit: int) -> dict[str, Any]:
    """Read only the registered root file through an anchored directory fd."""
    root = os.open(artifacts, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    info: os.stat_result | None = None
    raw = b""
    regular = False
    size = 0
    try:
        try:
            info = os.stat("answer.md", dir_fd=root, follow_symlinks=False)
        except FileNotFoundError:
            pass
        if info is not None:
            size = info.st_size
            regular = stat.S_ISREG(info.st_mode) and info.st_nlink == 1
        if regular:
            try:
                fd = os.open("answer.md", os.O_RDONLY | os.O_NOFOLLOW, dir_fd=root)
                with os.fdopen(fd, "rb") as stream:
                    before = os.fstat(stream.fileno())
                    regular = stat.S_ISREG(before.st_mode) and before.st_nlink == 1
                    size = before.st_size
                    if regular:
                        raw = stream.read(limit)
                    after = os.fstat(stream.fileno())
                    regular = regular and (
                        before.st_size,
                        before.st_mtime_ns,
                        before.st_nlink,
                    ) == (after.st_size, after.st_mtime_ns, after.st_nlink)
            except OSError:
                regular = False
    finally:
        os.close(root)
    return {
        "answer_path": "answer.md",
        "answer_exists": info is not None,
        "answer_is_regular_file": regular,
        "answer_file_mode": stat.filemode(info.st_mode) if info else None,
        "answer_bytes": size,
        "answer_truncated": size > limit,
        "answer": raw.decode("utf-8", errors="replace") if regular else "",
    }


def evaluate(container: str, project: str, artifacts: Path) -> dict[str, Any]:
    result: dict[str, Any] = {
        "schema_version": "1.0",
        "verification_origin": "host Docker read from isolated mock",
        "mock_container_id": container,
        "compose_project": project,
        "verifier_sha256": hashlib.sha256(
            (TESTS / "verify.py").read_bytes()
        ).hexdigest(),
        "truth_sha256": hashlib.sha256((TESTS / "truth.json").read_bytes()).hexdigest(),
        "host_checker_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
    }
    try:
        module = verifier()
        state = {"events": private_state(container, project)}
        evidence = capture_answer(artifacts, module.MAX_ANSWER_BYTES)
        failures = module.objective_failures(evidence, state)
        result.update(
            status="objective_failure" if failures else "passed",
            exit_code=10 if failures else 0,
            passed=not failures,
            failures=failures,
            answer_sha256=hashlib.sha256(evidence["answer"].encode()).hexdigest(),
            answer_audit=module.report_extra(evidence, state),
        )
    except Exception as failure:
        result.update(
            status="infrastructure",
            exit_code=2,
            passed=False,
            error_type=type(failure).__name__,
        )
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mock-container", required=True)
    parser.add_argument("--compose-project", required=True)
    parser.add_argument("--artifacts", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    result = evaluate(args.mock_container, args.compose_project, args.artifacts)
    fd = os.open(args.output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as stream:
        json.dump(result, stream, indent=2)
        stream.write("\n")
    print(json.dumps({"status": result["status"], "exit_code": result["exit_code"]}))
    raise SystemExit(result["exit_code"])


if __name__ == "__main__":
    main()
