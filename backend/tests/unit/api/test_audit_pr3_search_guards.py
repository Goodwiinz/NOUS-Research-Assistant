"""PR3 (audit R6-H1/M1/M2/L4/L5, R2-M15/M16/M17/M18/L20/L21/L25/L26) guards."""

from pathlib import Path

import pytest

BACKEND_ROOT = Path(__file__).resolve().parents[3]


def _read(rel: str) -> str:
    return (BACKEND_ROOT / rel).read_text()


def test_search_endpoints_offload_sync_pipelines() -> None:
    src = _read("src/api/search/search.py")
    # Every sync pipeline call site is wrapped (R6-H1)
    assert src.count("run_in_threadpool(") >= 6
    assert "from fastapi.concurrency import run_in_threadpool" in src


def test_vector_400_no_longer_becomes_500() -> None:
    src = _read("src/api/search/search.py")
    search_block = src[
        src.find("async def search_documents") : src.find("async def hybrid_search(")
    ]
    assert "except HTTPException:" in search_block
    assert "raise" in search_block.split("except HTTPException:")[1][:200]


def test_search_indexes_sql_valid_and_bound() -> None:
    src = _read("src/api/search/search.py")
    block = src[src.find("def get_search_indexes") :]
    block = block[:2000]
    assert "# Placeholder" not in block  # R6-M2: '#' is a PG syntax error
    assert "text(" in block
    assert "pg_relation_size" in block


def test_health_probes_use_text_and_scrub_errors() -> None:
    src = _read("src/api/search/search.py")
    health = src[src.find("health_status") : src.find("authenticated_hybrid_search")]
    assert "db.execute(text(index_check_query))" in health
    # R6-L4: no str(e) in client-visible bodies
    client_bodies = [seg for seg in health.split("content={") if len(seg) < 400]
    assert all("str(e)" not in seg for seg in client_bodies)


def test_feedback_is_persisted_not_mocked() -> None:
    svc = _read("src/services/search/search_quality_service.py")
    assert "_persist_feedback" in svc
    assert "SearchFeedback(" in svc
    analytics = svc[svc.find("def get_quality_analytics") :]
    assert "150" not in analytics  # R2-M16 mock count gone
    assert "func.count(SearchFeedback.id)" in analytics

    model = _read("src/models/search_feedback.py")
    assert '__tablename__ = "search_feedback"' in model or (
        "__tablename__ = 'search_feedback'" in model
    )


def test_search_analytics_uses_persisted_events() -> None:
    src = _read("src/api/search/search.py")
    analytics = src[
        src.find("async def get_search_analytics") : src.find(
            "async def rebuild_search_indexes"
        )
    ]
    logger = src[
        src.find("def log_search_query") : src.find(
            "async def persist_api_key_usage_log"
        )
    ]
    assert "_load_search_analytics" in analytics
    assert "Placeholder" not in analytics
    assert "SearchAnalyticsEvent(" in logger
    assert "db.commit()" in logger


def test_benchmark_admin_only_and_capped() -> None:
    src = _read("src/api/search/search_quality.py")
    bench = src[src.find("async def run_quality_benchmark") :]
    assert "Admin access required" in bench  # R6-M1
    assert "> 10" in bench
    assert "run_in_threadpool" in bench


def test_benchmark_serialization_tolerates_optional_fields() -> None:
    src = _read("src/api/search/search_quality.py")
    assert 'getattr(result, "document_type", None)' in src  # R2-M17


def test_rerank_pool_capped_not_limit_times_two() -> None:
    src = _read("src/services/search/hybrid_search_service.py")
    assert "search_request.limit * 2" not in src  # R2-M15
    assert "min(len(fused_results), 200)" in src


def test_entity_indicators_word_bounded() -> None:
    src = _read("src/services/search/hybrid_search_service.py")
    assert (
        'r"\\b(who|what|where|when|how' in src.replace("\n", "")
        or "\\b(who|what|where|when|how" in src
    )  # R2-L26
    assert '"relationship",' not in src  # old substring list gone


def test_hybrid_suggestions_not_hasattr_dead() -> None:
    src = _read("src/services/search/hybrid_search_service.py")
    assert 'hasattr(source_result, "suggestions")' not in src  # R2-L25


def test_prepared_terms_no_fake_tsquery_operators() -> None:
    src = _read("src/services/search/fulltext_search_service.py")
    prep = src[src.find("def _prepare_search_terms") :]
    prep = prep[:1400]
    assert '" & ".join' not in prep  # R2-L20
    assert '" ".join(words)' in prep


def test_suggestion_like_escapes_wildcards() -> None:
    src = _read("src/services/search/fulltext_search_service.py")
    assert "_escape_like(query)" in src  # R2-L21
    assert "ESCAPE" in src
    assert 'f"%{query}%"' not in src


def test_analytics_normalizes_like_per_feedback() -> None:
    # F3c: /5.0 read all-one-star as 0.2; must match (rating - 1) / 4.0
    svc = _read("src/services/search/search_quality_service.py")
    analytics = svc[svc.find("def get_quality_analytics") :]
    assert "float(avg_rating) / 5.0" not in analytics
    assert "(float(avg_rating) - 1.0) / 4.0" in analytics


def test_analytics_applies_search_type_filter() -> None:
    # F3d: the filter was labelled in the response but never applied
    svc = _read("src/services/search/search_quality_service.py")
    analytics = svc[svc.find("def get_quality_analytics") :]
    assert "SearchFeedback.search_type == search_type.value" in analytics

    model = _read("src/models/search_feedback.py")
    assert "search_type = Column(String(32)" in model

    migration = _read("alembic/versions/r6_search_feedback.py")
    assert "search_type VARCHAR(32)" in migration


def test_persist_feedback_raises_on_db_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # F3b: a lost row must not answer "Feedback recorded successfully"
    import src.core.database as database
    from src.services.search import search_quality_service as mod

    class _BoomSession:
        closed = False

        def add(self, obj: object) -> None:
            pass

        def commit(self) -> None:
            raise RuntimeError("db down")

        def close(self) -> None:
            self.closed = True

    session = _BoomSession()
    monkeypatch.setattr(database, "SessionLocal", lambda: session)

    with pytest.raises(RuntimeError, match="db down"):
        mod.search_quality_service._persist_feedback(
            organization_id="org",
            user_id="user",
            query_id="q",
            rating=5,
        )
    assert session.closed is True


def test_quality_endpoints_offload_sync_service_calls() -> None:
    # F3a: both handlers are async over a sync-session service
    src = _read("src/api/search/search_quality.py")
    assert "from fastapi.concurrency import run_in_threadpool" in src
    for call in ("record_user_feedback", "get_quality_analytics"):
        idx = src.find(f"search_quality_service.{call},")
        assert idx != -1, call
        assert "await run_in_threadpool(" in src[idx - 120 : idx]


def test_search_indexes_document_count_is_org_scoped() -> None:
    # F6: an unscoped count(*) leaked the platform-wide document population
    src = _read("src/api/search/search.py")
    block = src[src.find("def get_search_indexes") :][:2500]
    assert "d.organization_id = :org" in block
    assert "org_id = _require_org_id(current_user)" in block
    assert '{"org": org_id}' in block
    assert "NOT d.is_deleted" in block
    # sync handler -> FastAPI threadpool, no event-loop block on get_db_sync
    assert "\nasync def get_search_indexes" not in src
