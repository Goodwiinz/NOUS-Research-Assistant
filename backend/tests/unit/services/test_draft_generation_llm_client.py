"""Draft generation's LLM client must not be OpenAI-key-only.

`_init_openai_client` previously read only `settings.OPENAI_API_KEY`, so on
Azure-only dev environments it always returned `None` and `_build_draft_content`
silently fell back to the template. It now delegates to
`ExtractionMatrixService._get_openai_client` (Azure-first, same selection the
extraction matrix already uses live).
"""

from __future__ import annotations

from typing import Any, Dict
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import openai
import pytest

pytestmark = pytest.mark.unit

from src.services.research.draft_generation_service import DraftGenerationService


def test_azure_keys_set_returns_azure_client():
    fake_client = MagicMock(spec=openai.AsyncAzureOpenAI)
    with patch(
        "src.services.research.extraction_matrix_service"
        ".ExtractionMatrixService._get_openai_client",
        return_value=(fake_client, "gpt-4o-deployment"),
    ):
        client, model = DraftGenerationService._init_openai_client()

    assert client is fake_client
    assert model == "gpt-4o-deployment"


def test_openai_key_only_returns_openai_client():
    fake_client = MagicMock(spec=openai.AsyncOpenAI)
    with patch(
        "src.services.research.extraction_matrix_service"
        ".ExtractionMatrixService._get_openai_client",
        return_value=(fake_client, "gpt-4o-mini"),
    ):
        client, model = DraftGenerationService._init_openai_client()

    assert client is fake_client
    assert model == "gpt-4o-mini"


def test_no_keys_returns_none_and_template_fallback():
    with patch(
        "src.services.research.extraction_matrix_service"
        ".ExtractionMatrixService._get_openai_client",
        side_effect=RuntimeError("No OpenAI or Azure OpenAI API key configured."),
    ):
        client, model = DraftGenerationService._init_openai_client()

    assert client is None
    assert model == ""


@pytest.mark.asyncio
async def test_build_draft_content_falls_back_to_template_without_client():
    with patch(
        "src.services.research.extraction_matrix_service"
        ".ExtractionMatrixService._get_openai_client",
        side_effect=RuntimeError("No OpenAI or Azure OpenAI API key configured."),
    ):
        service = DraftGenerationService(db=MagicMock())

    assert service._openai_client is None

    content, used_fallback = await service._build_draft_content(
        documents=[],
        themes=["theme a"],
        style="academic",
        max_sections=5,
        include_abstract=True,
    )

    assert "## Abstract" in content
    assert used_fallback is True  # W-B6: degraded drafts must be marked


@pytest.mark.asyncio
async def test_build_draft_with_llm_passes_configured_model():
    fake_client = MagicMock()
    response = MagicMock()
    response.choices = [MagicMock(message=MagicMock(content="Some draft content."))]
    fake_client.chat.completions.create = AsyncMock(return_value=response)

    with patch(
        "src.services.research.extraction_matrix_service"
        ".ExtractionMatrixService._get_openai_client",
        return_value=(fake_client, "my-deployment"),
    ):
        service = DraftGenerationService(db=MagicMock())

    await service._build_draft_with_llm(
        documents=[],
        themes=["theme a"],
        style="academic",
        max_sections=5,
        include_abstract=True,
    )

    kwargs: Dict[str, Any] = fake_client.chat.completions.create.call_args.kwargs
    assert kwargs["model"] == "my-deployment"


@pytest.mark.asyncio
async def test_user_instructions_guide_passage_selection_and_prompt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_client = MagicMock()
    fake_client.chat.completions.create = AsyncMock(
        return_value=MagicMock(
            choices=[MagicMock(message=MagicMock(content="A grounded draft [Doc 1]."))]
        )
    )
    with patch(
        "src.services.research.extraction_matrix_service"
        ".ExtractionMatrixService._get_openai_client",
        return_value=(fake_client, "gpt-4o-test"),
    ):
        service = DraftGenerationService(db=MagicMock())

    captured: dict[str, list[str]] = {}

    def capture_context(
        _cls: type[DraftGenerationService],
        _documents: list[Any],
        max_chars: int = 0,
        queries: list[str] | None = None,
    ) -> str:
        del max_chars
        captured["queries"] = list(queries or [])
        return "[Doc 1] selected evidence"

    monkeypatch.setattr(
        DraftGenerationService,
        "_build_document_context",
        classmethod(capture_context),
    )
    instruction = "Compare methods X and Y directly."
    await service._build_draft_with_llm(
        documents=[MagicMock()],
        themes=["measurement quality"],
        style="academic",
        max_sections=5,
        include_abstract=True,
        instructions=instruction,
    )

    assert captured["queries"] == ["measurement quality", instruction]
    user_prompt = fake_client.chat.completions.create.call_args.kwargs["messages"][1][
        "content"
    ]
    assert instruction in user_prompt


