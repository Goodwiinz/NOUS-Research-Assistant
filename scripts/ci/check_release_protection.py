"""Validate the effective develop branch rule from GitHub GraphQL, read-only."""

from __future__ import annotations

import json
import sys


def main() -> int:
    try:
        rule = json.load(sys.stdin)
        checks = rule.get("requiredStatusChecks", [])
        # Strict checks are not required: release-dev waits for a successful full
        # Test Pipeline push run on the develop SHA, which tests the merged result.
        valid = rule.get("requiresStatusChecks") is True and any(
            check.get("context") == "Release Gate"
            and (check.get("app") or {}).get("databaseId") == 15368
            for check in checks
        )
    except (ValueError, AttributeError, TypeError):
        valid = False
    if not valid:
        print(
            "::error::develop must require the Release Gate check from GitHub Actions before automatic release promotion.",
            file=sys.stderr,
        )
        return 1
    print(
        "develop requires Release Gate from GitHub Actions; release protection verified."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
