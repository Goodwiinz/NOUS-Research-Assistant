"""Content-safe observability contracts for Daily Research Brief runs."""

from __future__ import annotations

import logging
from uuid import uuid4

import pytest

from src.services.research_engine.observability import ResearchObservability


def test_observability_records_only_bounded_identifiers_and_aggregates(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Changing the allowlist to include content would leak research material."""
    logger = logging.getLogger("test.research.observability")
    observer = ResearchObservability(logger=logger)
    run_id = uuid4()
    organization_id = uuid4()

    with caplog.at_level(logging.INFO, logger=logger.name):
        observer.record_stage_duration(
            run_id=run_id,
            organization_id=organization_id,
            step_index=2,
            stage_type="extract",
            duration_seconds=1.25,
            status="completed",
        )
        observer.record_provider_outcome(
            run_id=run_id,
            organization_id=organization_id,
            provider="openalex",
            outcome="success",
            returned_count=12,
        )
        observer.record_review(
            run_id=run_id,
            organization_id=organization_id,
            review_kind="extraction",
            outcome="approved",
            wait_seconds=30.0,
        )
        observer.record_final_status(
            run_id=run_id,
            organization_id=organization_id,
            status="verified",
        )
        observer.record_pause(
            run_id=run_id,
            organization_id=organization_id,
            pause_kind="review_required",
            review_kind="extraction",
        )
        observer.record_override(
            run_id=run_id,
            organization_id=organization_id,
            override_kind="verification",
            outcome="continued_unverified",
        )

    log_text = "\n".join(record.getMessage() for record in caplog.records)
    assert str(run_id) in log_text
    assert str(organization_id) in log_text
    assert "extract" in log_text
    assert "openalex" in log_text
    assert "12" in log_text
    assert "1.25" in log_text
    metrics = observer.snapshot()
    assert metrics["counters"]["provider_outcomes"]["success"] == 1
    assert metrics["counters"]["final_status"]["verified"] == 1
    assert metrics["counters"]["pauses"]["review_required"] == 1
    assert metrics["counters"]["overrides"]["continued_unverified"] == 1
    assert metrics["histograms"]["stage_duration_seconds"]["extract"] == [1.25]
    assert metrics["histograms"]["review_wait_seconds"]["extraction"] == [30.0]


def test_observability_rejects_research_content_without_logging_it(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Generic event fields must fail closed before sensitive text reaches logs."""
    logger = logging.getLogger("test.research.observability.forbidden")
    observer = ResearchObservability(logger=logger)
    secrets = {
        "question": "SECRET QUESTION",
        "criteria": "SECRET CRITERIA",
        "abstract": "SECRET ABSTRACT",
        "quote": "SECRET QUOTE",
        "extraction": "SECRET EXTRACTION",
        "prompt": "SECRET PROMPT",
        "review_note": "SECRET REVIEW NOTE",
        "report_body": "SECRET REPORT BODY",
    }

    with caplog.at_level(logging.INFO, logger=logger.name):
        for key, value in secrets.items():
            with pytest.raises(ValueError, match="unsupported observability field"):
                observer.record(
                    "validation_error",
                    run_id=uuid4(),
                    error_kind="contract",
                    **{key: value},
                )

    rendered = "\n".join(record.getMessage() for record in caplog.records)
    assert all(secret not in rendered for secret in secrets.values())
    assert observer.snapshot() == {"counters": {}, "histograms": {}}


def test_observability_bounds_categories_counts_and_durations() -> None:
    """Negative or unbounded aggregate values would corrupt operational metrics."""
    observer = ResearchObservability()

    with pytest.raises(ValueError):
        observer.record_provider_outcome(
            run_id=uuid4(),
            organization_id=uuid4(),
            provider="x" * 200,
            outcome="success",
            returned_count=1,
        )
    with pytest.raises(ValueError):
        observer.record_validation_error(
            run_id=uuid4(),
            organization_id=uuid4(),
            stage_type="verify",
            error_kind="schema",
            count=-1,
        )
    with pytest.raises(ValueError):
        observer.record_stage_duration(
            run_id=uuid4(),
            organization_id=uuid4(),
            step_index=1,
            stage_type="verify",
            duration_seconds=-0.1,
            status="failed",
        )
