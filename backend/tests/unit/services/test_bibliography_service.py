"""Unit tests for BibliographyService.

Tests all 4 citation formatters (BibTeX, IEEE, APA, MLA) with various
input shapes, edge cases, and error conditions.
"""

from unittest.mock import MagicMock

import pytest

from src.services.research.bibliography_service import BibliographyService

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_citation(**overrides) -> MagicMock:
    """Build a mock Citation with sensible defaults."""
    defaults = {
        "document_title": "Attention Is All You Need",
        "authors": ["Ashish Vaswani", "Noam Shazeer", "Niki Parmar"],
        "year": 2017,
        "venue": "NeurIPS",
        "doi": "10.5555/3295222.3295349",
        "arxiv_id": "1706.03762",
        "abstract": "The dominant sequence transduction models are based on complex recurrent or convolutional neural networks.",
    }
    defaults.update(overrides)
    citation = MagicMock()
    for key, value in defaults.items():
        setattr(citation, key, value)
    return citation


def _make_minimal_citation(**overrides) -> MagicMock:
    """Build a citation with only a title — all other fields None."""
    defaults = {
        "document_title": "Minimal Paper",
        "authors": None,
        "year": None,
        "venue": None,
        "doi": None,
        "arxiv_id": None,
        "abstract": None,
    }
    defaults.update(overrides)
    return _make_citation(**defaults)


# ===========================================================================
# BibTeX formatter
# ===========================================================================


class TestFormatBibtex:
    """Tests for BibliographyService.format_bibtex."""

    def test_single_citation_produces_valid_bibtex(self) -> None:
        citation = _make_citation()
        result = BibliographyService.format_bibtex([citation])

        assert "@misc{" in result  # ArXiv papers → misc
        assert "Attention Is All You Need" in result
        assert "Ashish Vaswani and Noam Shazeer and Niki Parmar" in result
        assert "2017" in result
        assert "1706.03762" in result

    def test_article_type_when_no_arxiv_id(self) -> None:
        citation = _make_citation(arxiv_id=None)
        result = BibliographyService.format_bibtex([citation])

        assert "@article{" in result
        assert "journal" in result.lower()

    def test_multiple_citations_produce_multiple_entries(self) -> None:
        citations = [
            _make_citation(document_title="Paper A", arxiv_id=None),
            _make_citation(document_title="Paper B", arxiv_id=None),
            _make_citation(document_title="Paper C"),
        ]
        result = BibliographyService.format_bibtex(citations)

        assert result.count("@") == 3

    def test_empty_list_returns_empty_string(self) -> None:
        assert BibliographyService.format_bibtex([]) == ""

    def test_citation_with_doi_includes_doi_field(self) -> None:
        citation = _make_citation()
        result = BibliographyService.format_bibtex([citation])

        assert "10.5555/3295222.3295349" in result

    def test_arxiv_citation_includes_eprint_fields(self) -> None:
        citation = _make_citation()
        result = BibliographyService.format_bibtex([citation])

        assert "eprint" in result.lower()
        assert "arXiv" in result

    def test_bibtex_key_format(self) -> None:
        citation = _make_citation()
        result = BibliographyService.format_bibtex([citation])

        # Key should contain author last name + year + first title word
        assert "vaswani2017attention" in result.lower()

    def test_minimal_citation_no_crash(self) -> None:
        citation = _make_minimal_citation()
        result = BibliographyService.format_bibtex([citation])

        assert "Minimal Paper" in result

    def test_no_authors_omits_author_field(self) -> None:
        citation = _make_citation(authors=[])
        result = BibliographyService.format_bibtex([citation])

        # Should still produce valid BibTeX without crashing
        assert "@" in result

    def test_abstract_included(self) -> None:
        citation = _make_citation()
        result = BibliographyService.format_bibtex([citation])

        assert "abstract" in result.lower()

    def test_explicit_keys_are_preserved(self) -> None:
        result = BibliographyService.format_bibtex([_make_citation()], keys=["doc7"])

        assert "@misc{doc7," in result


# ===========================================================================
# IEEE formatter
# ===========================================================================


class TestFormatIeee:
    """Tests for BibliographyService.format_ieee."""

    def test_single_citation_format(self) -> None:
        citation = _make_citation()
        result = BibliographyService.format_ieee([citation])

        assert result.startswith("[1]")
        assert '"Attention Is All You Need,"' in result
        assert "2017" in result

    def test_three_plus_authors_uses_et_al(self) -> None:
        citation = _make_citation()
        result = BibliographyService.format_ieee([citation])

        assert "et al." in result

    def test_two_authors_uses_and(self) -> None:
        citation = _make_citation(authors=["Alice Smith", "Bob Jones"])
        result = BibliographyService.format_ieee([citation])

        assert "Alice Smith and Bob Jones" in result
        assert "et al." not in result

    def test_single_author_no_and_or_et_al(self) -> None:
        citation = _make_citation(authors=["Alice Smith"])
        result = BibliographyService.format_ieee([citation])

        assert "Alice Smith," in result
        assert " and " not in result
        assert "et al." not in result

    def test_multiple_citations_numbered_sequentially(self) -> None:
        citations = [_make_citation(), _make_citation(document_title="Paper B")]
        result = BibliographyService.format_ieee(citations)

        assert "[1]" in result
        assert "[2]" in result

    def test_doi_included(self) -> None:
        citation = _make_citation()
        result = BibliographyService.format_ieee([citation])

        assert "doi:" in result.lower() or "doi: " in result.lower()

    def test_arxiv_included_when_no_doi(self) -> None:
        citation = _make_citation(doi=None)
        result = BibliographyService.format_ieee([citation])

        assert "arXiv: 1706.03762" in result

    def test_empty_list_returns_empty_string(self) -> None:
        assert BibliographyService.format_ieee([]) == ""

    def test_minimal_citation(self) -> None:
        citation = _make_minimal_citation()
        result = BibliographyService.format_ieee([citation])

        assert "[1]" in result
        assert "Minimal Paper" in result