@pytest.mark.asyncio
async def test_gpt5_model_omits_temperature_gpt4o_keeps_it():
    fake_client = MagicMock()
    response = MagicMock()
    response.choices = [MagicMock(message=MagicMock(content="Some draft content."))]
    fake_client.chat.completions.create = AsyncMock(return_value=response)

    with patch(
        "src.services.research.extraction_matrix_service"
        ".ExtractionMatrixService._get_openai_client",
        return_value=(fake_client, "gpt-5-deployment"),
    ):
        service = DraftGenerationService(db=MagicMock())

    await service._build_draft_with_llm(
        documents=[],
        themes=["theme a"],
        style="academic",
        max_sections=5,
        include_abstract=True,
    )

    kwargs = fake_client.chat.completions.create.call_args.kwargs
    assert "temperature" not in kwargs
    # gpt-5 also rejects max_tokens — must use max_completion_tokens.
    assert "max_tokens" not in kwargs
    assert kwargs["max_completion_tokens"] == 4000

    fake_client.chat.completions.create.reset_mock()
    with patch(
        "src.services.research.extraction_matrix_service"
        ".ExtractionMatrixService._get_openai_client",
        return_value=(fake_client, "gpt-4o"),
    ):
        service = DraftGenerationService(db=MagicMock())

    await service._build_draft_with_llm(
        documents=[],
        themes=["theme a"],
        style="academic",
        max_sections=5,
        include_abstract=True,
    )

    kwargs = fake_client.chat.completions.create.call_args.kwargs
    assert kwargs["temperature"] == 0.7
    assert kwargs["max_tokens"] == 4000
    assert "max_completion_tokens" not in kwargs


@pytest.mark.asyncio
async def test_generate_draft_reuses_active_generation():
    """An exact same-process request may reuse its in-flight task."""
    from types import SimpleNamespace
    from unittest.mock import MagicMock, patch
    from uuid import uuid4

    document_id = uuid4()
    user_id = uuid4()
    db = MagicMock()
    query_result = MagicMock()
    query_result.scalars.return_value.all.return_value = [
        SimpleNamespace(id=document_id)
    ]
    db.execute = AsyncMock(return_value=query_result)
    db.commit = AsyncMock()

    with patch(
        "src.services.research.extraction_matrix_service"
        ".ExtractionMatrixService._get_openai_client",
        side_effect=RuntimeError("no key"),
    ):
        service = DraftGenerationService(db=db)

    project_id = uuid4()
    request_hash = service._request_hash(
        project_id=project_id,
        user_id=user_id,
        themes=["t"],
        document_ids=[str(document_id)],
        instructions=None,
        style="academic",
        max_sections=5,
        include_abstract=True,
    )
    active = {
        "task_id": "abc123",
        "status": "generating",
        "user_id": str(user_id),
        "generation_request_hash": request_hash,
    }
    with (
        patch.object(
            DraftGenerationService, "get_latest_status", return_value=active
        ) as get_status,
        patch.object(DraftGenerationService, "_fire_and_forget") as fire,
    ):
        result = await service.generate_draft(
            project_id=project_id,
            user_id=user_id,
            themes=["t"],
        )

    get_status.assert_called_once_with(project_id, user_id=user_id, active_only=True)
    fire.assert_not_called()
    assert result["task_id"] == "abc123"
    assert "already in progress" in result["message"]


@pytest.mark.parametrize(
    ("requested_document_index", "instructions"),
    [(0, "Keep it concise "), (1, "Keep it concise")],
    ids=["exact-instruction-whitespace", "different-document-selection"],
)
@pytest.mark.asyncio
async def test_active_generation_conflicts_on_exact_instruction_or_source_change(
    requested_document_index: int,
    instructions: str,
) -> None:
    from types import SimpleNamespace

    document_ids = [uuid4(), uuid4()]
    project_id, user_id = uuid4(), uuid4()
    db = MagicMock()
    query_result = MagicMock()
    query_result.scalars.return_value.all.return_value = [
        SimpleNamespace(id=document_ids[requested_document_index])
    ]
    db.execute = AsyncMock(return_value=query_result)
    db.commit = AsyncMock()

    with patch(
        "src.services.research.extraction_matrix_service"
        ".ExtractionMatrixService._get_openai_client",
        side_effect=RuntimeError("no key"),
    ):
        service = DraftGenerationService(db=db)

    active_hash = service._request_hash(
        project_id=project_id,
        user_id=user_id,
        themes=["topic"],
        document_ids=[str(document_ids[0])],
        instructions="Keep it concise",
        style="academic",
        max_sections=5,
        include_abstract=True,
    )
    active = {
        "task_id": "active-task-17",
        "status": "generating",
        "user_id": str(user_id),
        "generation_request_hash": active_hash,
    }
    with (
        patch.object(DraftGenerationService, "get_latest_status", return_value=active),
        patch.object(DraftGenerationService, "_fire_and_forget") as fire,
        patch.object(
            DraftGenerationService, "publish_status", new=AsyncMock()
        ) as publish,
    ):
        result = await service.generate_draft(
            project_id=project_id,
            user_id=user_id,
            themes=["topic"],
            document_ids=[document_ids[requested_document_index]],
            instructions=instructions,
        )

    assert result.get("error_category") == "draft_generation_conflict"
    assert "task_id" not in result
    fire.assert_not_called()
    publish.assert_not_awaited()


