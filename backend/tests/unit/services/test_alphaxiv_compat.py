"""Pure wire projection and authority preflight; no provider calls."""

from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock
from uuid import UUID

import pytest
from pydantic import ValidationError

from src.core.config import get_settings
from src.models.document import DocumentType, ProcessingStatus
from src.models.user import User
from src.schemas.alphaxiv import (
    AlphaXivChatFields,
    AlphaXivChatRequest,
    AlphaXivLiveChatRequest,
)
from src.services.agent.agent_execution_service import AgentThreadResolutionError
from src.services.agent.schemas import PageFilterResponse
from src.services.compat import alphaxiv

PAPER = "11111111-1111-4111-8111-111111111111"
CHAT = "22222222-2222-4222-8222-222222222222"
USER = UUID("33333333-3333-4333-8333-333333333333")
ORG = UUID("44444444-4444-4444-8444-444444444444")


def _body(**fields: Any) -> AlphaXivChatRequest:
    body: AlphaXivChatRequest = AlphaXivChatRequest.model_validate(
        {"message": "Explain the results", "paperVersionId": PAPER, **fields}
    )
    return body


def _user() -> User:
    return cast(User, SimpleNamespace(id=USER, organization_id=ORG))


@pytest.fixture(params=[AlphaXivChatRequest, AlphaXivLiveChatRequest], ids=["v2", "v3"])
def request_type(request: pytest.FixtureRequest) -> type[AlphaXivChatFields]:
    return cast(type[AlphaXivChatFields], request.param)


@pytest.fixture
def accessible_pdf(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        alphaxiv,
        "get_accessible_document_or_none",
        AsyncMock(
            return_value=SimpleNamespace(
                document_type=DocumentType.PDF,
                processing_status=ProcessingStatus.COMPLETED,
            )
        ),
    )


@pytest.mark.asyncio
async def test_observed_pro_request_uses_native_defaults_without_rewriting_presentation(
    request_type: type[AlphaXivChatFields], accessible_pdf: None
) -> None:
    body = request_type.model_validate(
        {
            "message": "> Sequence\n\nExplain the method and cite relevant pages.",
            "paperVersionId": PAPER,
            "llmChatId": None,
            "parentMessageId": None,
            "model": "pro",
            "files": [],
            "thinking": None,
            "webSearch": "full",
            "selectionPageRange": None,
            "assistantVariant": "paper",
        }
    )
    original = body.model_dump()
    native = await alphaxiv.prepare_request(body, AsyncMock(), _user())
    assert native.model == ""
    assert native.page_filter_model == "cohere"
    assert native.selection_page_range is None
    assert native.document_only is False
    assert native.messages[0].content == body.message
    # Both transports read this body for run_start/durable presentation.
    assert body.model_dump() == original


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "filter_fields", [{}, {"filterModel": None}, {"filterModel": "cohere"}]
)
@pytest.mark.parametrize("page_range", [None, [1, 1]])
async def test_compat_filter_defaults_and_explicit_range_precedence(
    request_type: type[AlphaXivChatFields],
    accessible_pdf: None,
    filter_fields: dict[str, Any],
    page_range: list[int] | None,
) -> None:
    body = request_type.model_validate(
        {
            "message": "Explain the PDF",
            "paperVersionId": PAPER,
            "selectionPageRange": page_range,
            **filter_fields,
        }
    )
    native = await alphaxiv.prepare_request(body, AsyncMock(), _user())
    assert native.page_filter_model == (None if page_range is not None else "cohere")
    assert native.selection_page_range == (tuple(page_range) if page_range else None)


@pytest.mark.asyncio
@pytest.mark.parametrize("model", ["", "model-router", "gpt-5-mini", "gpt-5.6-luna"])
async def test_compat_explicit_native_models_are_preserved(
    request_type: type[AlphaXivChatFields], accessible_pdf: None, model: str
) -> None:
    body = request_type.model_validate(
        {"message": "Explain", "paperVersionId": PAPER, "model": model}
    )
    native = await alphaxiv.prepare_request(body, AsyncMock(), _user())
    assert native.model == model
    assert body.model == model