# ===========================================================================
# APA formatter
# ===========================================================================


class TestFormatApa:
    """Tests for BibliographyService.format_apa."""

    def test_single_author_format(self) -> None:
        citation = _make_citation(authors=["John Smith"])
        result = BibliographyService.format_apa([citation])

        # APA: Last, F. M.
        assert "Smith, J." in result

    def test_multiple_authors_uses_ampersand(self) -> None:
        citation = _make_citation(authors=["Alice Smith", "Bob Jones"])
        result = BibliographyService.format_apa([citation])

        assert "&" in result

    def test_year_in_parentheses(self) -> None:
        citation = _make_citation()
        result = BibliographyService.format_apa([citation])

        assert "(2017)" in result

    def test_doi_as_url(self) -> None:
        citation = _make_citation()
        result = BibliographyService.format_apa([citation])

        assert "https://doi.org/" in result

    def test_arxiv_fallback_when_no_doi(self) -> None:
        citation = _make_citation(doi=None)
        result = BibliographyService.format_apa([citation])

        assert "arXiv:1706.03762" in result

    def test_venue_in_italics(self) -> None:
        citation = _make_citation()
        result = BibliographyService.format_apa([citation])

        assert "*NeurIPS*" in result

    def test_empty_list_returns_empty_string(self) -> None:
        assert BibliographyService.format_apa([]) == ""

    def test_minimal_citation(self) -> None:
        citation = _make_minimal_citation()
        result = BibliographyService.format_apa([citation])

        assert "Minimal Paper" in result


# ===========================================================================
# MLA formatter
# ===========================================================================


class TestFormatMla:
    """Tests for BibliographyService.format_mla."""

    def test_single_author_format(self) -> None:
        citation = _make_citation(authors=["Alice Smith"])
        result = BibliographyService.format_mla([citation])

        assert "Alice Smith." in result

    def test_multiple_authors_uses_et_al(self) -> None:
        citation = _make_citation()
        result = BibliographyService.format_mla([citation])

        assert "et al." in result

    def test_title_in_quotes(self) -> None:
        citation = _make_citation()
        result = BibliographyService.format_mla([citation])

        assert '"Attention Is All You Need."' in result

    def test_venue_in_italics(self) -> None:
        citation = _make_citation()
        result = BibliographyService.format_mla([citation])

        assert "*NeurIPS*" in result

    def test_doi_included(self) -> None:
        citation = _make_citation()
        result = BibliographyService.format_mla([citation])

        assert "doi:" in result

    def test_arxiv_fallback_when_no_doi(self) -> None:
        citation = _make_citation(doi=None)
        result = BibliographyService.format_mla([citation])

        assert "arXiv:1706.03762" in result

    def test_empty_list_returns_empty_string(self) -> None:
        assert BibliographyService.format_mla([]) == ""


# ===========================================================================
# format_bibliography dispatcher
# ===========================================================================


class TestFormatBibliography:
    """Tests for the format_bibliography dispatcher."""

    def test_dispatches_to_bibtex(self) -> None:
        citation = _make_citation()
        result = BibliographyService.format_bibliography([citation], "bibtex")

        assert "@" in result

    def test_dispatches_to_ieee(self) -> None:
        citation = _make_citation()
        result = BibliographyService.format_bibliography([citation], "ieee")

        assert "[1]" in result

    def test_dispatches_to_apa(self) -> None:
        citation = _make_citation()
        result = BibliographyService.format_bibliography([citation], "apa")

        assert "(2017)" in result

    def test_dispatches_to_mla(self) -> None:
        citation = _make_citation()
        result = BibliographyService.format_bibliography([citation], "mla")

        assert '"Attention Is All You Need."' in result

    def test_case_insensitive_format(self) -> None:
        citation = _make_citation()
        result = BibliographyService.format_bibliography([citation], "BibTeX")

        assert "@" in result

    def test_unsupported_format_raises_value_error(self) -> None:
        citation = _make_citation()

        with pytest.raises(ValueError, match="Unsupported format"):
            BibliographyService.format_bibliography([citation], "chicago")

    def test_empty_string_format_raises_value_error(self) -> None:
        citation = _make_citation()

        with pytest.raises(ValueError):
            BibliographyService.format_bibliography([citation], "")


# ===========================================================================
# W-B2 regressions: nullable title / empty author must not crash BibTeX
# ===========================================================================


class TestBibtexNullableFields:
    """format_bibtex must tolerate the same nullable fields the ieee/apa/mla
    formatters already guard (Citation.document_title and authors are
    nullable; external-reference citations may carry only a DOI/arXiv id)."""

    def test_null_title_does_not_crash(self) -> None:
        citations = [_make_minimal_citation(document_title=None)]
        result = BibliographyService.format_bibtex(citations)
        assert "@" in result  # an entry was produced

    def test_empty_author_entry_does_not_crash(self) -> None:
        citations = [_make_minimal_citation(authors=[""])]
        result = BibliographyService.format_bibtex(citations)
        assert "@" in result

    def test_null_title_and_empty_author_key_is_stable(self) -> None:
        key = BibliographyService._generate_bibtex_key(
            _make_minimal_citation(document_title=None, authors=[""]), 1
        )
        assert key  # falls back to defaults instead of raising
