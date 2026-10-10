"""Workflow contracts for the Alembic migration-check job.

The static single-head/revision-length guard is blocking. The historical
from-empty replay, ``alembic check`` drift check, and downgrade/upgrade smoke
remain visible advisory measurements because they exercise the entire legacy
chain. The evidence migration additionally has a narrow blocking delta probe:
it establishes only the exact parent-state precondition, runs the real
``evidence_prov_20260816`` upgrade, checks the two columns, downgrades to the
parent, and checks that both columns are gone.

This is a pure YAML-parse contract test (no app import, no DB), mirroring
``test_generate_openapi.py::test_contract_job_verifies_generated_typescript``.
"""

from pathlib import Path
from typing import cast

import yaml  # type: ignore[import-untyped]  # CI lint gate installs no types-PyYAML

REPO_ROOT = Path(__file__).resolve().parents[4]
WORKFLOW_PATH = REPO_ROOT / ".github" / "workflows" / "test-pipeline.yml"


def _load_job() -> dict:
    workflow = yaml.safe_load(WORKFLOW_PATH.read_text(encoding="utf-8"))
    return cast(dict, workflow["jobs"]["migration-check"])


def _steps() -> list[dict]:
    return cast("list[dict]", _load_job()["steps"])


def _step_running(fragment: str) -> dict | None:
    """Return the first step whose ``run`` block contains ``fragment``."""
    for step in _steps():
        if fragment in (step.get("run") or ""):
            return step
    return None


def _step_named(name: str) -> dict:
    """Select one exact workflow step name, never a substring collision."""
    matches = [step for step in _steps() if step.get("name") == name]
    assert len(matches) == 1, f"expected exactly one workflow step named {name!r}"
    return matches[0]


def test_job_has_postgres_15_service_with_healthcheck() -> None:
    """The advisory execution checks need a real Postgres to upgrade against."""
    job = _load_job()
    services = job.get("services") or {}
    assert "postgres" in services, "migration-check must define a postgres service"
    postgres = services["postgres"]
    assert (
        postgres.get("image") == "public.ecr.aws/docker/library/postgres:15-alpine"
    ), f"expected official PostgreSQL 15 Alpine image, got {postgres.get('image')!r}"
    options = postgres.get("options") or ""
    assert "pg_isready" in options and "--health" in options, (
        "postgres service must declare a pg_isready health check so the upgrade "
        "steps run against a ready DB"
    )


def test_static_check_remains_blocking_and_untouched() -> None:
    """The original static guard stays: same script, and NOT advisory."""
    step = _step_named("Check single head + revision-id length")
    assert "scripts/ci/check_alembic.py" in (step.get("run") or "")
    assert "continue-on-error" not in step, (
        "the static single-head / revision-length guard is the BLOCKING contract "
        "and must not be made advisory"
    )


def test_upgrade_from_empty_is_advisory() -> None:
    """From-empty ``upgrade head`` is an explicit visible advisory measurement."""
    step = _step_named("Advisory — upgrade head from empty DB")
    assert "alembic upgrade head" in (step.get("run") or "")
    assert step.get("continue-on-error") is True, (
        "from-empty replay must remain advisory (continue-on-error: true) because "
        "it measures the known legacy chain, not this migration's delta"
    )
    assert (
        "advisory" in (step.get("name") or "").lower()
    ), "label the upgrade step Advisory so its non-blocking intent is obvious"

    run = step.get("run") or ""
    assert ":x: FAIL" in run and "advisory" in run.lower(), (
        "an empty-replay failure must be visible as FAIL/advisory, never reported "
        "as PASS"
    )


def test_targeted_evidence_delta_probe_is_blocking() -> None:
    """The exact evidence delta probe blocks and has no advisory override."""
    step = _step_named("Blocking — evidence migration delta")
    assert "probe_evidence_migration.py" in (step.get("run") or "")
    assert (
        step.get("continue-on-error") is not True
    ), "the targeted evidence migration probe is the blocking execution guard"
    assert "blocking" in (step.get("name") or "").lower()
    run = step.get("run") or ""
    assert "evidence_prov_20260816" in run
    assert "i9j0k1l2m3n4" in run
    assert "GITHUB_STEP_SUMMARY" in run
    assert "ci_evidence_delta_" in run


