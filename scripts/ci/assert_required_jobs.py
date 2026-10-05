#!/usr/bin/env python3
"""Require successful selected checks and account for every Release Gate job.

Without a profile, preserve the original all-jobs-must-succeed contract. With a
profile, only jobs explicitly excluded by a successful CI plan may be skipped.
Missing, malformed, failed or cancelled results always block the gate.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

REQUIRED_JOBS: tuple[str, ...] = (
    "lint-backend",
    "lint-frontend",
    "migration-check",
    "openapi-contract",
    "unit-tests",
    "golden-replay",
    "integration-tests",
    "resilience-tests",
    "frontend-tests",
    "security-scan",
    "e2e-tests",
)

COMMON_JOBS = ("ci-plan", "lightweight-checks")
PROFILE_JOBS: dict[str, tuple[str, ...]] = {
    "docs": (),
    "frontend": ("lint-frontend", "frontend-tests", "e2e-tests"),
    "backend": tuple(
        job for job in REQUIRED_JOBS if job not in ("lint-frontend", "frontend-tests")
    ),
    "full": REQUIRED_JOBS,
}

JOB_LABELS: dict[str, str] = {
    "ci-plan": "CI Plan",
    "lightweight-checks": "Lightweight Checks",
    "lint-backend": "Lint Backend",
    "lint-frontend": "Lint Frontend",
    "migration-check": "Alembic Migration Check",
    "openapi-contract": "OpenAPI Contract Ratchet",
    "unit-tests": "Unit Tests",
    "golden-replay": "Golden Replay",
    "integration-tests": "Integration Tests",
    "resilience-tests": "Resilience Tests",
    "frontend-tests": "Frontend Tests",
    "security-scan": "Security Scan",
    "e2e-tests": "E2E Tests",
}

MISSING_RESULT = "<missing>"
INVALID_RESULT = "<invalid>"


def profile_outputs(profile: str) -> dict[str, str]:
    """The selector and gate share the job-to-output contract."""
    jobs = PROFILE_JOBS[profile]
    return {
        "profile": profile,
        **{
            flag: str(job in jobs).lower()
            for flag, job in (
                ("backend", "lint-backend"),
                ("frontend", "lint-frontend"),
                ("integration", "integration-tests"),
                ("e2e", "e2e-tests"),
            )
        },
    }


def validate_plan(payload: Mapping[str, Any], profile: str) -> None:
    """Reject an absent/inconsistent plan or an unaccounted gate dependency."""
    unknown = set(payload) - set(COMMON_JOBS + REQUIRED_JOBS)
    if unknown:
        raise ValueError(f"unaccounted gate dependencies: {', '.join(sorted(unknown))}")
    plan = payload.get("ci-plan")
    outputs = plan.get("outputs") if isinstance(plan, Mapping) else None
    if not isinstance(outputs, Mapping):
        raise ValueError("CI plan outputs are missing or malformed")
    for flag, expected in profile_outputs(profile).items():
        if outputs.get(flag) != expected:
            raise ValueError(
                f"CI plan output {flag!r} does not match profile {profile!r}"
            )


def normalize_results(
    payload: Mapping[str, Any], jobs: Sequence[str] = REQUIRED_JOBS
) -> dict[str, str]:
    """Return one normalized result for every required job.

    ``toJSON(needs)`` produces ``{job: {result, outputs}}``.  Flat
    ``{job: result}`` maps are also accepted so the decision function remains
    easy to exercise outside GitHub Actions.
    """

    normalized: dict[str, str] = {}
    for job in jobs:
        if job not in payload:
            normalized[job] = MISSING_RESULT
            continue

        value = payload[job]
        if isinstance(value, Mapping):
            value = value.get("result")

        normalized[job] = value if isinstance(value, str) else INVALID_RESULT
    return normalized


def blocking_results(
    results: Mapping[str, str],
    jobs: Sequence[str] = REQUIRED_JOBS,
    *,
    optional_jobs: Sequence[str] = (),
) -> dict[str, str]:
    """Only explicitly unselected jobs may return ``skipped``."""

    return {
        job: results.get(job, MISSING_RESULT)
        for job in jobs
        if results.get(job)
        not in (("success", "skipped") if job in optional_jobs else ("success",))
    }


def render_summary(
    results: Mapping[str, str],
    jobs: Sequence[str] = REQUIRED_JOBS,
    *,
    optional_jobs: Sequence[str] = (),
    profile: str | None = None,
) -> str:
    """Render the same fail-closed decision as a GitHub step-summary table."""

    lines = ["## Release Gate", ""]
    if profile is not None:
        lines += [f"CI profile: **{profile}**", ""]
    lines += ["| Required job | Result | Selection |", "|---|---|---|"]
    for job in jobs:
        result = results.get(job, MISSING_RESULT)
        marker = ":white_check_mark:" if result == "success" else ":x:"
        if job in optional_jobs and result == "skipped":
            marker = ":heavy_minus_sign:"
        safe_result = result.replace("|", "\\|").replace("\n", " ")
        selection = "not selected" if job in optional_jobs else "required"
        lines.append(f"| {JOB_LABELS[job]} | {marker} `{safe_result}` | {selection} |")
    return "\n".join(lines) + "\n"


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--profile",
        choices=tuple(PROFILE_JOBS),
        help="Validated CI plan profile. Omit to require success from all legacy jobs.",
    )
    parser.add_argument(
        "--results-json",
        default=os.environ.get("REQUIRED_JOB_RESULTS"),
        help=(
            "JSON job-result map. Defaults to REQUIRED_JOB_RESULTS, which the "
            "workflow populates from toJSON(needs)."
        ),
    )
    parser.add_argument(
        "--summary-file",
        type=Path,
        default=None,
        help="Optional file to append the Markdown result table to.",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.results_json is None:
        print(
            "Release gate configuration error: no job-result JSON was supplied.",
            file=sys.stderr,
        )
        return 2

    try:
        payload = json.loads(args.results_json)
    except json.JSONDecodeError as exc:
        print(f"Release gate configuration error: invalid JSON: {exc}", file=sys.stderr)
        return 2

    if not isinstance(payload, Mapping):
        print(
            "Release gate configuration error: job results must be a JSON object.",
            file=sys.stderr,
        )
        return 2

    jobs = REQUIRED_JOBS
    optional_jobs: tuple[str, ...] = ()
    if args.profile is not None:
        try:
            validate_plan(payload, args.profile)
        except ValueError as exc:
            print(f"Release gate configuration error: {exc}", file=sys.stderr)
            return 2
        jobs = COMMON_JOBS + REQUIRED_JOBS
        optional_jobs = tuple(
            job for job in REQUIRED_JOBS if job not in PROFILE_JOBS[args.profile]
        )

    results = normalize_results(payload, jobs)
    summary = render_summary(
        results, jobs, optional_jobs=optional_jobs, profile=args.profile
    )
    print(summary, end="")
    if args.summary_file is not None:
        with args.summary_file.open("a", encoding="utf-8") as handle:
            handle.write(summary)

    blocked = blocking_results(results, jobs, optional_jobs=optional_jobs)
    if blocked:
        detail = ", ".join(f"{job}={result}" for job, result in blocked.items())
        print(f"Release gate blocked: {detail}", file=sys.stderr)
        return 1

    print("Release gate passed: every required job succeeded.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
