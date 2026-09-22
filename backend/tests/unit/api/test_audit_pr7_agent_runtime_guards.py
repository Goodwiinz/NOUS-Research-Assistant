"""PR7 (audit R2-M3/M8/M11/M12/M13/L2/L3/L5/L7/L8/L14/L16/L24, R5-M17-M20/L23/L16) guards."""

from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parents[3]


def _read(rel: str) -> str:
    return (BACKEND_ROOT / rel).read_text()


# R2-M3
def test_cancelled_is_terminal_for_status_updates() -> None:
    src = _read("src/services/research/draft_generation_service.py")
    upd = src[src.find("def _update_status") :]
    assert 'cur_status == "cancelled"' in upd[:900]
    assert "CancelledError" in upd[:1200]


# R2-L2 / R5-L16(status dict)
def test_generation_status_dicts_bounded() -> None:
    d = _read("src/services/research/draft_generation_service.py")
    assert "_GENERATION_STATUS_MAX" in d
    e = _read("src/services/research/extraction_matrix_service.py")
    assert "_EXTRACTION_STATUS_MAX" in e
    assert "def set_extraction_status" in e


# R2-L3
def test_doc_zero_not_wrapped_to_last_document() -> None:
    src = _read("src/services/research/draft_generation_service.py")
    start = src.index("def _extract_citations_from_content")
    end = src.index("\n    def _update_status", start)
    blk = src[start:end]
    assert "if doc_idx < 0:" in blk


# R2-L5
def test_delete_draft_promotes_next_current() -> None:
    src = _read("src/services/research/draft_generation_service.py")
    blk = src[
        src.find("async def delete_draft") : src.find("async def get_draft_citations")
    ]
    assert "was_current" in blk and "is_current = True" in blk


# R2-M11
def test_summary_prompt_uses_recent_window() -> None:
    src = _read("src/services/threads/thread_summarization_service.py")
    fmt = src[src.find("def _format_messages_for_prompt") :]
    assert "recent_window: int = 40" in fmt[:400]
    assert "messages[-recent_window:]" in fmt[:800]
    assert "reversed(messages)" in fmt[:1000]


# R2-L7
def test_timeout_fallback_persisted() -> None:
    src = _read("src/services/threads/thread_summarization_service.py")
    blk = src[src.find("except asyncio.TimeoutError") :]
    assert "_persist_summary(thread, thread_id, fallback, inflight_token)" in blk[:500]


# R2-L14
def test_llm_clients_closed() -> None:
    src = _read("src/services/threads/thread_summarization_service.py")
    assert "async with openai.AsyncOpenAI(" in src
    assert "async with anthropic.AsyncAnthropic(" in src


# R2-L8
def test_redis_socket_timeouts() -> None:
    src = _read("src/services/threads/thread_summarization_service.py")
    assert "socket_connect_timeout=2" in src
    assert "socket_timeout=2" in src


# R2-M12
def test_eval_cleanup_deletes_children_first() -> None:
    src = _read("src/tasks/evaluation_tasks.py")
    blk = src[src.find("def cleanup_old_evaluations") :]
    assert "EvaluationMetric, EvaluationReport, EvaluationComparison" in blk
    assert "synchronize_session=False" in blk


# R2-M13
def test_sweep_preserves_prior_error() -> None:
    src = _read("src/tasks/agent_run_tasks.py")
    blk = src[src.find("error = prior_error or") - 200 :]
    assert "prior_error or (" in blk[:300]


# R5-M17
def test_stale_research_run_sweeper_registered() -> None:
    task_src = _read("src/tasks/research_run_tasks.py")
    assert "sweep_stale_research_runs" in task_src
    app = _read("src/tasks/celery_app.py")
    assert '"src.tasks.research_run_tasks"' in app
    assert "sweep-stale-research-runs" in app


# R5-M18
def test_stream_claims_running_atomically() -> None:
    src = _read("src/api/research_engine/runs.py")
    stream = (
        src[src.find("async def stream_run") :]
        if "async def stream_run" in src
        else src
    )
    assert "_sa_update(ResearchRun)" in stream
    assert 'ResearchRun.status.in_(["pending", "paused"])' in stream
    assert "rowcount == 0" in stream


# R5-M19
def test_engine_rehydrates_prior_outputs() -> None:
    runs = _read("src/api/research_engine/runs.py")
    assert "prior_outputs" in runs
    eng = _read("src/services/research_engine/engine.py")
    assert "initial_context" in eng
    assert "context.update(initial_context)" in eng


# R5-M20 + migration
def test_integrity_uniques_migration_exists() -> None:
    mig = _read("alembic/versions/r6_run_integrity_uniques.py")
    assert "uq_research_steps_run_idx" in mig
    assert "uq_generated_drafts_project_version" in mig
    assert "uq_generated_drafts_current" in mig


# R5-M23
def test_extraction_offloaded_to_background_task() -> None:
    src = _read("src/api/research/extraction_matrix.py")
    tasks = _read("src/tasks/research_tasks.py")
    assert "run_extraction_matrix.apply_async(" in src
    assert 'name="src.tasks.research_tasks.run_extraction_matrix"' in tasks
    assert "asyncio.run(" in tasks
    assert "run_background_extraction" in tasks
    assert "Maximum 10 documents per extraction batch" in src


# R2-L16 / R2-L24 — dead code deleted
def test_dead_workflow_task_deleted() -> None:
    src = _read("src/tasks/research_tasks.py")
    assert "execute_research_workflow" not in src


def test_bm25_update_statistics_deleted() -> None:
    src = _read("src/services/search/bm25_service.py")
    assert "def update_statistics" not in src
