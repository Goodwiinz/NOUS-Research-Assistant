"""FTS snippets keep numbers and abbreviations whole (audit RT-1).

retrieve_passages hands these snippets to a model as quoted evidence, so a
sentence split on every "." turned "95.3% ... 91.2%" into "3% ... 91". The
strings are the verifier's (findings/verify-V5-readtools-library.md).
"""

from __future__ import annotations

import pytest

from src.models.search_schemas import SearchQuery, SearchSortOrder, SearchType
from src.services.search.fulltext_search_service import FullTextSearchService

pytestmark = pytest.mark.unit


def _passage(highlighted_content: str, highlighted_title: str = "") -> str:
    """The text read_tools._retrieve_passages builds from the snippets."""
    request = SearchQuery(
        query="q",
        search_type=SearchType.FULLTEXT,
        sort_order=SearchSortOrder.RELEVANCE,
        filters=None,
    )
    snippets = FullTextSearchService()._extract_snippets(
        highlighted_content, highlighted_title, request
    )
    text = " ".join(snippet.text for snippet in snippets)
    for tag in ("<mark>", "</mark>"):
        text = text.replace(tag, "")
    return text


@pytest.mark.parametrize(
    ("headline", "expected"),
    [
        (
            "Our model reaches 95.3% top-1 <mark>accuracy</mark> on "
            "<mark>ImageNet</mark>, up from 91.2% (Fig. 3).",
            "Our model reaches 95.3% top-1 accuracy on ImageNet, up from "
            "91.2% (Fig. 3).",
        ),
        (
            "On <mark>ImageNet</mark> the error drops to 4.7% versus 8.8% for "
            "the baseline.",
            "On ImageNet the error drops to 4.7% versus 8.8% for the baseline.",
        ),
        (
            "As shown by Smith et al. the <mark>dropout</mark> rate of 0.5 is "
            "optimal (p < 0.001).",
            "As shown by Smith et al. the dropout rate of 0.5 is optimal "
            "(p < 0.001).",
        ),
        (
            "As shown by Smith et al. The <mark>dropout</mark> rate is optimal.",
            "As shown by Smith et al. The dropout rate is optimal.",
        ),
    ],
    ids=["decimals-and-fig", "decimals", "et-al-lowercase", "et-al-capital"],
)
def test_snippets_keep_numbers_and_abbreviations_whole(
    headline: str, expected: str
) -> None:
    assert _passage(headline) == expected


@pytest.mark.parametrize(
    ("headline", "expected"),
    [
        (
            "Large models (e.g. BERT) <mark>help</mark>.",
            "Large models (e.g. BERT) help.",
        ),
        (
            "Ablations (Fig. S1) show <mark>gains</mark>.",
            "Ablations (Fig. S1) show gains.",
        ),
        (
            "Prior work (cf. Smith et al. 2020) reports <mark>gains</mark>.",
            "Prior work (cf. Smith et al. 2020) reports gains.",
        ),
        (
            "See <mark>Fig</mark>. A1 for details.",
            "See Fig. A1 for details.",
        ),
        (
            "Results (<mark>Fig</mark>. S1) improve.",
            "Results (Fig. S1) improve.",
        ),
    ],
    ids=["paren-e-g", "paren-fig", "paren-cf", "highlighted-fig", "paren-highlighted"],
)
def test_abbreviations_in_parentheses_or_highlights_do_not_end_a_sentence(
    headline: str, expected: str
) -> None:
    assert _passage(headline) == expected


def test_e_g_and_a_version_number_survive_after_the_title() -> None:
    passage = _passage(
        "We use e.g. <mark>BERT</mark> and v2.1 of the tokenizer.",
        "<mark>BERT</mark> study",
    )
    assert passage == "BERT study We use e.g. BERT and v2.1 of the tokenizer."


def test_only_the_sentences_with_a_highlight_are_kept() -> None:
    passage = _passage(
        "Intro sentence without a hit. The <mark>dropout</mark> rate is 0.5 "
        "here. Unrelated closing sentence."
    )
    assert passage == "The dropout rate is 0.5 here."


@pytest.mark.parametrize(
    ("opening", "closing"),
    [('"', '"'), ("“", "”"), ("(", ")"), ("[", "]"), ("'", "'")],
    ids=["straight-quote", "curly-quote", "paren", "bracket", "single-quote"],
)
def test_a_sentence_opening_with_a_quote_or_bracket_is_split_off(
    opening: str, closing: str
) -> None:
    # The old split on every "." separated these; the capital-letter
    # lookahead must look past the opening mark (PR #1946 review).
    passage = _passage(
        f"Unrelated text. {opening}The <mark>result</mark> improves.{closing}"
    )
    assert passage == f"{opening}The result improves.{closing}"


def test_a_highlighted_word_after_an_opening_quote_starts_a_sentence() -> None:
    passage = _passage('Unrelated text. "<mark>Results</mark> improve."')
    assert passage == '"Results improve."'


def test_a_sentence_closed_by_a_quote_is_split_from_the_next_one() -> None:
    passage = _passage(
        'Unrelated text. "The <mark>result</mark> improves." Unrelated closing '
        "sentence."
    )
    assert passage == '"The result improves."'


@pytest.mark.parametrize(
    ("headline", "expected"),
    [
        (
            "Large models help, e.g. (Fig. 2) the <mark>ResNet</mark> family "
            "at 95.3% accuracy.",
            "Large models help, e.g. (Fig. 2) the ResNet family at 95.3% " "accuracy.",
        ),
        (
            'Large models (e.g. "BERT") <mark>help</mark> by 4.7%.',
            'Large models (e.g. "BERT") help by 4.7%.',
        ),
        (
            "Large models (“e.g. BERT”) <mark>help</mark>.",
            "Large models (“e.g. BERT”) help.",
        ),
        (
            "Prior work (Smith et al.) <mark>BERT</mark> variants help.",
            "Prior work (Smith et al.) BERT variants help.",
        ),
    ],
    ids=["e-g-paren-fig", "e-g-quoted", "curly-quoted-e-g", "et-al-closing-paren"],
)
def test_quotes_and_brackets_keep_abbreviations_and_decimals_whole(
    headline: str, expected: str
) -> None:
    assert _passage(headline) == expected
