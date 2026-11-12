#!/usr/bin/env python3
"""Fail unless a deployed backend reports healthy for consecutive polls.

CI7: the dev release chain ends when GitHub auto-merges the GitOps values
proposal; Argo CD then syncs out of band and nothing checked that the result
serves. This probe is the post-deploy gate. It polls ``--url`` until the JSON
body reports ``status == "healthy"`` on ``--consecutive`` polls in a row, or
exits non-zero when ``--timeout`` runs out.

Healthy alone is weak evidence. ``/health`` is static and the Deployment rolls
with maxSurge 1 / maxUnavailable 0, so a crash-looping new image leaves the
old pod answering healthy. With ``--values-file`` the probe also compares the
``git_sha`` that ``/health`` reports (opt-in via HEALTH_EXPOSE_BUILD_SHA, audit
I17) with ``backend.image.sourceSha`` in that values file. A mismatch fails
only once the values file was last committed more than ``--grace-minutes``
ago, which leaves Argo CD time to sync and roll. A missing ``git_sha`` warns
and passes, so the gate keeps working until the flag reaches dev.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Callable, Optional, Sequence

HEALTHY = "healthy"
SHA_RE = re.compile(r"[0-9a-f]{40}")
SOURCE_SHA_PATH = ("backend", "image", "sourceSha")


def fetch_json(url: str, request_timeout: float) -> tuple[Optional[dict], str]:
    """One GET. Returns (JSON object or None, short reason)."""
    request = urllib.request.Request(
        url,
        headers={"Accept": "application/json", "User-Agent": "deploy-health-probe"},
    )
    try:
        with urllib.request.urlopen(request, timeout=request_timeout) as response:
            status_code = response.status
            body = response.read(64 * 1024)
    except urllib.error.HTTPError as error:
        return None, f"HTTP {error.code}"
    except (urllib.error.URLError, TimeoutError, OSError) as error:
        return None, f"unreachable: {type(error).__name__}"
    if status_code != 200:
        return None, f"HTTP {status_code}"
    try:
        payload = json.loads(body)
    except ValueError:
        return None, "HTTP 200 with a non-JSON body"
    if not isinstance(payload, dict):
        return None, "HTTP 200 with a non-object JSON body"
    return payload, "ok"


def probe(url: str, request_timeout: float) -> tuple[bool, str]:
    """One GET. Returns (healthy, short reason)."""
    payload, reason = fetch_json(url, request_timeout)
    if payload is None:
        return False, reason
    status = payload.get("status")
    if status != HEALTHY:
        return False, f"status={status!r}"
    return True, "healthy"


def read_source_sha(values_file: Path) -> str:
    """Return ``backend.image.sourceSha`` from a Helm values file.

    A tiny indentation-aware reader for block mappings, so the gate needs no
    PyYAML on the runner. Raises ValueError unless the value is a full SHA.
    """
    stack: list[tuple[int, str]] = []
    for raw in values_file.read_text(encoding="utf-8").splitlines():
        line = raw.split(" #", 1)[0].rstrip()
        stripped = line.lstrip()
        if not stripped or stripped.startswith(("#", "-")):
            continue
        match = re.match(r"([A-Za-z0-9_.-]+):(?:\s+(.*))?$", stripped)
        if match is None:
            continue
        indent = len(line) - len(stripped)
        while stack and stack[-1][0] >= indent:
            stack.pop()
        stack.append((indent, match.group(1)))
        if tuple(key for _, key in stack) == SOURCE_SHA_PATH:
            value = (match.group(2) or "").strip().strip("\"'")
            if not SHA_RE.fullmatch(value):
                raise ValueError(
                    f"backend.image.sourceSha is not a full SHA: {value!r}"
                )
            return value
    raise ValueError(f"backend.image.sourceSha not found in {values_file}")


def last_changed_at(path: Path) -> Optional[int]:
    """Unix time of the last commit touching ``path`` (needs full history)."""
    result = subprocess.run(
        ["git", "log", "-1", "--format=%ct", "--", path.name],
        cwd=path.parent,
        capture_output=True,
        text=True,
        check=False,
    )
    output = result.stdout.strip()
    if result.returncode != 0 or not output.isdigit():
        return None
    return int(output)


def check_build(
    url: str,
    *,
    expected_sha: str,
    changed_at: Optional[int],
    grace_seconds: float,
    request_timeout: float = 10.0,
    now: Callable[[], float] = time.time,
    log: Callable[[str], None] = print,
) -> bool:
    """Compare /health ``git_sha`` with the expected source SHA.

    Fails only on a confirmed mismatch after the grace window. A missing
    ``git_sha`` (flag not rolled out yet) or an in-grace mismatch passes with a
    warning.
    """
    payload, reason = fetch_json(url, request_timeout)
    if payload is None:
        log(f"::error::build check could not read {url}: {reason}")
        return False
    served = payload.get("git_sha")
    if not isinstance(served, str) or not served:
        log(
            "::warning::/health reports no git_sha (HEALTH_EXPOSE_BUILD_SHA off?); "
            "cannot tell which build serves"
        )
        return True
    if served == expected_sha:
        log(f"dev serves the expected build {expected_sha}")
        return True
    if changed_at is None:
        age = None
        log("::warning::could not date the values file; treating grace as elapsed")
    else:
        age = now() - changed_at
    detail = f"dev serves {served}, values file selects {expected_sha}"
    if age is not None and age <= grace_seconds:
        log(
            f"::warning::{detail}; values changed {age / 60:.0f} min ago, "
            f"inside the {grace_seconds / 60:.0f} min rollout grace"
        )
        return True
    log(f"::error::{detail} after the {grace_seconds / 60:.0f} min rollout grace")
    return False


def wait_until_healthy(
    url: str,
    *,
    timeout: float,
    interval: float,
    consecutive: int,
    request_timeout: float = 10.0,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
    log: Callable[[str], None] = print,
) -> bool:
    """Poll until ``consecutive`` healthy answers in a row, or time out."""
    deadline = clock() + timeout
    streak = 0
    attempt = 0
    while True:
        attempt += 1
        healthy, reason = probe(url, request_timeout)
        streak = streak + 1 if healthy else 0
        log(f"poll {attempt}: {reason} (streak {streak}/{consecutive})")
        if streak >= consecutive:
            return True
        if clock() + interval > deadline:
            return False
        sleep(interval)


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--url", required=True)
    parser.add_argument("--timeout", type=float, default=600.0)
    parser.add_argument("--interval", type=float, default=15.0)
    parser.add_argument("--consecutive", type=int, default=3)
    parser.add_argument("--request-timeout", type=float, default=10.0)
    parser.add_argument(
        "--values-file",
        type=Path,
        help="Helm values file whose backend.image.sourceSha dev must serve",
    )
    parser.add_argument(
        "--grace-minutes",
        type=float,
        default=20.0,
        help="tolerate a SHA mismatch this long after the values file changed",
    )
    args = parser.parse_args(argv)
    if args.consecutive < 1:
        parser.error("--consecutive must be at least 1")
    if args.grace_minutes < 0:
        parser.error("--grace-minutes must not be negative")
    expected_sha: Optional[str] = None
    if args.values_file is not None:
        try:
            expected_sha = read_source_sha(args.values_file)
        except (OSError, ValueError) as error:
            print(f"::error::{error}", file=sys.stderr)
            return 1
    ok = wait_until_healthy(
        args.url,
        timeout=args.timeout,
        interval=args.interval,
        consecutive=args.consecutive,
        request_timeout=args.request_timeout,
    )
    if not ok:
        print(
            f"::error::{args.url} did not report healthy {args.consecutive} times "
            f"in a row within {args.timeout:.0f}s",
            file=sys.stderr,
        )
        return 1
    print(f"{args.url} is healthy")
    if expected_sha is None:
        return 0
    build_ok = check_build(
        args.url,
        expected_sha=expected_sha,
        changed_at=last_changed_at(args.values_file),
        grace_seconds=args.grace_minutes * 60,
        request_timeout=args.request_timeout,
    )
    return 0 if build_ok else 1


if __name__ == "__main__":
    sys.exit(main())