@pytest.mark.asyncio
@pytest.mark.parametrize("model", ["smart", "unknown-preset", "unconfigured-model"])
async def test_compat_unknown_presets_and_models_fail_before_access(
    request_type: type[AlphaXivChatFields], monkeypatch: pytest.MonkeyPatch, model: str
) -> None:
    access = AsyncMock()
    monkeypatch.setattr(alphaxiv, "get_accessible_document_or_none", access)
    body = request_type.model_validate(
        {"message": "Explain", "paperVersionId": PAPER, "model": model}
    )
    with pytest.raises(alphaxiv.CompatibilityError) as caught:
        await alphaxiv.prepare_request(body, AsyncMock(), _user())
    assert caught.value.status_code == 422
    access.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("effort", ["low", "medium", "high"])
async def test_pro_thinking_validates_the_resolved_native_deployment(
    request_type: type[AlphaXivChatFields],
    accessible_pdf: None,
    monkeypatch: pytest.MonkeyPatch,
    effort: str,
) -> None:
    settings = get_settings()
    for name, value in {
        "AZURE_OPENAI_CHAT_ENDPOINT": "https://example.cognitiveservices.azure.com/",
        "AZURE_OPENAI_CHAT_API_VERSION": "2025-04-01-preview",
        "AZURE_OPENAI_CHAT_DEPLOYMENT_NAME": "gpt-5-mini",
        "AGENT_USE_RESPONSES_API": True,
    }.items():
        monkeypatch.setattr(settings, name, value)
    body = request_type.model_validate(
        {
            "message": "Explain",
            "paperVersionId": PAPER,
            "model": "pro",
            "thinking": effort,
            "webSearch": "off",
        }
    )
    native = await alphaxiv.prepare_request(body, AsyncMock(), _user())
    assert native.model == ""
    assert native.reasoning_effort == effort
    assert body.model == "pro"
    assert body.thinking == effort


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "deployment,web_search,responses,reason",
    [
        ("unsupported-deployment", "off", True, "supported NOUS GPT-5 model"),
        ("gpt-5-mini", "full", True, "document-only"),
        ("gpt-5-mini", "off", False, "Responses API"),
    ],
)
async def test_pro_thinking_does_not_bypass_native_capability_checks(
    request_type: type[AlphaXivChatFields],
    monkeypatch: pytest.MonkeyPatch,
    deployment: str,
    web_search: str,
    responses: bool,
    reason: str,
) -> None:
    monkeypatch.setattr(get_settings(), "AZURE_OPENAI_CHAT_DEPLOYMENT_NAME", deployment)
    monkeypatch.setattr(get_settings(), "AGENT_USE_RESPONSES_API", responses)
    access = AsyncMock()
    monkeypatch.setattr(alphaxiv, "get_accessible_document_or_none", access)
    body = request_type.model_validate(
        {
            "message": "Explain",
            "paperVersionId": PAPER,
            "model": "pro",
            "thinking": "high",
            "webSearch": web_search,
        }
    )
    with pytest.raises(alphaxiv.CompatibilityError, match=reason) as caught:
        await alphaxiv.prepare_request(body, AsyncMock(), _user())
    assert caught.value.status_code == 422
    access.assert_not_awaited()


@pytest.mark.parametrize("pages", [[-1, 1], [2, 1], [True, 1], [1.5, 2], [0]])
def test_selection_requires_ordered_integer_indices(pages: list[Any]) -> None:
    with pytest.raises(ValidationError):
        _body(selectionPageRange=pages)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "field,value",
    [
        ("customFilter", {"apiKey": "secret", "url": "http://localhost"}),
        ("filterModel", "unknown-filter"),
        ("thinking", True),
        ("webSearch", "unknown"),
        ("overviewId", CHAT),
        ("model", "unconfigured-model"),
    ],
)
async def test_unsupported_options_fail_before_document_access(
    monkeypatch: pytest.MonkeyPatch, field: str, value: Any
) -> None:
    access = AsyncMock()
    monkeypatch.setattr(alphaxiv, "get_accessible_document_or_none", access)
    with pytest.raises(alphaxiv.CompatibilityError) as caught:
        await alphaxiv.prepare_request(_body(**{field: value}), AsyncMock(), _user())
    assert caught.value.status_code == 422
    assert "secret" not in str(caught.value)
    access.assert_not_awaited()


