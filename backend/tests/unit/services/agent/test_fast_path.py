from types import SimpleNamespace

import pytest


def _message(role: str, content: str):
    return SimpleNamespace(role=role, content=content)


def _decide(
    content: str,
    *,
    use_rag: bool = False,
    page_type: str = "chat",
    project_id: str | None = None,
    history: list | None = None,
    has_attachments: bool = False,
):
    from src.services.agent.fast_path import classify_fast_path_turn

    messages = [*(history or []), _message("user", content)]
    return classify_fast_path_turn(
        messages=messages,
        page_context={"type": page_type, "project_id": project_id},
        use_rag=use_rag,
        max_input_chars=8_000,
        has_attachments=has_attachments,
    )


@pytest.mark.parametrize("content", ["hi", "Thanks!", "okay", "goodbye"])
def test_bare_conversation_uses_fast_path_even_when_rag_toggle_is_on(content):
    decision = _decide(content, use_rag=True)

    assert decision.eligible is True
    assert decision.reason == "bare_conversation"


@pytest.mark.parametrize(
    "content",
    [
        "Search arXiv for recent RAG papers",
        "Find the documents I uploaded yesterday",
        "Create a note in my project",
        "Summarize this paper",
        "Compare these sources",
    ],
)
def test_tool_or_evidence_language_fails_closed_to_langgraph(content):
    decision = _decide(content)

    assert decision.eligible is False
    assert decision.reason == "agent_capability_required"


def test_explicit_rag_fails_closed_for_non_conversational_question():
    decision = _decide("Explain the transformer attention mechanism", use_rag=True)

    assert decision.eligible is False
    assert decision.reason == "rag_requested"


def test_attachment_turn_fails_closed_to_grounded_graph_path_when_rag_is_off():
    decision = _decide(
        "What is the launch code?",
        use_rag=False,
        has_attachments=True,
    )

    assert decision.eligible is False
    assert decision.reason == "attachments_require_grounding"


@pytest.mark.parametrize("page_type", ["project", "documents"])
def test_grounded_page_context_fails_closed(page_type):
    decision = _decide("Rewrite this more clearly", page_type=page_type)

    assert decision.eligible is False
    assert decision.reason == "grounded_page_context"


def test_project_id_fails_closed_even_when_page_type_is_chat():
    decision = _decide(
        "Rewrite this more clearly",
        page_type="chat",
        project_id="4a370aff-0347-4e51-8cf5-e67999232b47",
    )

    assert decision.eligible is False
    assert decision.reason == "grounded_page_context"


@pytest.mark.parametrize(
    "content",
    ["What about the previous result?", "Try again", "Tell me more about that"],
)
def test_ambiguous_follow_up_fails_closed(content):
    decision = _decide(
        content,
        history=[_message("assistant", "I searched your project.")],
    )

    assert decision.eligible is False
    assert decision.reason == "context_dependent"


def test_ambiguous_follow_up_fails_closed_when_client_omits_history():
    decision = _decide("Try again")

    assert decision.eligible is False
    assert decision.reason == "context_dependent"


@pytest.mark.parametrize(
    "content",
    [
        "Explain why the sky appears blue",
        "Brainstorm five names for a research newsletter",
        "Rewrite this sentence in a warmer tone: The proposal was rejected.",
    ],
)
def test_ungrounded_non_tool_request_uses_fast_path_when_rag_is_off(content):
    decision = _decide(content, use_rag=False)

    assert decision.eligible is True
    assert decision.reason == "ungrounded_generation"


def test_oversized_context_fails_closed():
    decision = _decide("Explain this: " + ("x" * 8_001), use_rag=False)

    assert decision.eligible is False
    assert decision.reason == "context_budget_exceeded"


def test_no_user_message_fails_closed():
    from src.services.agent.fast_path import classify_fast_path_turn

    decision = classify_fast_path_turn(
        messages=[_message("assistant", "hello")],
        page_context={"type": "chat"},
        use_rag=False,
        max_input_chars=8_000,
    )

    assert decision.eligible is False
    assert decision.reason == "missing_user_message"
