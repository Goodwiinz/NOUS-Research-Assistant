#!/usr/bin/env python3
"""Fail unless a deployed backend reports healthy for consecutive polls.

CI7: the dev release chain ends when GitHub auto-merges the GitOps values
proposal; Argo CD then syncs out of band and nothing checked that the result
serves. This probe is the post-deploy gate. It polls ``--url`` until the JSON
body reports ``status == "healthy"`` on ``--consecutive`` polls in a row, or
exits non-zero when ``--timeout`` runs out.

It proves the deployment answers healthy. It cannot prove *which* build is
serving: the public health contract deliberately exposes no build identity.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.request
from typing import Callable, Optional, Sequence

HEALTHY = "healthy"


def probe(url: str, request_timeout: float) -> tuple[bool, str]:
    """One GET. Returns (healthy, short reason)."""
    request = urllib.request.Request(
        url,
        headers={"Accept": "application/json", "User-Agent": "deploy-health-probe"},
    )
    try:
        with urllib.request.urlopen(request, timeout=request_timeout) as response:
            status_code = response.status
            body = response.read(64 * 1024)
    except urllib.error.HTTPError as error:
        return False, f"HTTP {error.code}"
    except (urllib.error.URLError, TimeoutError, OSError) as error:
        return False, f"unreachable: {type(error).__name__}"
    if status_code != 200:
        return False, f"HTTP {status_code}"
    try:
        payload = json.loads(body)
    except ValueError:
        return False, "HTTP 200 with a non-JSON body"
    status = payload.get("status") if isinstance(payload, dict) else None
    if status != HEALTHY:
        return False, f"status={status!r}"
    return True, "healthy"


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
    args = parser.parse_args(argv)
    if args.consecutive < 1:
        parser.error("--consecutive must be at least 1")
    ok = wait_until_healthy(
        args.url,
        timeout=args.timeout,
        interval=args.interval,
        consecutive=args.consecutive,
        request_timeout=args.request_timeout,
    )
    if ok:
        print(f"{args.url} is healthy")
        return 0
    print(
        f"::error::{args.url} did not report healthy {args.consecutive} times in a "
        f"row within {args.timeout:.0f}s",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    sys.exit(main())