@pytest.mark.asyncio
async def test_document_denial_uses_actor_and_org_before_native_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    access = AsyncMock(return_value=None)
    monkeypatch.setattr(alphaxiv, "get_accessible_document_or_none", access)
    db = AsyncMock()
    with pytest.raises(alphaxiv.CompatibilityError) as caught:
        await alphaxiv.prepare_request(_body(), db, _user())
    assert caught.value.status_code == 404
    access.assert_awaited_once_with(db, UUID(PAPER), USER, ORG)


@pytest.mark.asyncio
@pytest.mark.parametrize("web_search,document_only", [("off", True), ("full", False)])
async def test_owned_pdf_maps_into_canonical_request(
    monkeypatch: pytest.MonkeyPatch, web_search: str, document_only: bool
) -> None:
    monkeypatch.setattr(
        alphaxiv,
        "get_accessible_document_or_none",
        AsyncMock(
            return_value=SimpleNamespace(
                document_type=DocumentType.PDF,
                processing_status=ProcessingStatus.COMPLETED,
            )
        ),
    )
    resolve = AsyncMock(return_value=(SimpleNamespace(id=CHAT), "conversation"))
    monkeypatch.setattr(alphaxiv, "_resolve_thread", resolve)
    body = _body(
        llmChatId=CHAT,
        parentMessageId=CHAT,
        selectionPageRange=[0, 2],
        filterModel="cohere",
        webSearch=web_search,
    )
    user = _user()
    db = AsyncMock()
    native = await alphaxiv.prepare_request(body, db, user)
    assert native.thread_id == CHAT
    assert native.attachment_ids == [UUID(PAPER)]
    assert native.selection_page_range == (0, 2)
    assert native.page_filter_model is None
    assert native.document_only is document_only
    assert native.branch_document_id == UUID(PAPER)
    assert native.parent_message_id == UUID(CHAT)
    assert native.messages[0].client_message_id is not None
    assert native.messages[0].content == body.message
    resolve.assert_awaited_once_with(db, user, native, create_if_missing=False)


@pytest.mark.asyncio
async def test_inaccessible_existing_chat_never_becomes_new_chat(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        alphaxiv,
        "get_accessible_document_or_none",
        AsyncMock(
            return_value=SimpleNamespace(
                document_type=DocumentType.PDF,
                processing_status=ProcessingStatus.COMPLETED,
            )
        ),
    )
    monkeypatch.setattr(
        alphaxiv,
        "_resolve_thread",
        AsyncMock(side_effect=AgentThreadResolutionError("private details")),
    )
    with pytest.raises(alphaxiv.CompatibilityError, match="^Chat not found$") as caught:
        await alphaxiv.prepare_request(_body(llmChatId=CHAT), AsyncMock(), _user())
    assert caught.value.status_code == 404


def test_split_native_doc_citation_uses_only_authorized_page_provenance() -> None:
    projection = alphaxiv.EventProjection(PAPER)
    projection.project(
        "rag_context",
        {
            "contexts": [
                {
                    "source_position": 1,
                    "document_id": PAPER,
                    "page_number": 3,
                    "title": 'A "result"',
                },
                {
                    "source_position": 2,
                    "document_id": CHAT,
                    "page_number": 8,
                    "title": "Other PDF",
                },
                {"source_position": 3, "document_id": PAPER, "page_number": None},
            ]
        },
    )
    events: list[dict[str, Any]] = []
    for chunk in ("Result [D", "oc ", "1] and [Doc 2] [Doc 3] [Doc 9]."):
        events.extend(projection.project("token", {"content": chunk}))
    value = "".join(event["delta"] for event in events)
    assert f'paper="{PAPER}" page="3"' in value
    assert 'title="A &quot;result&quot;"' in value
    assert "[Doc 1]" not in value
    assert "[Doc 2] [Doc 3] [Doc 9]" in value
    assert {event["index"] for event in events} == {0}


