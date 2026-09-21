"""Regression tests for classifier intent routing.

Covers the disambiguation between the indexed document corpus
("knowledge base", "our docs") — which routes to the ``research`` intent
and the ``do_kb_retrieve`` tool — and the Neo4j entity graph
("knowledge graph", "extract entities") — which routes to the
``knowledge_graph`` intent and the data subgraph.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from src.services.agent.classifier import (
    ClassificationResult,
    classify_intent_keywords,
    classify_intent_with_fallback,
)


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("query", "expected_intent"),
    [
        (
            "Compare the documents in our organization knowledge base about transformers.",
            "writing",
        ),
        ("Write a summary using our knowledge base evidence.", "writing"),
        ("Summarize our knowledge base evidence.", "writing"),
        ("Export a bibliography from our knowledge base.", "writing"),
        ("Extract entities from our knowledge base.", "knowledge_graph"),
        (
            "Explore relationships in our organization knowledge base.",
            "knowledge_graph",
        ),
        ("Find relationships in our knowledge base.", "knowledge_graph"),
        ("Search our docs and explain the retrieval policy.", "research"),
        ("Don't use Python. Export a bibliography from our knowledge base.", "writing"),
        (
            "Extract the entities mentioned across our knowledge base.",
            "knowledge_graph",
        ),
        ("Explain the phrase 'extract entities'; don't run tools.", "general"),
    ],
)
async def test_kb_actions_preserve_explicit_action_intent(
    query: str, expected_intent: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Policy test: a semantic verdict must not be pre-empted by phrase matches.
    semantic = AsyncMock(
        return_value=ClassificationResult(
            intent=expected_intent, confidence=0.95, reasoning="fixture", source="llm"
        )
    )
    monkeypatch.setattr("src.services.agent.classifier.classify_intent_llm", semantic)
    result = await classify_intent_with_fallback(query, page_context={})
    semantic.assert_awaited_once()
    assert result.intent == expected_intent
    assert result.source == "llm"


@pytest.mark.unit
def test_kb_query_classified_research() -> None:
    """KB queries route to research so do_kb_retrieve becomes available."""
    result = classify_intent_keywords(
        "What does our knowledge base say about transformer attention?"
    )
    assert result.intent == "research"
    assert result.source == "keyword"
    assert result.confidence > 0


@pytest.mark.unit
@pytest.mark.asyncio
async def test_kb_query_rejects_semantic_graph_confusion(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    semantic = AsyncMock(
        return_value=ClassificationResult(
            intent="knowledge_graph",
            confidence=0.95,
            reasoning="misread knowledge base as entity graph",
            source="llm",
        )
    )
    monkeypatch.setattr("src.services.agent.classifier.classify_intent_llm", semantic)

    result = await classify_intent_with_fallback(
        "What does our knowledge base say about transformer attention?",
        page_context={},
    )

    assert result.intent == "research"
    assert result.source == "keyword"


@pytest.mark.unit
def test_our_docs_classified_research() -> None:
    """'our docs' phrasing routes to research."""
    result = classify_intent_keywords("Search our docs for RLHF")
    assert result.intent == "research"


@pytest.mark.unit
def test_our_library_classified_research() -> None:
    """'our library' phrasing routes to research."""
    result = classify_intent_keywords("Look up our library for diffusion papers")
    assert result.intent == "research"


@pytest.mark.unit
def test_kg_query_classified_knowledge_graph() -> None:
    """Regression guard: 'knowledge graph' still routes to knowledge_graph."""
    result = classify_intent_keywords(
        "Search the knowledge graph for entities related to BERT"
    )
    assert result.intent == "knowledge_graph"


@pytest.mark.unit
def test_extract_entities_classified_knowledge_graph() -> None:
    """Regression guard: explicit entity-extraction intent unchanged."""
    result = classify_intent_keywords("Extract entities from this paper")
    assert result.intent == "knowledge_graph"


@pytest.mark.unit
def test_kb_phrase_does_not_substring_match_skbio() -> None:
    """Sanity check: 'kb' was deliberately omitted from keyword list to avoid
    matching tokens like 'skbio'. A query about scikit-bio must not be
    classified as research on the basis of 'kb' alone."""
    result = classify_intent_keywords("Tell me about skbio versions")
    assert result.intent == "general"
