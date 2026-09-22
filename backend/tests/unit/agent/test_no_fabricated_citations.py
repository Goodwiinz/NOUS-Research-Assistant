"""Regression tests for the empty-retrieval anti-fabrication guidance.

User-reproduced 2026-07-04: with RAG returning nothing, the static
"[Doc N]" citation rule led the model to improvise a full "Cited
sources" bibliography from parametric memory (unverifiable references).
The fix injects explicit no-retrieval guidance instead of silently
omitting the context block, and conditions the static citation rule on
documents actually being present.
"""

from __future__ import annotations

import pytest

from src.services.agent._nodes_llm import NO_RETRIEVAL_GUIDANCE, _retrieval_context_part
from src.services.agent._prompts import _LLM_NODE_STATIC_PROMPT


@pytest.mark.unit
class TestRetrievalContextPart:
    def test_empty_retrieval_injects_anti_fabrication_guidance(self):
        part = _retrieval_context_part([])
        assert part == NO_RETRIEVAL_GUIDANCE
        assert "do NOT invent citations" in part
        assert "nothing relevant was found" in part

    def test_non_empty_retrieval_renders_doc_blocks(self):
        part = _retrieval_context_part(
            [
                {"title": "Attention Is All You Need", "content": "abstract..."},
                {"title": "Transformer-XL", "content": "longer context..."},
            ]
        )
        assert part.startswith("Retrieved context:")
        assert "[Doc 1]" in part
        assert "title: Attention Is All You Need" in part
        assert "[Doc 2]" in part
        assert "title: Transformer-XL" in part
        assert NO_RETRIEVAL_GUIDANCE not in part


@pytest.mark.unit
class TestStaticCitationRule:
    def test_citation_rule_is_conditioned_on_retrieved_docs(self):
        # The rule must scope [Doc N] to actually-retrieved documents and
        # forbid fabricated bibliographies — not just say "cite sources".
        assert "ONLY for documents that appear" in _LLM_NODE_STATIC_PROMPT
        assert "Never fabricate citations" in _LLM_NODE_STATIC_PROMPT