@pytest.mark.parametrize("replacement_pages", [[], [7]])
def test_latest_source_snapshot_replaces_prior_page_attribution(
    replacement_pages: list[int],
) -> None:
    projection = alphaxiv.EventProjection(PAPER)

    def snapshot(pages: list[int]) -> dict[str, Any]:
        return {
            "contexts": [
                {
                    "source_position": index,
                    "document_id": PAPER,
                    "page_number": page,
                }
                for index, page in enumerate(pages, start=1)
            ]
        }

    projection.project("rag_context", snapshot([3, 4]))
    projection.project("rag_context", snapshot(replacement_pages))
    events = projection.project("token", {"content": "[Doc 1] [Doc 2]"})
    text = "".join(event["delta"] for event in events)
    assert 'page="3"' not in text
    assert 'page="4"' not in text
    assert "[Doc 2]" in text
    if replacement_pages:
        assert 'page="7"' in text
    else:
        assert text == "[Doc 1] [Doc 2]"


def test_actual_filter_metadata_maps_without_fabricated_measurements() -> None:
    projection = alphaxiv.EventProjection(PAPER)
    assert (
        projection.project("rag_context", {"page_filter": {"accepted_pages": [2]}})
        == []
    )
    timing = {"average": 21.0, "min": 21.0, "max": 21.0, "count": 1}
    result = projection.project(
        "rag_context",
        {
            "page_filter": {
                "accepted_pages": [2, 4],
                "total_pages": 12,
                "short_circuited": False,
                "model": "configured-reranker",
                "timing_ms": timing,
                "priorities": [{"page": 2, "score": 0.9}, {"page": 4, "score": 0.7}],
            }
        },
    )
    assert result == [
        {
            "type": "paper_filter_debug",
            "acceptedPages": [2, 4],
            "totalPages": 12,
            "shortCircuited": False,
            "filterModel": "configured-reranker",
            "timingMs": timing,
            "acceptedPagePriorities": [
                {"page": 2, "priority": 0.9},
                {"page": 4, "priority": 0.7},
            ],
        }
    ]


@pytest.mark.parametrize("selection", [None, (0, 0)])
def test_serialized_unmeasured_filter_does_not_emit_null_timing(
    selection: tuple[int, int] | None,
) -> None:
    # Match the native SSE serialization, which retains optional null fields.
    metadata = PageFilterResponse(
        document_id=UUID(PAPER),
        accepted_pages=[1],
        total_pages=1,
        short_circuited=True,
        selection_page_range=selection,
    ).model_dump(mode="json")
    assert metadata["timing_ms"] is None
    assert (
        alphaxiv.EventProjection(PAPER).project(
            "rag_context", {"page_filter": metadata}
        )
        == []
    )


def test_truncated_eof_and_approval_are_not_success() -> None:
    projection = alphaxiv.EventProjection(PAPER)
    projection.project("token", {"content": "Incomplete [Doc "})
    events = projection.finish()
    assert events[0]["delta"] == "[Doc "
    assert events[-2]["type"] == "error"
    assert events[-1] == {"type": "run_end", "kind": "error"}
    assert projection.finish() == []
    approval = alphaxiv.EventProjection(PAPER).project("confirmation", {})
    assert approval[0] == {
        "type": "error",
        "error": "This response requires approval in NOUS",
    }
    assert approval[-1]["kind"] == "error"


def test_reasoning_and_output_keep_distinct_stable_content_indices() -> None:
    projection = alphaxiv.EventProjection(PAPER)
    events = projection.project("reasoning_delta", {"content": "Summary"})
    events += projection.project("token", {"content": "Answer"})
    events += projection.project("token", {"content": " continues"})
    assert [event["index"] for event in events] == [0, 1, 1]
    assert projection.project("done", {}) == [{"type": "run_end", "kind": "completed"}]
    assert projection.project("error", {"error": "late"}) == []


@pytest.mark.parametrize(
    "page_range", [[0, 100], [5, 2], [-1, 3]], ids=["too-wide", "reversed", "negative"]
)
def test_selection_page_range_matches_native_bounds(page_range: list[int]) -> None:
    with pytest.raises(ValidationError):
        _body(selectionPageRange=page_range)
    assert _body(selectionPageRange=[0, 99]).selection_page_range == (0, 99)