def test_generation_request_hash_has_scope_version_and_canonical_style() -> None:
    import hashlib
    import json

    project_id, user_id, document_id = uuid4(), uuid4(), uuid4()
    kwargs = {
        "project_id": project_id,
        "user_id": user_id,
        "themes": ["topic"],
        "document_ids": [str(document_id)],
        "instructions": "Keep it concise",
        "max_sections": 5,
        "include_abstract": True,
    }
    actual = DraftGenerationService._request_hash(style=" ACADEMIC ", **kwargs)
    expected_payload = {
        "source_scope_version": 1,
        "project_id": str(project_id),
        "user_id": str(user_id),
        "themes": ["topic"],
        "document_ids": [str(document_id)],
        "instructions": "Keep it concise",
        "style": "academic",
        "max_sections": 5,
        "include_abstract": True,
    }
    expected = hashlib.sha256(
        json.dumps(
            expected_payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
    ).hexdigest()

    assert DraftGenerationService._normalize_style(" ACADEMIC ") == "academic"
    assert actual == expected
    with pytest.raises(ValueError, match="supported draft style"):
        DraftGenerationService._normalize_style("freeform")


def test_generation_registration_locks_are_project_scoped_and_weakly_retained() -> None:
    import gc

    from src.services.research import draft_generation_service as module

    project_a, project_b = uuid4(), uuid4()
    lock_a = module._generation_registration_lock(project_a)
    assert module._generation_registration_lock(project_a) is lock_a
    assert module._generation_registration_lock(project_b) is not lock_a

    key_a = str(project_a)
    del lock_a
    gc.collect()
    assert key_a not in module._generation_registration_locks


@pytest.mark.asyncio
async def test_generation_rechecks_exact_sources_immediately_before_model_dispatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from contextlib import asynccontextmanager
    from types import SimpleNamespace
    from uuid import uuid4

    from src.services.research import draft_generation_service as module

    document_a, document_b = uuid4(), uuid4()
    sessions = iter(
        [
            [SimpleNamespace(id=document_a), SimpleNamespace(id=document_b)],
            [SimpleNamespace(id=document_a)],
        ]
    )
    query_count = 0

    class ReadSession:
        async def execute(self, _statement: Any) -> Any:
            nonlocal query_count
            query_count += 1
            rows = next(sessions)
            return SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: rows))

        async def __aenter__(self) -> ReadSession:
            return self

        async def __aexit__(self, *_args: object) -> None:
            return None

    @asynccontextmanager
    async def session_factory():
        yield ReadSession()

    monkeypatch.setattr(module, "AsyncSessionLocal", session_factory)
    monkeypatch.setattr(module, "finish_task", AsyncMock(return_value=True))

    async def no_sleep(_seconds: float) -> None:
        return None

    monkeypatch.setattr(module.asyncio, "sleep", no_sleep)
    service = DraftGenerationService(db=MagicMock())
    service._set_status = AsyncMock()
    service._record_generation_metrics = MagicMock()
    service._build_draft_content = AsyncMock()

    await service._generate_draft_async(
        task_id="task-source-recheck",
        project_id=uuid4(),
        user_id=uuid4(),
        themes=["topic"],
        document_ids=[document_a, document_b],
        instructions=None,
        style="academic",
        max_sections=5,
        include_abstract=True,
        selection_mode="explicit",
        generation_request_hash="versioned-hash",
    )

    assert query_count == 2
    service._build_draft_content.assert_not_awaited()
    assert (
        service._set_status.await_args_list[-1].args[1]
        == module.DraftGenerationStatus.FAILED
    )


@pytest.mark.asyncio
async def test_revision_prompt_keeps_saved_and_new_instructions_separate() -> None:
    fake_client = MagicMock()
    fake_client.chat.completions.create = AsyncMock(
        return_value=MagicMock(
            choices=[MagicMock(message=MagicMock(content="Revised draft [Doc 1]."))]
        )
    )
    with patch(
        "src.services.research.extraction_matrix_service"
        ".ExtractionMatrixService._get_openai_client",
        return_value=(fake_client, "gpt-5-test"),
    ):
        service = DraftGenerationService(db=MagicMock())

    await service._build_revision_with_llm(
        base_content="Original [Doc 1].",
        original_instructions="Original generation constraints",
        instructions="New revision request",
        mode="revise",
        document_context="[Doc 1] evidence",
    )

    prompt = fake_client.chat.completions.create.call_args.kwargs["messages"][1][
        "content"
    ]
    assert "Original generation constraints" in prompt
    assert "New revision request" in prompt
    assert prompt.index("Original generation constraints") < prompt.index(
        "New revision request"
    )
