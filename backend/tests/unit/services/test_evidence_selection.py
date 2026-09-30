import pytest

from src.services.research.citation_verification_service import _sanitize_excerpt
from src.services.research.draft_generation_service import DraftGenerationService
from src.services.research.evidence_selection import (
    evidence_location,
    select_relevant_passages,
    verbatim_evidence,
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
_UNGROUNDED = (None, "source excerpt", None)


def test_narrative_wrapped_verbatim_quote_is_located() -> None:
    """GOO-292: the live verifier wraps its quote in narrative.

    Evidence shape observed on dev (review be869bdc…): ``The excerpt
    explicitly states: “…” It also explains …``. The inner curly-quoted span is
    verbatim source text, so it is located on its page and returned alone.

    Mutation check (2026-09-30): making ``_grounded_span`` consider only the
    whole evidence string (the pre-fix behaviour) makes this test fail with
    ``(None, "source excerpt", None)``. Restoring the quoted-span extraction
    makes it pass.
    """
    evidence = (
        f"The excerpt explicitly states: “{_VERBATIM}” It also explains how "
        "the architecture dispenses with recurrence."
    )

    assert evidence_location(_PAPER, evidence) == (3, "Page 3", _VERBATIM)
    assert evidence_location(_PAPER, f'The source says "{_VERBATIM}"') == (
        3,
        "Page 3",
        _VERBATIM,
    )
    # A bare verbatim quote keeps working.
    assert evidence_location(_PAPER, _VERBATIM) == (3, "Page 3", _VERBATIM)


def test_paraphrased_or_fabricated_evidence_is_not_located() -> None:
    """Unlocated evidence must stay ``source excerpt`` so the gate fails closed.

    Mutation check (2026-09-30): making ``evidence_location`` return
    ``(None, "legacy unanchored text", evidence)`` whenever the evidence is
    non-empty (accepting unlocated evidence) makes this test fail. Restoring
    the verbatim search makes it pass.
    """
    paraphrase = (
        "The excerpt explicitly states: “The Transformer is the first model to "
        "use only attention for transduction.” It also explains recurrence."
    )
    short_fragment = "The source mentions “the Transformer” and invents the rest."
    # A narrative prefix without quote marks can carry a fabricated claim.
    fabricated_prefix = (
        "It reaches 99% BLEU on all tasks: The Transformer is the first "
        "transduction model"
    )

    assert evidence_location(_PAPER, paraphrase) == _UNGROUNDED
    assert evidence_location(_PAPER, short_fragment) == _UNGROUNDED
    assert evidence_location(_PAPER, fabricated_prefix) == _UNGROUNDED

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


def test_real_span_cannot_carry_a_fabricated_span() -> None:
    """Every quoted span of 4+ words must be verbatim, not just one.

    Mutation check (2026-09-30): replacing ``all(occurs(span) ...)`` with
    ``any(...)`` in ``_grounded_span`` makes this test fail (located as Page 3).
    Restoring ``all`` makes it pass.
    """
    evidence = (
        "“The Transformer is the first” and the paper also states "
        "“it achieves 99% accuracy on every benchmark”"
    )

    assert evidence_location(_PAPER, evidence) == _UNGROUNDED
    assert verbatim_evidence(_PAPER, evidence) is None


def test_negated_narrative_does_not_ground_its_quote() -> None:
    """Narrative that denies its own verbatim quote must not ground a verdict.

    Mutation check (2026-09-30): deleting the ``_NEGATION_RE`` check in
    ``_grounded_span`` makes this test fail. Restoring it makes it pass.
    """
    for evidence in (
        f"The excerpt does not say “{_VERBATIM}”",
        f"The claim contradicts the passage “{_VERBATIM}”",
        f"The excerpt doesn’t support this; it only says “{_VERBATIM}”",
    ):
        assert evidence_location(_PAPER, evidence) == _UNGROUNDED, evidence


def test_short_bare_quote_does_not_ground() -> None:
    """A bare one-word or one-letter "quote" is too generic to be evidence.

    Mutation check (2026-09-30): dropping ``_long_enough(bare)`` from the bare
    candidate in ``_grounded_span`` makes this test fail (``Transformer`` and
    ``e`` are located). Restoring it makes it pass.
    """
    for evidence in ("Transformer", "e", "“self-attention”", "input and output"):
        assert evidence_location(_PAPER, evidence) == _UNGROUNDED, evidence
        assert verbatim_evidence(_PAPER, evidence) is None, evidence


_TWO_PAGE_PAPER = (
    "[Page 4]\nWe train on the WMT 2014 English-German dataset.\n\n"
    "Sentences were encoded using byte-pair encoding.\n"
    "[Page 5]\nThe shared vocabulary has about 37000 tokens."
)


def test_quote_crossing_a_passage_boundary_is_located_on_its_start_page() -> None:
    """GOO-321 D-02 (GOO-292): the verifier's excerpt joins passages with spaces, so its
    quote can cross a paragraph or page boundary while still being verbatim.

    Mutation check (2026-09-30): restoring the pre-fix per-passage search in
    ``evidence_location`` (``needle in passage_text`` for each passage from
    ``_passages``) makes the two boundary-crossing asserts fail with
    ``(None, "source excerpt", None)``. Restoring the joined search makes it
    pass.
    """
    across_paragraph = (
        "the WMT 2014 English-German dataset. Sentences were encoded using"
    )
    across_page = "encoded using byte-pair encoding. The shared vocabulary has"

    assert evidence_location(_TWO_PAGE_PAPER, across_paragraph) == (
        4,
        "Page 4",
        across_paragraph,
    )
    assert evidence_location(_TWO_PAGE_PAPER, across_page) == (
        4,
        "Page 4",
        across_page,
    )
    # The stored span is the source's own text, not the model's casing.
    assert evidence_location(_TWO_PAGE_PAPER, across_page.upper())[2] == across_page
    # A paraphrase across the same boundary is still rejected.
    paraphrase = "the WMT 2014 English-German dataset. Sentences were tokenised using"
    assert evidence_location(_TWO_PAGE_PAPER, paraphrase) == _UNGROUNDED


_RAW_PDF_PAPER = (
    "[Page 7]\nThe ﬁnal model improves training eﬃ-\nciency by a factor of "
    "three over the base\u00adline."
)


def test_pdf_ligatures_and_line_break_hyphens_match_repaired_quotes() -> None:
    """GOO-321 D-02 (GOO-292): a quote with repaired PDF typography is still verbatim.

    Mutation check (2026-09-30): dropping the typographic folding in
    ``_normalize_with_offsets`` (``folded = char`` instead of NFKC, and
    ``_JOIN_RE = re.compile(r"\\s+")`` without the soft-hyphen and
    de-hyphenation alternatives) makes this test fail with
    ``(None, "source excerpt") == (7, "Page 7")``. Restoring it makes it pass.
    """
    repaired = "The final model improves training efficiency by a factor of three"
    soft_hyphen = "by a factor of three over the baseline."
    # The verifier sees newlines as spaces, so it may copy the hyphen as-is.
    as_seen = "model improves training eﬃ- ciency by a factor"

    located = evidence_location(_RAW_PDF_PAPER, repaired)
    assert located[:2] == (7, "Page 7")
    # Stored span is the raw source text, ligatures and hyphen included.
    assert located[2] == (
        "The ﬁnal model improves training eﬃ- ciency by a factor of three"
    )
    assert evidence_location(_RAW_PDF_PAPER, soft_hyphen)[:2] == (7, "Page 7")
    assert evidence_location(_RAW_PDF_PAPER, as_seen)[:2] == (7, "Page 7")
    assert verbatim_evidence(_RAW_PDF_PAPER, repaired) == repaired
    # A changed word is still rejected.
    changed = "The final model improves inference efficiency by a factor of three"
    assert evidence_location(_RAW_PDF_PAPER, changed) == _UNGROUNDED
    assert verbatim_evidence(_RAW_PDF_PAPER, changed) is None


def test_quote_copied_from_real_excerpt_with_page_anchors_is_located() -> None:
    """GOO-321 D-02 (GOO-292): the verifier's excerpt keeps ``[Page N]`` anchors
    between passages, so a faithful cross-passage quote may contain one.

    Mutation check (2026-09-30): removing the ``_INLINE_PAGE_MARKER_RE`` strip
    at the top of ``evidence_location`` makes this test fail with
    ``(None, "source excerpt", None)``. Restoring it makes it pass.
    """
    excerpt = _sanitize_excerpt(
        select_relevant_passages(
            _TWO_PAGE_PAPER,
            queries=["WMT 2014 English-German byte-pair vocabulary tokens"],
            max_chars=8000,
        ),
        8000,
    )

    def copied(first: str, last: str) -> str:
        start = excerpt.index(first)
        return excerpt[start : excerpt.index(last, start) + len(last)]

    across_paragraph = copied("the WMT 2014", "encoded using")
    across_page = copied("encoded using", "vocabulary has")
    assert "[Page 4]" in across_paragraph and "[Page 5]" in across_page

    assert evidence_location(_TWO_PAGE_PAPER, across_paragraph) == (
        4,
        "Page 4",
        "the WMT 2014 English-German dataset. Sentences were encoded using",
    )
    assert evidence_location(_TWO_PAGE_PAPER, across_page) == (
        4,
        "Page 4",
        "encoded using byte-pair encoding. The shared vocabulary has",
    )
    # Anchor tokens never count toward the four-word minimum.
    assert evidence_location(_TWO_PAGE_PAPER, "[Page 4] dataset. [Page 5]") == (
        _UNGROUNDED
    )


def test_superscripts_and_subscripts_are_not_folded_into_digits() -> None:
    """GOO-321 D-02 (GOO-292): NFKC would turn ``10⁴`` into ``104``, changing
    the claim; only non-super/subscript compatibility forms are folded.

    Mutation check (2026-09-30): making ``_fold_char`` apply NFKC to every
    non-ASCII char (dropping the ``<super>``/``<sub>`` exception) makes this
    test fail (``104 to 105`` located on Page 2). Restoring it makes it pass.
    """
    source = (
        "[Page 2]\nScores rose from 10⁴ to 10⁵ steps with the ﬁnal "
        "schedule, holding CO₂ at 95%¹ throughout."
    )

    assert evidence_location(source, "Scores rose from 104 to 105 steps") == (
        _UNGROUNDED
    )
    assert evidence_location(source, "holding CO2 at 95%1 throughout.") == (_UNGROUNDED)
    assert evidence_location(source, "Scores rose from 10⁴ to 10⁵ steps")[:2] == (
        2,
        "Page 2",
    )
    assert evidence_location(source, "steps with the final schedule")[:2] == (
        2,
        "Page 2",
    )


def test_soft_hyphen_at_line_break_joins_the_word() -> None:
    """A soft hyphen absorbs the line break after it (``base\u00ad\nline``).

    Mutation check (2026-09-30): dropping the trailing whitespace from the
    soft-hyphen alternative of ``_JOIN_RE`` makes this test fail with
    ``(None, "source excerpt") == (3, "Page 3")``. Restoring it makes it pass.
    """
    source = "[Page 3]\nWe compare against the strong base\u00ad\nline model."

    assert evidence_location(source, "against the strong baseline model.")[:2] == (
        3,
        "Page 3",
    )


def test_digit_hyphen_line_break_is_not_joined() -> None:
    """``2014-\n2015`` is a range, never the number ``20142015``.

    Mutation check (2026-09-30): widening the de-hyphenation lookarounds in
    ``_JOIN_RE`` back to any word character makes this test fail
    (``20142015`` located on Page 6). Restoring letters-only makes it pass.
    """
    source = "[Page 6]\nData were collected over 2014-\n2015 in two cohorts."

    assert evidence_location(source, "collected over 20142015 in two") == (_UNGROUNDED)
    assert evidence_location(source, "collected over 2014- 2015 in two")[:2] == (
        6,
        "Page 6",
    )
