"""Tests for the do_kb_retrieve agent tool.

Asserts: schema present in AGENT_TOOLS, empty-KB path returns empty list
without calling client, populated-KB returns parsed chunks, errors are
swallowed and surfaced as `error` field.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID

import pytest

from src.services.agent import tools as tools_module
from src.services.agent import tools_impl
from src.services.agent.tools import do_kb_retrieve
from src.services.agent.tools_impl import (
    AGENT_TOOLS,
    _tool_do_kb_retrieve,
    _tool_search_documents,
)
from src.services.do_kb.client import DOKnowledgeBaseError
from src.services.do_kb.models import Chunk, RetrieveResult
from src.services.do_kb.retrieval import DOKBRetrieveOutcome, DOKBRetrieveStatus

TARGET_ID = UUID("11111111-1111-4111-8111-111111111111")
DISTRACTOR_ID = UUID("22222222-2222-4222-8222-222222222222")
MISSING_ID = UUID("33333333-3333-4333-8333-333333333333")


def _settings() -> SimpleNamespace:
    return SimpleNamespace(
        DO_KB_ENABLED=True,
        DO_KB_DEFAULT_TOP_K=8,
        AGENT_DOKB_COHERE_RERANK=False,
        AGENT_ITERATIVE_RETRIEVAL=False,
    )


def _scalar_result(values: list[UUID]) -> MagicMock:
    result = MagicMock()
    result.scalars.return_value.all.return_value = values
    result.__iter__.side_effect = lambda: iter((value,) for value in values)
    return result


def _authorized_document_result(
    rows: list[tuple[UUID, str | None, str]],
) -> MagicMock:
    """Mirror the three-column authorization result used by named retrieval."""
    result = MagicMock()
    result.all.return_value = rows
    result.__iter__.side_effect = lambda: iter(rows)
    return result


def _filtered_item_names(filters: dict) -> list[str]:
    if "equals" in filters:
        return [filters["equals"]["value"]]
    return [clause["equals"]["value"] for clause in filters["or_all"]]


@pytest.mark.unit
def test_schema_registered():
    names = {t["function"]["name"] for t in AGENT_TOOLS}
    assert "do_kb_retrieve" in names
    legacy = next(
        tool for tool in AGENT_TOOLS if tool["function"]["name"] == "do_kb_retrieve"
    )
    properties = legacy["function"]["parameters"]["properties"]
    assert properties["document_ids"] == {
        "type": "array",
        "items": {"type": "string"},
        "description": (
            "Canonical document UUIDs returned by search_documents. "
            "When supplied, retrieval is limited to these documents (maximum 20)."
        ),
    }


@pytest.mark.unit
def test_decorated_schema_exposes_optional_document_ids_not_config():
    schema = do_kb_retrieve.args_schema.model_json_schema()

    assert set(schema["properties"]) == {"query", "top_k", "document_ids"}
    assert schema["required"] == ["query"]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_wrapper_forwards_document_ids_with_positional_config(monkeypatch):
    db = MagicMock()
    user = SimpleNamespace(id="user-1", organization_id="org-1")
    captured = AsyncMock(return_value={"chunks": [], "total": 0})

    @asynccontextmanager
    async def fake_context(_config):
        yield db, user, {}

    monkeypatch.setattr(tools_module, "_tool_context", fake_context)
    monkeypatch.setattr(tools_impl, "_tool_do_kb_retrieve", captured)
    config = {"configurable": {"user_id": "user-1"}}

    result = await do_kb_retrieve.coroutine(
        "attention mechanisms", 6, config, document_ids=[str(TARGET_ID)]
    )

    assert result == {"chunks": [], "total": 0}
    captured.assert_awaited_once_with(
        {
            "query": "attention mechanisms",
            "top_k": 6,
            "document_ids": [str(TARGET_ID)],
        },
        db,
        user,
    )


@pytest.mark.unit
@pytest.mark.asyncio
async def test_wrapper_preserves_explicit_empty_document_scope(monkeypatch):
    captured = AsyncMock(return_value={"chunks": [], "total": 0})

    @asynccontextmanager
    async def fake_context(_config):
        yield MagicMock(), SimpleNamespace(id="user-1", organization_id="org-1"), {}

    monkeypatch.setattr(tools_module, "_tool_context", fake_context)
    monkeypatch.setattr(tools_impl, "_tool_do_kb_retrieve", captured)

    await do_kb_retrieve.coroutine("attention", document_ids=[])

    assert captured.await_args.args[0]["document_ids"] == []


@pytest.mark.unit
@pytest.mark.asyncio
async def test_returns_empty_when_disabled(monkeypatch):
    fake_settings = MagicMock()
    fake_settings.DO_KB_ENABLED = False
    monkeypatch.setattr(
        "src.services.agent.tools_impl.settings", fake_settings, raising=False
    )

    user = MagicMock()
    user.organization_id = "org-1"
    db = MagicMock()
    result = await _tool_do_kb_retrieve({"query": "anything", "top_k": 4}, db, user)
    assert result["chunks"] == []
    assert result["total"] == 0
    assert result.get("reason") == "disabled"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_named_scope_is_authorized_before_disabled_limitation():
    user = SimpleNamespace(id="user-1", organization_id=UUID(int=10))
    db = MagicMock()
    db.execute = AsyncMock(
        return_value=_authorized_document_result([(TARGET_ID, None, "local")])
    )
    kb_lookup = AsyncMock(return_value="kb-1")
    provider = AsyncMock()
    disabled_settings = _settings()
    disabled_settings.DO_KB_ENABLED = False

    with (
        patch("src.core.config.settings", disabled_settings),
        patch("src.services.do_kb.retrieval.resolve_org_kb_uuid", kb_lookup),
        patch("src.services.do_kb.retrieval.retrieve_kb_chunks", provider),
    ):
        result = await _tool_do_kb_retrieve(
            {
                "query": "attention",
                "document_ids": [str(TARGET_ID)],
            },
            db,
            user,
        )

    authorization_sql = str(db.execute.await_args.args[0]).lower()
    assert "documents.organization_id" in authorization_sql
    assert "documents.is_deleted = false" in authorization_sql
    assert result["reason"] == "scoped_retrieval_unavailable"
    assert "No other document was substituted" in result["note"]
    kb_lookup.assert_not_awaited()
    provider.assert_not_awaited()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_returns_empty_when_org_has_no_kb():
    user = MagicMock()
    user.organization_id = "org-1"

    db = MagicMock()
    db.get = AsyncMock(return_value=MagicMock(do_kb_uuid=None))

    with (
        patch(
            "src.core.config.settings",
            MagicMock(DO_KB_ENABLED=True),
        ),
        patch(
            "src.services.do_kb.get_do_kb_client",
        ) as mock_factory,
    ):
        result = await _tool_do_kb_retrieve({"query": "anything", "top_k": 4}, db, user)
        mock_factory.assert_not_called()

    assert result["chunks"] == []
    assert result.get("reason") == "not_provisioned"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_happy_path_returns_chunks():
    user = MagicMock()
    user.organization_id = "org-1"

    db = MagicMock()
    org_row = MagicMock()
    org_row.do_kb_uuid = "kb-1"
    db.get = AsyncMock(return_value=org_row)
    # Document lookup for title resolution returns no match. Unresolved
    # storage identifiers must not be exposed as citation IDs or titles.
    empty_rows = MagicMock()
    empty_rows.__iter__ = lambda self: iter([])
    db.execute = AsyncMock(return_value=empty_rows)

    fake_client = MagicMock()
    fake_client.retrieve = AsyncMock(
        return_value=RetrieveResult(
            chunks=[
                Chunk(
                    text="hello", score=0.9, document_id="doc-1", metadata={"k": "v"}
                ),
                Chunk(text="world", score=0.5, document_id="doc-2", metadata={}),
            ],
            total=2,
        )
    )

    with (
        patch(
            "src.core.config.settings",
            MagicMock(DO_KB_ENABLED=True),
        ),
        patch("src.services.do_kb.get_do_kb_client", return_value=fake_client),
    ):
        result = await _tool_do_kb_retrieve({"query": "hello", "top_k": 5}, db, user)

    assert result["total"] == 2
    assert result["source"] == "do_kb"
    assert result["chunks"][0]["text"] == "hello"
    # Title resolution had no DB match, so the raw storage id is omitted.
    assert result["chunks"][0]["document_id"] is None
    assert result["chunks"][0]["title"] == "Untitled"
    fake_client.retrieve.assert_awaited_once_with(
        kb_uuid="kb-1", query="hello", top_k=5
    )


@pytest.mark.unit
@pytest.mark.asyncio
async def test_retrieve_failure_returns_error_not_raise():
    user = MagicMock()
    user.organization_id = "org-1"

    db = MagicMock()
    org_row = MagicMock()
    org_row.do_kb_uuid = "kb-1"
    db.get = AsyncMock(return_value=org_row)

    fake_client = MagicMock()
    fake_client.retrieve = AsyncMock(side_effect=RuntimeError("boom"))

    with (
        patch(
            "src.core.config.settings",
            MagicMock(DO_KB_ENABLED=True),
        ),
        patch("src.services.do_kb.get_do_kb_client", return_value=fake_client),
    ):
        result = await _tool_do_kb_retrieve({"query": "x", "top_k": 3}, db, user)

    assert result["chunks"] == []
    assert "error" in result
    assert result["source"] == "do_kb"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_retrieve_404_logs_error_and_falls_back(caplog):
    """FIX A3: a 404 (KB deleted on DO's side) is a permanent failure — it must
    be logged distinctly at ERROR, not blended into WARNING transient noise.
    Still returns an empty result so the agent falls back cleanly."""
    import logging

    user = MagicMock()
    user.organization_id = "org-1"

    db = MagicMock()
    org_row = MagicMock()
    org_row.do_kb_uuid = "kb-1"
    db.get = AsyncMock(return_value=org_row)

    fake_client = MagicMock()
    fake_client.retrieve = AsyncMock(
        side_effect=DOKnowledgeBaseError("not found", status_code=404)
    )

    with (
        patch("src.core.config.settings", MagicMock(DO_KB_ENABLED=True)),
        patch("src.services.do_kb.get_do_kb_client", return_value=fake_client),
        caplog.at_level(logging.ERROR, logger="src.services.agent.tools_impl"),
    ):
        result = await _tool_do_kb_retrieve({"query": "x", "top_k": 3}, db, user)

    # Fallback preserved: empty result, not a raise.
    assert result["chunks"] == []
    assert result["total"] == 0
    assert result["source"] == "do_kb"
    # The 404 was logged at ERROR level with a distinct message.
    error_records = [r for r in caplog.records if r.levelno >= logging.ERROR]
    assert any("404" in r.getMessage() for r in error_records)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_retrieve_transient_error_stays_warning(caplog):
    """A non-404 DOKnowledgeBaseError (transient) must NOT be logged at ERROR —
    it stays at WARNING so 404s remain distinguishable."""
    import logging

    user = MagicMock()
    user.organization_id = "org-1"

    db = MagicMock()
    org_row = MagicMock()
    org_row.do_kb_uuid = "kb-1"
    db.get = AsyncMock(return_value=org_row)

    fake_client = MagicMock()
    fake_client.retrieve = AsyncMock(
        side_effect=DOKnowledgeBaseError("upstream down", status_code=503)
    )

    with (
        patch("src.core.config.settings", MagicMock(DO_KB_ENABLED=True)),
        patch("src.services.do_kb.get_do_kb_client", return_value=fake_client),
        caplog.at_level(logging.WARNING, logger="src.services.agent.tools_impl"),
    ):
        result = await _tool_do_kb_retrieve({"query": "x", "top_k": 3}, db, user)

    assert result["chunks"] == []
    error_records = [r for r in caplog.records if r.levelno >= logging.ERROR]
    assert not error_records  # no ERROR for a transient failure


@pytest.mark.unit
@pytest.mark.asyncio
async def test_clamps_top_k():
    user = MagicMock()
    user.organization_id = "org-1"

    db = MagicMock()
    org_row = MagicMock()
    org_row.do_kb_uuid = "kb-1"
    db.get = AsyncMock(return_value=org_row)

    fake_client = MagicMock()
    fake_client.retrieve = AsyncMock(return_value=RetrieveResult(chunks=[], total=0))

    with (
        patch(
            "src.core.config.settings",
            MagicMock(DO_KB_ENABLED=True),
        ),
        patch("src.services.do_kb.get_do_kb_client", return_value=fake_client),
    ):
        await _tool_do_kb_retrieve({"query": "x", "top_k": 9999}, db, user)

    args = fake_client.retrieve.await_args
    assert args.kwargs["top_k"] == 20


@pytest.mark.unit
@pytest.mark.asyncio
async def test_missing_query_rejected():
    user = MagicMock()
    user.organization_id = "org-1"
    result = await _tool_do_kb_retrieve({"query": "  "}, MagicMock(), user)
    assert "error" in result
    assert result["chunks"] == []


@pytest.mark.unit
@pytest.mark.asyncio
async def test_named_scope_authorizes_before_provider_and_drops_stronger_distractor():
    user = SimpleNamespace(id="user-1", organization_id=UUID(int=10))
    db = MagicMock()
    events: list[str] = []

    async def execute(stmt):
        events.append(
            "document-authorization" if not events else "canonical-resolution"
        )
        if len(events) == 1:
            return _authorized_document_result([(TARGET_ID, None, "local")])
        return [
            (
                TARGET_ID,
                f"documents/{user.organization_id}/{TARGET_ID}.pdf",
                "Attention Is All You Need",
            ),
            (
                DISTRACTOR_ID,
                f"documents/{user.organization_id}/{DISTRACTOR_ID}.pdf",
                "Unrelated Paper",
            ),
        ]

    db.execute = AsyncMock(side_effect=execute)

    async def resolve_kb(*_args, **_kwargs):
        events.append("kb-lookup")
        return "kb-1"

    async def retrieve(**_kwargs):
        events.append("provider")
        return DOKBRetrieveOutcome(
            status=DOKBRetrieveStatus.SUCCESS,
            result=RetrieveResult(
                chunks=[
                    Chunk(
                        text="stronger but unrelated",
                        score=0.99,
                        document_id=f"{DISTRACTOR_ID}.txt",
                        metadata={"score_source": "upstream"},
                    ),
                    Chunk(
                        text="target evidence",
                        score=0.51,
                        document_id=f"{TARGET_ID}.txt",
                        metadata={"score_source": "upstream"},
                    ),
                ],
                total=2,
            ),
        )

    kb_lookup = AsyncMock(side_effect=resolve_kb)
    provider = AsyncMock(side_effect=retrieve)
    with (
        patch("src.core.config.settings", _settings()),
        patch("src.services.do_kb.retrieval.resolve_org_kb_uuid", kb_lookup),
        patch("src.services.do_kb.retrieval.retrieve_kb_chunks", provider),
    ):
        result = await _tool_do_kb_retrieve(
            {
                "query": "attention mechanism",
                "top_k": 8,
                "document_ids": [str(TARGET_ID)],
            },
            db,
            user,
        )

    authorization_sql = str(db.execute.await_args_list[0].args[0]).lower()
    selected_columns = authorization_sql.split("\nfrom", 1)[0]
    assert "documents.id" in selected_columns
    assert "documents.storage_path" in selected_columns
    assert "documents.storage_backend" in selected_columns
    assert "documents.title" not in selected_columns
    assert "documents.organization_id" in authorization_sql
    assert "documents.is_deleted = false" in authorization_sql
    assert "documents.id in" in authorization_sql
    assert events[:3] == ["document-authorization", "kb-lookup", "provider"]
    provider.assert_awaited_once_with(
        kb_uuid="kb-1",
        query="attention mechanism",
        org_id=user.organization_id,
        top_k=8,
        filters={
            "equals": {
                "key": "item_name",
                "value": f"{TARGET_ID}.txt",
            }
        },
    )
    assert result["total"] == 1
    assert result["chunks"] == [
        {
            "text": "target evidence",
            "score": 0.51,
            "score_source": "upstream",
            "document_id": str(TARGET_ID),
            "title": "Attention Is All You Need",
            "metadata": {"score_source": "upstream"},
        }
    ]


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("source_case", "storage_path"),
    [
        (
            "no-text",
            "documents/00000000-0000-0000-0000-000000000010/"
            "11111111-1111-4111-8111-111111111111/rate-policy.pdf",
        ),
        (
            "canonical-upload-failure",
            "documents/00000000-0000-0000-0000-000000000010/"
            "11111111-1111-4111-8111-111111111111/upload-fallback.pdf",
        ),
    ],
)
async def test_named_scope_retrieves_original_s3_ingest_fallback(
    source_case: str, storage_path: str
):
    """The durable row cannot distinguish why ingest selected its original.

    Ingest uses the original S3 object both when content text is absent and
    when uploading the canonical text mirror fails. Named retrieval must allow
    both possible item names so either already-indexed source stays reachable.
    """
    user = SimpleNamespace(id="user-1", organization_id=UUID(int=10))
    leaf = storage_path.rsplit("/", 1)[-1]
    authorization = _authorized_document_result([(TARGET_ID, storage_path, "s3")])
    resolution_rows = [
        (TARGET_ID, storage_path, "API Rate Limit Policy"),
    ]
    db = MagicMock()
    db.execute = AsyncMock(side_effect=[authorization, resolution_rows])

    async def retrieve(**kwargs):
        allowed_names = _filtered_item_names(kwargs["filters"])
        chunks = (
            [
                Chunk(
                    text=f"evidence from {source_case}",
                    score=0.91,
                    document_id=leaf,
                    metadata={"score_source": "upstream"},
                )
            ]
            if leaf in allowed_names
            else []
        )
        return DOKBRetrieveOutcome(
            status=DOKBRetrieveStatus.SUCCESS,
            result=RetrieveResult(chunks=chunks, total=len(chunks)),
        )

    provider = AsyncMock(side_effect=retrieve)
    with (
        patch("src.core.config.settings", _settings()),
        patch(
            "src.services.do_kb.retrieval.resolve_org_kb_uuid",
            AsyncMock(return_value="kb-1"),
        ),
        patch("src.services.do_kb.retrieval.retrieve_kb_chunks", provider),
    ):
        result = await _tool_do_kb_retrieve(
            {
                "query": "rate limit",
                "document_ids": [str(TARGET_ID)],
            },
            db,
            user,
        )

    assert [chunk["text"] for chunk in result["chunks"]] == [
        f"evidence from {source_case}"
    ]
    assert result["chunks"][0]["document_id"] == str(TARGET_ID)
    assert result["chunks"][0]["title"] == "API Rate Limit Policy"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_multiple_named_documents_retrieve_mixed_canonical_and_original_sources():
    user = SimpleNamespace(id="user-1", organization_id=UUID(int=10))
    original_path = (
        f"documents/{user.organization_id}/{TARGET_ID}/1700000000_rate-policy.pdf"
    )
    original_leaf = original_path.rsplit("/", 1)[-1]
    authorization = _authorized_document_result(
        [
            (TARGET_ID, original_path, "s3"),
            (DISTRACTOR_ID, None, "local"),
        ]
    )
    resolution_rows = [
        (TARGET_ID, original_path, "API Rate Limit Policy"),
        (DISTRACTOR_ID, None, "Webhook Retry Policy"),
    ]
    db = MagicMock()
    db.execute = AsyncMock(side_effect=[authorization, resolution_rows])

    async def retrieve(**kwargs):
        allowed_names = _filtered_item_names(kwargs["filters"])
        required_names = {original_leaf, f"{DISTRACTOR_ID}.txt"}
        chunks = []
        if required_names.issubset(allowed_names):
            chunks = [
                Chunk(
                    text="original-object evidence",
                    score=0.94,
                    document_id=original_leaf,
                    metadata={"score_source": "upstream"},
                ),
                Chunk(
                    text="canonical evidence",
                    score=0.83,
                    document_id=f"{DISTRACTOR_ID}.txt",
                    metadata={"score_source": "upstream"},
                ),
            ]
        return DOKBRetrieveOutcome(
            status=DOKBRetrieveStatus.SUCCESS,
            result=RetrieveResult(chunks=chunks, total=len(chunks)),
        )

    provider = AsyncMock(side_effect=retrieve)
    with (
        patch("src.core.config.settings", _settings()),
        patch(
            "src.services.do_kb.retrieval.resolve_org_kb_uuid",
            AsyncMock(return_value="kb-1"),
        ),
        patch("src.services.do_kb.retrieval.retrieve_kb_chunks", provider),
    ):
        result = await _tool_do_kb_retrieve(
            {
                "query": "compare policies",
                "document_ids": [str(TARGET_ID), str(DISTRACTOR_ID)],
            },
            db,
            user,
        )

    assert _filtered_item_names(provider.await_args.kwargs["filters"]) == [
        f"{TARGET_ID}.txt",
        original_leaf,
        f"{DISTRACTOR_ID}.txt",
    ]
    assert [chunk["text"] for chunk in result["chunks"]] == [
        "original-object evidence",
        "canonical evidence",
    ]
    assert [chunk["document_id"] for chunk in result["chunks"]] == [
        str(TARGET_ID),
        str(DISTRACTOR_ID),
    ]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_multiple_named_documents_use_one_or_all_filter():
    user = SimpleNamespace(id="user-1", organization_id=UUID(int=10))
    db = MagicMock()
    db.execute = AsyncMock(
        return_value=_authorized_document_result(
            [
                (TARGET_ID, None, "local"),
                (DISTRACTOR_ID, None, "local"),
            ]
        )
    )
    provider = AsyncMock(
        return_value=DOKBRetrieveOutcome(
            status=DOKBRetrieveStatus.SUCCESS,
            result=RetrieveResult(chunks=[], total=0),
        )
    )

    with (
        patch("src.core.config.settings", _settings()),
        patch(
            "src.services.do_kb.retrieval.resolve_org_kb_uuid",
            AsyncMock(return_value="kb-1"),
        ),
        patch("src.services.do_kb.retrieval.retrieve_kb_chunks", provider),
    ):
        result = await _tool_do_kb_retrieve(
            {
                "query": "compare papers",
                "document_ids": [str(TARGET_ID), str(DISTRACTOR_ID)],
            },
            db,
            user,
        )

    assert provider.await_args.kwargs["filters"] == {
        "or_all": [
            {
                "equals": {
                    "key": "item_name",
                    "value": f"{TARGET_ID}.txt",
                }
            },
            {
                "equals": {
                    "key": "item_name",
                    "value": f"{DISTRACTOR_ID}.txt",
                }
            },
        ]
    }
    assert result["reason"] == "no_scoped_chunks"
    assert "No other document was substituted" in result["note"]


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize(
    "document_ids",
    [
        [],
        str(TARGET_ID),
        [""],
        ["not-a-uuid"],
        [str(TARGET_ID)] * 21,
    ],
    ids=["empty", "non-list", "empty-member", "malformed", "over-cap"],
)
async def test_invalid_named_scope_never_broadens_or_calls_provider(document_ids):
    user = SimpleNamespace(id="user-1", organization_id=UUID(int=10))
    db = MagicMock()
    db.execute = AsyncMock()
    kb_lookup = AsyncMock(return_value="kb-1")
    provider = AsyncMock()

    with (
        patch("src.core.config.settings", _settings()),
        patch("src.services.do_kb.retrieval.resolve_org_kb_uuid", kb_lookup),
        patch("src.services.do_kb.retrieval.retrieve_kb_chunks", provider),
    ):
        result = await _tool_do_kb_retrieve(
            {"query": "attention", "document_ids": document_ids}, db, user
        )

    assert result["chunks"] == []
    assert result["total"] == 0
    assert result["reason"] == "invalid_document_scope"
    assert "No other document was substituted" in result["note"]
    db.execute.assert_not_awaited()
    kb_lookup.assert_not_awaited()
    provider.assert_not_awaited()


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize("unavailable_case", ["missing", "foreign", "deleted"])
async def test_unavailable_named_document_fails_closed_before_kb_lookup(
    unavailable_case: str,
):
    user = SimpleNamespace(id="user-1", organization_id=UUID(int=10))
    db = MagicMock()
    db.execute = AsyncMock(return_value=_authorized_document_result([]))
    kb_lookup = AsyncMock(return_value="kb-1")
    provider = AsyncMock()

    with (
        patch("src.core.config.settings", _settings()),
        patch("src.services.do_kb.retrieval.resolve_org_kb_uuid", kb_lookup),
        patch("src.services.do_kb.retrieval.retrieve_kb_chunks", provider),
    ):
        result = await _tool_do_kb_retrieve(
            {"query": "attention", "document_ids": [str(MISSING_ID)]}, db, user
        )

    sql = str(db.execute.await_args.args[0]).lower()
    assert "documents.organization_id" in sql, unavailable_case
    assert "documents.is_deleted = false" in sql, unavailable_case
    assert result["reason"] == "requested_documents_unavailable"
    assert "No other document was substituted" in result["note"]
    kb_lookup.assert_not_awaited()
    provider.assert_not_awaited()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_partial_named_document_authorization_rejects_entire_set():
    user = SimpleNamespace(id="user-1", organization_id=UUID(int=10))
    db = MagicMock()
    db.execute = AsyncMock(
        return_value=_authorized_document_result([(TARGET_ID, None, "local")])
    )
    kb_lookup = AsyncMock(return_value="kb-1")
    provider = AsyncMock()

    with (
        patch("src.core.config.settings", _settings()),
        patch("src.services.do_kb.retrieval.resolve_org_kb_uuid", kb_lookup),
        patch("src.services.do_kb.retrieval.retrieve_kb_chunks", provider),
    ):
        result = await _tool_do_kb_retrieve(
            {
                "query": "compare",
                "document_ids": [str(TARGET_ID), str(MISSING_ID)],
            },
            db,
            user,
        )

    assert result["reason"] == "requested_documents_unavailable"
    kb_lookup.assert_not_awaited()
    provider.assert_not_awaited()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_soft_deleted_project_membership_is_not_authorized():
    project_id = UUID("44444444-4444-4444-8444-444444444444")
    user = SimpleNamespace(id="user-1", organization_id=UUID(int=10))
    db = MagicMock()
    db.execute = AsyncMock(
        side_effect=[
            _authorized_document_result([(TARGET_ID, None, "local")]),
            _scalar_result([]),
        ]
    )
    owned_project = SimpleNamespace(id=project_id)
    verify_project = AsyncMock(return_value=owned_project)
    kb_lookup = AsyncMock(return_value="kb-1")
    provider = AsyncMock()

    with (
        patch("src.core.config.settings", _settings()),
        patch(
            "src.services.agent.tools_impl._verify_project_ownership",
            verify_project,
        ),
        patch("src.services.do_kb.retrieval.resolve_org_kb_uuid", kb_lookup),
        patch("src.services.do_kb.retrieval.retrieve_kb_chunks", provider),
    ):
        result = await _tool_do_kb_retrieve(
            {
                "query": "attention",
                "project_id": str(project_id),
                "document_ids": [str(TARGET_ID)],
            },
            db,
            user,
        )

    membership_sql = str(db.execute.await_args_list[1].args[0]).lower()
    assert "collection_documents.collection_id" in membership_sql
    assert "collection_documents.document_id in" in membership_sql
    assert "collection_documents.is_deleted = false" in membership_sql
    assert result["reason"] == "requested_documents_unavailable"
    verify_project.assert_awaited_once()
    kb_lookup.assert_not_awaited()
    provider.assert_not_awaited()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_owned_project_accepts_complete_multi_document_scope():
    project_id = UUID("44444444-4444-4444-8444-444444444444")
    user = SimpleNamespace(id="user-1", organization_id=UUID(int=10))
    authorization = _authorized_document_result(
        [
            (TARGET_ID, None, "local"),
            (DISTRACTOR_ID, None, "local"),
        ]
    )
    named_membership = _scalar_result([TARGET_ID, DISTRACTOR_ID])
    resolution_rows = [
        (TARGET_ID, None, "API Rate Limit Policy"),
        (DISTRACTOR_ID, None, "Webhook Retry Policy"),
    ]
    resolver_membership = [(TARGET_ID,), (DISTRACTOR_ID,)]
    db = MagicMock()
    db.execute = AsyncMock(
        side_effect=[
            authorization,
            named_membership,
            resolution_rows,
            resolver_membership,
        ]
    )
    provider = AsyncMock(
        return_value=DOKBRetrieveOutcome(
            status=DOKBRetrieveStatus.SUCCESS,
            result=RetrieveResult(
                chunks=[
                    Chunk(
                        text="rate evidence",
                        score=0.9,
                        document_id=f"{TARGET_ID}.txt",
                        metadata={"score_source": "upstream"},
                    ),
                    Chunk(
                        text="retry evidence",
                        score=0.8,
                        document_id=f"{DISTRACTOR_ID}.txt",
                        metadata={"score_source": "upstream"},
                    ),
                ],
                total=2,
            ),
        )
    )

    with (
        patch("src.core.config.settings", _settings()),
        patch(
            "src.services.agent.tools_impl._verify_project_ownership",
            AsyncMock(return_value=SimpleNamespace(id=project_id)),
        ),
        patch(
            "src.services.do_kb.retrieval.resolve_org_kb_uuid",
            AsyncMock(return_value="kb-1"),
        ),
        patch("src.services.do_kb.retrieval.retrieve_kb_chunks", provider),
    ):
        result = await _tool_do_kb_retrieve(
            {
                "query": "compare policies",
                "project_id": str(project_id),
                "document_ids": [str(TARGET_ID), str(DISTRACTOR_ID)],
            },
            db,
            user,
        )

    assert provider.await_count == 1
    assert [chunk["document_id"] for chunk in result["chunks"]] == [
        str(TARGET_ID),
        str(DISTRACTOR_ID),
    ]
    named_membership_sql = str(db.execute.await_args_list[1].args[0]).lower()
    assert "collection_documents.collection_id" in named_membership_sql
    assert "collection_documents.is_deleted = false" in named_membership_sql


@pytest.mark.unit
@pytest.mark.asyncio
async def test_unowned_project_named_scope_does_not_fall_back_org_wide():
    user = SimpleNamespace(id="user-1", organization_id=UUID(int=10))
    db = MagicMock()
    db.execute = AsyncMock(
        return_value=_authorized_document_result([(TARGET_ID, None, "local")])
    )
    verify_project = AsyncMock(return_value=None)
    kb_lookup = AsyncMock(return_value="kb-1")
    provider = AsyncMock()

    with (
        patch("src.core.config.settings", _settings()),
        patch(
            "src.services.agent.tools_impl._verify_project_ownership",
            verify_project,
        ),
        patch("src.services.do_kb.retrieval.resolve_org_kb_uuid", kb_lookup),
        patch("src.services.do_kb.retrieval.retrieve_kb_chunks", provider),
    ):
        result = await _tool_do_kb_retrieve(
            {
                "query": "attention",
                "project_id": "not-owned",
                "document_ids": [str(TARGET_ID)],
            },
            db,
            user,
        )

    assert result["reason"] == "requested_documents_unavailable"
    kb_lookup.assert_not_awaited()
    provider.assert_not_awaited()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_filtered_provider_400_returns_limitation_without_retry():
    user = SimpleNamespace(id="user-1", organization_id=UUID(int=10))
    db = MagicMock()
    db.execute = AsyncMock(
        return_value=_authorized_document_result([(TARGET_ID, None, "local")])
    )
    provider_error = DOKnowledgeBaseError("invalid request body", status_code=400)
    provider = AsyncMock(
        return_value=DOKBRetrieveOutcome(
            status=DOKBRetrieveStatus.ERROR_OTHER,
            error=provider_error,
        )
    )

    with (
        patch("src.core.config.settings", _settings()),
        patch(
            "src.services.do_kb.retrieval.resolve_org_kb_uuid",
            AsyncMock(return_value="kb-1"),
        ),
        patch("src.services.do_kb.retrieval.retrieve_kb_chunks", provider),
    ):
        result = await _tool_do_kb_retrieve(
            {
                "query": "attention",
                "document_ids": [str(TARGET_ID)],
            },
            db,
            user,
        )

    assert provider.await_count == 1
    assert provider.await_args.kwargs["filters"]["equals"]["value"] == (
        f"{TARGET_ID}.txt"
    )
    assert result["chunks"] == []
    assert result["reason"] == "scoped_retrieval_unavailable"
    assert "No other document was substituted" in result["note"]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_named_scope_without_provisioned_kb_returns_explicit_limitation():
    user = SimpleNamespace(id="user-1", organization_id=UUID(int=10))
    db = MagicMock()
    db.execute = AsyncMock(
        return_value=_authorized_document_result([(TARGET_ID, None, "local")])
    )
    provider = AsyncMock()

    with (
        patch("src.core.config.settings", _settings()),
        patch(
            "src.services.do_kb.retrieval.resolve_org_kb_uuid",
            AsyncMock(return_value=None),
        ),
        patch("src.services.do_kb.retrieval.retrieve_kb_chunks", provider),
    ):
        result = await _tool_do_kb_retrieve(
            {"query": "attention", "document_ids": [str(TARGET_ID)]}, db, user
        )

    assert result["reason"] == "scoped_retrieval_unavailable"
    assert "No other document was substituted" in result["note"]
    provider.assert_not_awaited()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_named_scope_provider_exception_returns_limitation_without_retry():
    user = SimpleNamespace(id="user-1", organization_id=UUID(int=10))
    db = MagicMock()
    db.execute = AsyncMock(
        return_value=_authorized_document_result([(TARGET_ID, None, "local")])
    )
    provider = AsyncMock(side_effect=RuntimeError("provider transport failed"))

    with (
        patch("src.core.config.settings", _settings()),
        patch(
            "src.services.do_kb.retrieval.resolve_org_kb_uuid",
            AsyncMock(return_value="kb-1"),
        ),
        patch("src.services.do_kb.retrieval.retrieve_kb_chunks", provider),
    ):
        result = await _tool_do_kb_retrieve(
            {"query": "attention", "document_ids": [str(TARGET_ID)]}, db, user
        )

    assert provider.await_count == 1
    assert result["reason"] == "scoped_retrieval_unavailable"
    assert "No other document was substituted" in result["note"]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_search_documents_zero_hit_does_not_recommend_broad_named_substitute():
    db = MagicMock()
    rows = MagicMock()
    rows.scalars.return_value.all.return_value = []
    db.execute = AsyncMock(return_value=rows)
    user = SimpleNamespace(organization_id=UUID(int=10))

    result = await _tool_search_documents(
        {"query": "Attention Is All You Need", "max_results": 10}, db, user
    )

    assert result["documents"] == []
    assert "specific named source" in result["suggestion"]
    assert "do not substitute broad retrieval" in result["suggestion"]


# -- Phase 2: project_id post-filter -----------------------------------------
# Trace 019e168a showed KB returning chunks from sibling projects because the
# KB is org-scoped. When `project_id` is supplied, drop chunks whose resolved
# document is not in the active project (via collection_documents).


@pytest.mark.unit
@pytest.mark.asyncio
async def test_project_id_filters_out_cross_project_chunks():
    import uuid

    user = MagicMock()
    user.organization_id = "org-1"

    in_project_doc_id = uuid.uuid4()
    out_of_project_doc_id = uuid.uuid4()
    pid = uuid.uuid4()

    db = MagicMock()
    org_row = MagicMock()
    org_row.do_kb_uuid = "kb-1"
    db.get = AsyncMock(return_value=org_row)

    # First db.execute call resolves title_by_key (storage-key → UUID, title).
    # Second call resolves project membership (collection_documents).
    title_rows = [
        (in_project_doc_id, "in.pdf", "In-project doc"),
        (out_of_project_doc_id, "out.pdf", "Cross-project doc"),
    ]
    membership_rows = [(in_project_doc_id,)]

    title_result = MagicMock()
    title_result.__iter__ = lambda self: iter(title_rows)
    membership_result = MagicMock()
    membership_result.__iter__ = lambda self: iter(membership_rows)
    db.execute = AsyncMock(side_effect=[title_result, membership_result])

    fake_client = MagicMock()
    fake_client.retrieve = AsyncMock(
        return_value=RetrieveResult(
            chunks=[
                Chunk(text="in", score=0.9, document_id="in.pdf", metadata={}),
                Chunk(text="out", score=0.8, document_id="out.pdf", metadata={}),
            ],
            total=2,
        )
    )

    owned = MagicMock()
    owned.id = pid
    with (
        patch(
            "src.core.config.settings",
            MagicMock(DO_KB_ENABLED=True),
        ),
        patch("src.services.do_kb.get_do_kb_client", return_value=fake_client),
        patch(
            "src.services.agent.tools_impl._verify_project_ownership",
            AsyncMock(return_value=owned),
        ),
    ):
        result = await _tool_do_kb_retrieve(
            {"query": "x", "top_k": 5, "project_id": str(pid)}, db, user
        )

    assert len(result["chunks"]) == 1
    assert result["chunks"][0]["text"] == "in"
    assert result["total"] == 1


@pytest.mark.unit
@pytest.mark.asyncio
async def test_no_project_id_keeps_all_chunks():
    """Without project_id, all chunks pass through (legacy behavior)."""
    user = MagicMock()
    user.organization_id = "org-1"

    db = MagicMock()
    org_row = MagicMock()
    org_row.do_kb_uuid = "kb-1"
    db.get = AsyncMock(return_value=org_row)

    empty_rows = MagicMock()
    empty_rows.__iter__ = lambda self: iter([])
    db.execute = AsyncMock(return_value=empty_rows)

    fake_client = MagicMock()
    fake_client.retrieve = AsyncMock(
        return_value=RetrieveResult(
            chunks=[
                Chunk(text="a", score=0.9, document_id="a.pdf", metadata={}),
                Chunk(text="b", score=0.5, document_id="b.pdf", metadata={}),
            ],
            total=2,
        )
    )

    with (
        patch(
            "src.core.config.settings",
            MagicMock(DO_KB_ENABLED=True),
        ),
        patch("src.services.do_kb.get_do_kb_client", return_value=fake_client),
    ):
        result = await _tool_do_kb_retrieve({"query": "x", "top_k": 5}, db, user)

    assert len(result["chunks"]) == 2
    assert result["total"] == 2


@pytest.mark.unit
@pytest.mark.asyncio
async def test_owned_project_id_with_unresolvable_chunks_returns_empty():
    """Owned project_id but no chunk resolves to a known document → empty.

    Once ownership is verified, the shared resolve_and_filter_chunks helper
    still returns an empty list when none of the retrieved chunks map to a
    document in the project, so unscoped content is never leaked.
    """
    user = MagicMock()
    user.organization_id = "org-1"

    db = MagicMock()
    org_row = MagicMock()
    org_row.do_kb_uuid = "kb-1"
    db.get = AsyncMock(return_value=org_row)

    empty_rows = MagicMock()
    empty_rows.__iter__ = lambda self: iter([])
    db.execute = AsyncMock(return_value=empty_rows)

    fake_client = MagicMock()
    fake_client.retrieve = AsyncMock(
        return_value=RetrieveResult(
            chunks=[Chunk(text="a", score=0.9, document_id="a.pdf", metadata={})],
            total=1,
        )
    )

    owned = MagicMock()
    with (
        patch(
            "src.core.config.settings",
            MagicMock(DO_KB_ENABLED=True),
        ),
        patch("src.services.do_kb.get_do_kb_client", return_value=fake_client),
        patch(
            "src.services.agent.tools_impl._verify_project_ownership",
            AsyncMock(return_value=owned),
        ),
    ):
        result = await _tool_do_kb_retrieve(
            {
                "query": "x",
                "top_k": 5,
                "project_id": "11111111-1111-1111-1111-111111111111",
            },
            db,
            user,
        )

    # Unresolvable chunks + project_id → empty (no leaking unscoped content).
    assert len(result["chunks"]) == 0


@pytest.mark.unit
@pytest.mark.asyncio
async def test_project_id_not_owned_falls_back_to_org_wide():
    """An unverifiable project_id (stale page context, another member's
    project) must NOT hard-fail retrieval — it is usually injected, not
    model-chosen (trace 01a0209b). The filter is dropped and org-wide
    results are returned; org-wide output reveals nothing about the
    unverified project, so membership inference is still prevented."""
    user = MagicMock()
    user.organization_id = "org-1"

    db = MagicMock()
    org_row = MagicMock()
    org_row.do_kb_uuid = "kb-1"
    db.get = AsyncMock(return_value=org_row)

    empty_rows = MagicMock()
    empty_rows.__iter__ = lambda self: iter([])
    db.execute = AsyncMock(return_value=empty_rows)

    fake_client = MagicMock()
    fake_client.retrieve = AsyncMock(
        return_value=RetrieveResult(
            chunks=[Chunk(text="a", score=0.9, document_id="a.pdf", metadata={})],
            total=1,
        )
    )

    with (
        patch(
            "src.core.config.settings",
            MagicMock(DO_KB_ENABLED=True),
        ),
        patch("src.services.do_kb.get_do_kb_client", return_value=fake_client),
        patch(
            "src.services.agent.tools_impl._verify_project_ownership",
            AsyncMock(return_value=None),  # not owned / not found
        ),
    ):
        result = await _tool_do_kb_retrieve(
            {"query": "x", "top_k": 5, "project_id": "someone-elses-project"},
            db,
            user,
        )

    # Fallback behaves exactly like the no-project path: no error, and no
    # project-filtered result set derived from the unverified id.
    assert "error" not in result
    assert result["total"] == 1
