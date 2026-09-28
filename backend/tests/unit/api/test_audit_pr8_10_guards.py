"""PR8-10 (audit R5-M10/M11/L7/L9/L11/L15/L17/L19/L23-refuted, R6-L7/L8/L9/L21/L23/L24) guards."""

from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parents[3]


def _read(rel: str) -> str:
    return (BACKEND_ROOT / rel).read_text()


# R5-M10
def test_thread_context_route_unwraps_service_shape() -> None:
    api = _read("src/api/threads/threads.py")
    blk = api[api.find("async def get_thread_context") :]
    assert "context is None" in blk[:900]
    assert "metadata.get(" in blk[:1400]
    svc = _read("src/services/threads/chat_service.py")
    ctx = svc[svc.find("async def get_thread_context") :]
    assert "return None\n\n        messages = []" in ctx


# R5-M11
def test_feedback_commit_after_thread_check() -> None:
    svc = _read("src/services/threads/chat_service.py")
    fb = svc[svc.find("async def update_message_feedback") :]
    assert "flush()" in fb[:1200] and "commit()" not in fb[:800]
    api = _read("src/api/threads/threads.py")
    seg = api[
        api.find("async def update_message_feedback") : api.rfind("@router.delete(")
    ]
    assert "await db.commit()" in seg
    assert "await db.rollback()" in seg


# R5-L7
def test_conversation_counts_exclude_deleted_threads() -> None:
    src = _read("src/services/threads/conversation_service.py")
    agg = src[src.find("agg_stmt = (") :]
    assert "Thread.is_deleted == False" in agg[:600]


# R5-L8
def test_conversation_search_escapes_like() -> None:
    for rel in (
        "src/services/threads/chat_service.py",
        "src/services/threads/conversation_service.py",
    ):
        src = _read(rel)
        assert "_escape_like" in src
        assert 'ilike(f"%{query}%")' not in src
        assert 'pattern = f"%{search_query}%"' not in src


# R5-L9
def test_add_member_validates_target_user() -> None:
    src = _read("src/services/threads/workspace_service.py")
    add = src[src.find("async def add_member") :]
    assert "Target user does not exist" in add[:1500]


# R5-L11
def test_workspace_quota_cap() -> None:
    src = _read("src/services/threads/workspace_service.py")
    cw = src[src.find("async def create_workspace") :]
    assert "MAX_WORKSPACES_PER_ORG" in cw[:2000]
    assert _read("src/core/config.py").count("MAX_WORKSPACES_PER_ORG") >= 1
    assert "Project limit reached" in _read("src/services/research/project_service.py")
    engine_src = _read("src/api/research_engine/projects.py")
    assert "collection_id is required" in engine_src
    assert "Project is already linked" in engine_src


# R2-L6
def test_collection_list_aggregates_document_count() -> None:
    src = _read("src/services/threads/collection_service.py")
    block = src[
        src.find("async def list_collections") : src.find("async def update_collection")
    ]
    assert "func.count(CollectionDocument.id)" in block
    assert "selectinload(Collection.documents)" not in block


# R6-M11
def test_local_fallback_secrets_are_process_stable() -> None:
    from src.core.config import Settings

    first = Settings(
        ENVIRONMENT="development",
        DATABASE_URL="postgresql+asyncpg://localhost/test",
        SECRET_KEY="",
        JWT_SECRET_KEY="",
    )
    second = Settings(
        ENVIRONMENT="development",
        DATABASE_URL="postgresql+asyncpg://localhost/test",
        SECRET_KEY="",
        JWT_SECRET_KEY="",
    )
    assert first.SECRET_KEY == second.SECRET_KEY
    assert first.JWT_SECRET_KEY == second.JWT_SECRET_KEY
    assert first.SECRET_KEY != first.JWT_SECRET_KEY


# R5-L15 — already SQL-aggregated upstream; assert no full-row load
def test_metrics_summary_is_sql_aggregate() -> None:
    src = _read("src/api/infrastructure/evaluation.py")
    blk = src[src.find("async def get_metrics_summary") :]
    assert "func.count(EvaluationMetric.id)" in blk[:2500]
    assert ".all()\n        )" in blk  # aggregates, not ORM rows loop


# R5-L17
def test_orphan_citations_fail_closed() -> None:
    src = _read("src/api/research/citations.py")
    af = src[src.find("access_filter = or_(") :]
    # R5-L17 final: no creator column exists, so orphans match nothing —
    # fail-closed beats world-readable.
    assert "document_id.is_(None), Citation.message_id.is_(None)" not in af[:600]
    assert "R5-L17" in af[:800]


# R5-L19
def test_export_does_not_read_empty_evidence_table() -> None:
    src = _read("src/services/research_engine/export_service.py")
    assert "ResearchEvidence" not in src
    assert '"evidence_count": 0' in src
    assert not (BACKEND_ROOT / "src/models/research_evidence.py").exists()


# R5-M23/R5-L16
def test_matrix_extraction_uses_durable_worker_and_shared_status() -> None:
    api = _read("src/api/research/extraction_matrix.py")
    projects = _read("src/api/research/projects.py")
    task = _read("src/tasks/research_tasks.py")
    assert "asyncio.create_task" not in api
    assert "asyncio.create_task" not in projects
    assert api.count("run_extraction_matrix.apply_async") == 2
    assert "run_extraction_matrix.apply_async" in projects
    assert 'name="src.tasks.research_tasks.run_extraction_matrix"' in task


# R6-L7
def test_note_payload_bounds() -> None:
    src = _read("src/shared/research_schemas.py")
    nc = src[src.find("class ProjectNoteCreate") : src.find("class ProjectNoteUpdate")]
    assert "max_length=200_000" in nc
    assert "max_length=100" in nc
    dr = _read("src/api/research/drafts.py")
    assert "Maximum 10 items" in dr or "max_length=500" in dr


# R6-L8
def test_draft_export_filename_and_latex_escape() -> None:
    src = _read("src/services/research/draft_generation_service.py")
    assert "_safe_filename(draft.title" in src
    assert "_latex_escape(markdown_content)" in src
    assert "draft.title.replace(' ', '_')" not in src


# R6-L9
def test_relationship_type_typed_confidence_bounded() -> None:
    src = _read("src/shared/research_schemas.py")
    blk = src[src.find("class CitationRelationshipCreate") :]
    assert "relationship_type: CitationRelationshipType" in blk[:700]
    assert "allow_inf_nan=False" in blk[:900]
    api = _read("src/api/research/citations.py")
    assert "_normalize_citation_relationship_type(relationship_type)" in api
    assert 'relationship_type: str = "CITES"' in api
