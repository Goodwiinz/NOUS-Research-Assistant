"""R2-M12: nightly ``cleanup_old_evaluations`` must delete job dependents.

EvaluationComparison has no ``job_id`` column (it references jobs through
``baseline_job_id`` and ``comparison_job_id``), so filtering it on
``job_id`` raised AttributeError every night and nothing was cleaned.
"""

from __future__ import annotations

import uuid
from typing import Any
from unittest.mock import MagicMock

import pytest

pytestmark = pytest.mark.unit

from src.models.evaluation import (  # noqa: E402
    EvaluationComparison,
    EvaluationJob,
    EvaluationMetric,
    EvaluationReport,
)
from src.tasks import evaluation_tasks as et  # noqa: E402


def _session_for(old_job: MagicMock) -> tuple[MagicMock, dict[type, MagicMock]]:
    """Sync session mock whose query(model) returns a per-model query mock."""
    queries: dict[type, MagicMock] = {}

    def _query(model: type) -> MagicMock:
        if model not in queries:
            q = MagicMock(name=f"query({model.__name__})")
            if model is EvaluationJob:
                q.filter.return_value.all.return_value = [old_job]
            queries[model] = q
        return queries[model]

    db = MagicMock()
    db.query.side_effect = _query
    return db, queries


def _filter_expr(query_mock: MagicMock) -> Any:
    query_mock.filter.assert_called_once()
    (expr,), _ = query_mock.filter.call_args
    return expr


def _compiled(expr: Any) -> tuple[str, dict[str, Any]]:
    compiled = expr.compile()
    return str(compiled), dict(compiled.params)


def test_cleanup_deletes_comparisons_by_both_job_fks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    job = MagicMock()
    job.id = uuid.uuid4()
    db, queries = _session_for(job)
    monkeypatch.setattr(et, "SessionLocal", lambda: db)

    result = et.cleanup_old_evaluations.apply().get()

    assert result == {"cleaned_jobs": 1}

    # Comparisons: baseline_job_id IN (...) OR comparison_job_id IN (...)
    cmp_sql, cmp_params = _compiled(_filter_expr(queries[EvaluationComparison]))
    assert "evaluation_comparisons.baseline_job_id IN" in cmp_sql
    assert "evaluation_comparisons.comparison_job_id IN" in cmp_sql
    assert " OR " in cmp_sql
    assert list(cmp_params.values()) == [[job.id], [job.id]]
    queries[EvaluationComparison].filter.return_value.delete.assert_called_once_with(
        synchronize_session=False
    )

    # Metrics and reports: job_id IN (...)
    for model, table in (
        (EvaluationMetric, "evaluation_metrics"),
        (EvaluationReport, "evaluation_reports"),
    ):
        sql, params = _compiled(_filter_expr(queries[model]))
        assert f"{table}.job_id IN" in sql
        assert list(params.values()) == [[job.id]]
        queries[model].filter.return_value.delete.assert_called_once_with(
            synchronize_session=False
        )

    db.delete.assert_called_once_with(job)
    db.commit.assert_called_once()
    db.close.assert_called_once()


def test_cleanup_with_no_old_jobs_skips_child_deletes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    job_query = MagicMock()
    job_query.filter.return_value.all.return_value = []
    db = MagicMock()
    db.query.side_effect = lambda model: (
        job_query if model is EvaluationJob else pytest.fail("unexpected query")
    )
    monkeypatch.setattr(et, "SessionLocal", lambda: db)

    result = et.cleanup_old_evaluations.apply().get()

    assert result == {"cleaned_jobs": 0}
    db.delete.assert_not_called()
    db.commit.assert_called_once()