def test_targeted_probe_uses_separate_scratch_database() -> None:
    """The blocking probe cannot consume the advisory replay's database."""
    targeted = _step_named("Blocking — evidence migration delta")
    replay = _step_named("Advisory — upgrade head from empty DB")
    targeted_run = targeted.get("run") or ""
    replay_env = replay.get("env") or {}
    assert "--admin-database-url" in targeted_run
    assert "ci_evidence_delta_" in targeted_run
    assert "ci_evidence_delta_" not in str(replay_env.get("DATABASE_URL", ""))


def test_alembic_check_drift_is_advisory() -> None:
    """``alembic check`` (models-vs-migrations drift) runs advisory."""
    step = _step_named("Advisory — alembic check (models vs migrations drift)")
    assert "alembic check" in (step.get("run") or "")
    assert step.get("continue-on-error") is True, (
        "drift check must be advisory (continue-on-error: true) — the schema is "
        "known to carry create-migration debt today"
    )
    assert "advisory" in (step.get("name") or "").lower()


def test_downgrade_smoke_is_advisory() -> None:
    """A ``downgrade -1`` then re-``upgrade head`` smoke, advisory."""
    step = _step_named("Advisory — downgrade -1 then upgrade head (round-trip smoke)")
    assert "alembic downgrade -1" in (step.get("run") or "")
    run = step.get("run") or ""
    assert "alembic upgrade head" in run, (
        "downgrade smoke must re-apply `upgrade head` to prove the last "
        "migration round-trips"
    )
    assert (
        step.get("continue-on-error") is True
    ), "downgrade smoke must be advisory (continue-on-error: true)"
    assert "advisory" in (step.get("name") or "").lower()


def test_db_url_passed_via_env_to_advisory_steps() -> None:
    """env.py reads DATABASE_URL/SUPABASE_DB_URL; the advisory steps must set it.

    Service-container credentials are CI-local throwaways, so a plain env value
    (postgres/postgres against localhost) is fine and conventional — no secrets
    interpolation is expected here.
    """
    for name, fragment in (
        ("Advisory — upgrade head from empty DB", "alembic upgrade head"),
        ("Advisory — alembic check (models vs migrations drift)", "alembic check"),
        (
            "Advisory — downgrade -1 then upgrade head (round-trip smoke)",
            "alembic downgrade -1",
        ),
    ):
        step = _step_named(name)
        assert fragment in (step.get("run") or "")
        env = step.get("env") or {}
        assert "DATABASE_URL" in env, (
            f"step {name!r} must pass DATABASE_URL so alembic/env.py can build "
            "its engine URL"
        )
        assert "localhost" in str(
            env["DATABASE_URL"]
        ), "the advisory steps should target the CI service container on localhost"


def test_each_probe_writes_to_step_summary() -> None:
    """Every execution probe must annotate its outcome in the job summary."""
    for name in (
        "Blocking — evidence migration delta",
        "Advisory — upgrade head from empty DB",
        "Advisory — alembic check (models vs migrations drift)",
        "Advisory — downgrade -1 then upgrade head (round-trip smoke)",
    ):
        step = _step_named(name)
        assert "GITHUB_STEP_SUMMARY" in (step.get("run") or ""), (
            f"probe {name!r} must write a PASS/FAIL line to "
            "$GITHUB_STEP_SUMMARY so the outcome is captured in the run record"
        )


def test_install_step_annotates_outcome_in_summary() -> None:
    """The probe-deps install writes its own outcome line so a failed install is
    visible in the record and readers know the probes below are unreliable."""
    step = _step_named("Install backend requirements (for execution probes)")
    assert "pip install --build-constraint" in (step.get("run") or "")
    assert step.get("continue-on-error") is True, (
        "install must stay advisory (continue-on-error: true) so a dependency "
        "hiccup never fails the blocking static guard that already ran"
    )
    assert "GITHUB_STEP_SUMMARY" in (
        step.get("run") or ""
    ), "install step must write a PASS/FAIL line to $GITHUB_STEP_SUMMARY"
