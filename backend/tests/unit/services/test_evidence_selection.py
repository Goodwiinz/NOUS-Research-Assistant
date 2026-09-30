import pytest

from src.services.research.draft_generation_service import DraftGenerationService
from src.services.research.evidence_selection import (
    evidence_location,
    select_relevant_passages,
)


def test_selector_finds_attention_benchmark_evidence_beyond_prefix() -> None:
    source = (
        "[Page 1]\n"
        + ("background unrelated to benchmarks. " * 180)
        + "\n[Page 8]\nThe model achieved 28.4 BLEU on English-to-German."
        + "\n[Page 9]\nIt achieved 41.8 BLEU on English-to-French."
        + "\n[Page 12]\nTraining took 3.5 days on 8 GPUs."
    )

    selected = select_relevant_passages(
        source,
        queries=["28.4 BLEU 41.8 BLEU 3.5 days 8 GPUs"],
        max_chars=700,
    )

    assert "28.4 BLEU" in selected
    assert "41.8 BLEU" in selected
    assert "3.5 days on 8 GPUs" in selected
    assert "[Page 8]" in selected
    assert "[Page 9]" in selected
    assert "[Page 12]" in selected
    assert len(selected) <= 700


def test_selector_does_not_invent_page_for_legacy_text() -> None:
    selected = select_relevant_passages(
        "Legacy extracted prose says the result was 28.4 BLEU.",
        queries=["28.4 BLEU"],
        max_chars=200,
    )

    assert "28.4 BLEU" in selected
    assert "[Page" not in selected


def test_selector_centers_tight_budget_on_late_match() -> None:
    source = "[Page 8]\n" + ("unrelated " * 80) + "28.4 BLEU benchmark result."

    selected = select_relevant_passages(source, queries=["28.4 BLEU"], max_chars=100)

    assert "[Page 8]" in selected
    assert "28.4 BLEU" in selected
    assert len(selected) <= 100


def test_selector_prefers_late_numeric_match_over_early_generic_match() -> None:
    source = (
        "[Page 8]\nBenchmark context "
        + ("unrelated " * 80)
        + "the model achieved 28.4 BLEU."
    )

    selected = select_relevant_passages(
        source,
        queries=["benchmark result 28.4 BLEU"],
        max_chars=100,
    )

    assert "[Page 8]" in selected
    assert "28.4 BLEU" in selected
    assert len(selected) <= 100


_PAPER = (
    "[Page 2]\nBackground on sequence transduction models.\n"
    "[Page 3]\nThe Transformer is the first transduction model relying entirely "
    "on self-attention to compute representations of its input and output."
)
_VERBATIM = (
    "The Transformer is the first transduction model relying entirely on "
    "self-attention to compute representations of its input and output."
)


def test_narrative_wrapped_verbatim_quote_is_located() -> None:
    """GOO-292: the live verifier wraps its quote in narrative.

    Evidence shape observed on dev (review be869bdc…): ``The excerpt
    explicitly states: “…” It also explains …``. The inner curly-quoted span is
    verbatim source text, so it must be located on its page.

    Mutation check (2026-09-30): making ``_quote_candidates`` return only the
    whole evidence string (the pre-fix behaviour) makes this test fail with
    ``(None, "source excerpt")``. Restoring the quoted-span extraction makes it
    pass.
    """
    evidence = (
        f"The excerpt explicitly states: “{_VERBATIM}” It also explains how "
        "the architecture dispenses with recurrence."
    )

    assert evidence_location(_PAPER, evidence) == (3, "Page 3")
    # Straight quotes and a bare narrative prefix are located the same way.
    assert evidence_location(_PAPER, f'The source says "{_VERBATIM}"') == (
        3,
        "Page 3",
    )
    assert evidence_location(_PAPER, f"The excerpt states: {_VERBATIM}") == (
        3,
        "Page 3",
    )
    # A bare verbatim quote keeps working.
    assert evidence_location(_PAPER, _VERBATIM) == (3, "Page 3")


def test_paraphrased_or_fabricated_evidence_is_not_located() -> None:
    """Unlocated evidence must stay ``source excerpt`` so the gate fails closed.

    Mutation check (2026-09-30): making ``evidence_location`` return
    ``(None, "legacy unanchored text")`` whenever the evidence is non-empty
    (accepting unlocated evidence) makes this test fail. Restoring the
    verbatim search makes it pass.
    """
    paraphrase = (
        "The excerpt explicitly states: “The Transformer is the first model to "
        "use only attention for transduction.” It also explains recurrence."
    )
    short_fragment = "The source mentions “the Transformer” and invents the rest."

    assert evidence_location(_PAPER, paraphrase) == (None, "source excerpt")
    assert evidence_location(_PAPER, short_fragment) == (None, "source excerpt")

    review = {
        "docs_skipped": 0,
        "coverage": {"complete": True, "factual_classification_complete": False},
        "uncited_assertions": [],
        "verdicts": [
            {
                "doc_index": 1,
                "verdict": "exact",
                "evidence": paraphrase,
                "location": evidence_location(_PAPER, paraphrase)[1],
            }
        ],
    }
    with pytest.raises(ValueError, match="grounded evidence"):
        DraftGenerationService._require_passing_citation_review(review, [1])

    review["verdicts"][0]["evidence"] = f"The excerpt states: “{_VERBATIM}”"
    review["verdicts"][0]["location"] = evidence_location(
        _PAPER, review["verdicts"][0]["evidence"]
    )[1]
    DraftGenerationService._require_passing_citation_review(review, [1])
