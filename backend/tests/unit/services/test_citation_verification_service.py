"""Unit tests for CitationVerificationService (CiteCheck-style faithfulness verifier)."""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from src.models.document import Document
from src.services.research.citation_extraction_service import CitationExtractionService
from src.services.research.citation_verification_service import (
    CitationVerificationService,
    _LLMVerdict,
)
from src.shared.research_schemas import CitationCreate

_MODULE = "src.services.research.citation_verification_service"


def _make_document(
    *,
    title: str = "",
    document_metadata: dict | None = None,
    content_summary: str | None = "Some abstract text.",
    content_text: str | None = None,
) -> MagicMock:
    document = MagicMock(spec=Document)
    document.id = uuid4()
    document.title = title
    document.document_metadata = (
        document_metadata if document_metadata is not None else {}
    )
    document.content_summary = content_summary
    document.content_text = content_text
    return document


def _llm_mock(*verdicts: _LLMVerdict) -> MagicMock:
    """Build a build_lightweight_llm() replacement returning the given verdicts in order."""
    structured = AsyncMock()
    if len(verdicts) == 1:
        structured.ainvoke = AsyncMock(return_value=verdicts[0])
    else:
        structured.ainvoke = AsyncMock(side_effect=list(verdicts))
    llm = MagicMock()
    llm.with_structured_output.return_value = structured
    return llm, structured


class TestClaimsByDocIndex:
    def test_groups_dedupes_and_ignores_invalid_indices(self) -> None:
        content = (
            "Transformers improved translation quality [Doc 1]. "
            "Transformers improved translation quality [Doc 1]. "
            "Attention scales well with data [Doc 2].\n"
            "Some other note [Doc 0] should be ignored."
        )
        service = CitationVerificationService(AsyncMock())
        claims = service._claims_by_doc_index(content)

        assert set(claims.keys()) == {1, 2}
        assert len(claims[1]) == 1  # exact-duplicate sentence deduped
        assert "translation quality" in claims[1][0]
        assert len(claims[2]) == 1
        assert "scaling" not in claims  # no stray keys
        assert "scales well with data" in claims[2][0]

    def test_preserves_decimal_numbers_in_claim_sentence(self) -> None:
        content = (
            "The model achieved 28.4 BLEU and 41.8 BLEU, then trained for "
            "3.5 days on 8 GPUs [Doc 1]."
        )

        claims = CitationVerificationService._claims_by_doc_index(content)

        assert claims[1] == [content]


@pytest.mark.asyncio
class TestVerifyDraftCitations:
    async def test_identity_mismatch_forces_major_without_consulting_llm(self) -> None:
        document = _make_document(
            title="Attention Is All You Need",
            document_metadata={"doi": "10.1000/xyz123"},
        )
        mismatched = CitationCreate(
            document_title="A Completely Unrelated Paper",
            authors=["Someone Else"],
            doi="10.1000/xyz123",
            metadata_source="crossref",
        )
        service = CitationVerificationService(AsyncMock())

        with (
            patch.object(
                CitationExtractionService,
                "extract_from_crossref",
                AsyncMock(return_value=mismatched),
            ) as crossref_mock,
            patch(f"{_MODULE}.build_lightweight_llm") as llm_factory_mock,
        ):
            result = await service.verify_draft_citations(
                "The transformer architecture uses self-attention [Doc 1].",
                [document],
            )

        crossref_mock.assert_awaited_once()
        llm_factory_mock.assert_not_called()
        entry = result["verdicts"][0]
        assert entry["identity"] == "mismatch"
        assert entry["verdict"] == "major"
        assert entry["identity_source"] == "crossref"
        assert "A Completely Unrelated Paper" in entry["evidence"]
        assert result["summary"] == {
            "exact": 0,
            "minor": 0,
            "major": 1,
            "unverified": 0,
        }

    async def test_identity_match_and_exact_llm_verdict_no_escalation(self) -> None:
        document = _make_document(
            title="Attention Is All You Need",
            document_metadata={"doi": "10.1000/abc789", "authors": ["Ashish Vaswani"]},
            content_summary="Introduces the transformer architecture.",
            content_text="Full paper text about transformers. " * 50,
        )
        matched = CitationCreate(
            document_title="Attention Is All You Need",
            authors=["Ashish Vaswani"],
            doi="10.1000/abc789",
            abstract="Introduces the transformer architecture.",
            metadata_source="crossref",
        )
        llm, structured = _llm_mock(
            _LLMVerdict(verdict="exact", evidence="Directly supported.")
        )
        service = CitationVerificationService(AsyncMock())

        with (
            patch.object(
                CitationExtractionService,
                "extract_from_crossref",
                AsyncMock(return_value=matched),
            ),
            patch(f"{_MODULE}.build_lightweight_llm", return_value=llm),
        ):
            result = await service.verify_draft_citations(
                "Self-attention replaces recurrence entirely [Doc 1].", [document]
            )

        entry = result["verdicts"][0]
        assert entry["identity"] == "match"
        assert entry["verdict"] == "exact"
        assert entry["escalated_to_fulltext"] is False
        structured.ainvoke.assert_awaited_once()

    async def test_minor_verdict_on_abstract_escalates_to_fulltext(self) -> None:
        document = _make_document(
            content_summary="Short abstract.",
            content_text="Long full text with more nuance. " * 50,
        )
        minor_then_exact = (
            _LLMVerdict(verdict="minor", evidence="Abstract is vague."),
            _LLMVerdict(verdict="exact", evidence="Full text confirms claim."),
        )
        llm, structured = _llm_mock(*minor_then_exact)
        service = CitationVerificationService(AsyncMock())

        with patch(f"{_MODULE}.build_lightweight_llm", return_value=llm):
            result = await service.verify_draft_citations(
                "The method scales linearly [Doc 1].", [document]
            )

        entry = result["verdicts"][0]
        assert entry["identity"] == "no_identifiers"
        assert entry["verdict"] == "exact"
        assert entry["escalated_to_fulltext"] is True
        assert structured.ainvoke.await_count == 2

    async def test_all_providers_none_yields_unresolved_but_llm_verdict_used(
        self,
    ) -> None:
        document = _make_document(
            title="Obscure Paper",
            document_metadata={"doi": "10.1000/none-found"},
            content_text=None,
        )
        llm, structured = _llm_mock(_LLMVerdict(verdict="exact", evidence="Matches."))
        service = CitationVerificationService(AsyncMock())

        with (
            patch.object(
                CitationExtractionService,
                "extract_from_crossref",
                AsyncMock(return_value=None),
            ) as crossref_mock,
            patch(f"{_MODULE}.build_lightweight_llm", return_value=llm),
        ):
            result = await service.verify_draft_citations(
                "The result holds under distribution shift [Doc 1].", [document]
            )

        crossref_mock.assert_awaited_once()
        entry = result["verdicts"][0]
        assert entry["identity"] == "unresolved"
        assert entry["identity_source"] is None
        assert entry["verdict"] == "exact"

    async def test_llm_timeout_yields_unverified_not_major(self) -> None:
        document = _make_document()
        structured = AsyncMock()
        structured.ainvoke = AsyncMock(side_effect=asyncio.TimeoutError())
        llm = MagicMock()
        llm.with_structured_output.return_value = structured
        service = CitationVerificationService(AsyncMock())

        with patch(f"{_MODULE}.build_lightweight_llm", return_value=llm):
            result = await service.verify_draft_citations(
                "This claim cannot be verified in time [Doc 1].", [document]
            )

        entry = result["verdicts"][0]
        assert entry["verdict"] == "unverified"
        assert result["summary"]["unverified"] == 1

    async def test_llm_cancelled_error_propagates(self) -> None:
        document = _make_document()
        structured = AsyncMock()
        structured.ainvoke = AsyncMock(side_effect=asyncio.CancelledError())
        llm = MagicMock()
        llm.with_structured_output.return_value = structured
        service = CitationVerificationService(AsyncMock())

        with patch(f"{_MODULE}.build_lightweight_llm", return_value=llm):
            with pytest.raises(asyncio.CancelledError):
                await service.verify_draft_citations(
                    "This claim triggers a cancellation [Doc 1].", [document]
                )

    async def test_doc_without_identifiers_skips_identity_llm_only(self) -> None:
        document = _make_document()
        llm, structured = _llm_mock(_LLMVerdict(verdict="exact", evidence="Supported."))
        service = CitationVerificationService(AsyncMock())

        with (
            patch.object(
                CitationExtractionService, "extract_from_crossref"
            ) as crossref_mock,
            patch.object(
                CitationExtractionService, "extract_from_semantic_scholar"
            ) as s2_mock,
            patch(f"{_MODULE}.build_lightweight_llm", return_value=llm),
        ):
            result = await service.verify_draft_citations(
                "A claim with no resolvable identity [Doc 1].", [document]
            )

        crossref_mock.assert_not_called()
        s2_mock.assert_not_called()
        entry = result["verdicts"][0]
        assert entry["identity"] == "no_identifiers"
        assert entry["identity_source"] is None
        assert entry["verdict"] == "exact"

    async def test_fulltext_escalation_selects_claim_relevant_late_pages(
        self,
    ) -> None:
        content_text = (
            "[Page 1]\n"
            + ("unrelated background. " * 600)
            + "\n[Page 8]\nThe model achieved 28.4 BLEU on English-to-German."
            + "\n[Page 9]\nIt achieved 41.8 BLEU on English-to-French."
            + "\n[Page 12]\nTraining took 3.5 days on 8 GPUs."
        )
        document = _make_document(
            content_summary="Short abstract that will not fully verify.",
            content_text=content_text,
        )
        minor_then_exact = (
            _LLMVerdict(verdict="minor", evidence="Abstract is vague."),
            _LLMVerdict(verdict="exact", evidence="Full text confirms claim."),
        )
        llm, structured = _llm_mock(*minor_then_exact)
        service = CitationVerificationService(AsyncMock())

        with patch(f"{_MODULE}.build_lightweight_llm", return_value=llm):
            result = await service.verify_draft_citations(
                "The benchmarks were 28.4 BLEU and 41.8 BLEU, and training "
                "took 3.5 days on 8 GPUs [Doc 1].",
                [document],
            )

        assert structured.ainvoke.await_count == 2
        escalated_messages = structured.ainvoke.await_args_list[1].args[0]
        escalated_user_content = escalated_messages[1]["content"]
        assert "28.4 BLEU" in escalated_user_content
        assert "41.8 BLEU" in escalated_user_content
        assert "3.5 days on 8 GPUs" in escalated_user_content
        assert "[Page 8]" in escalated_user_content
        assert result["verdicts"][0]["escalated_to_fulltext"] is True

    async def test_summary_counts_and_max_docs_cap(self) -> None:
        documents = [_make_document() for _ in range(12)]  # cap is 10
        content = " ".join(
            f"Claim about topic {i} [Doc {i}]." for i in range(1, len(documents) + 1)
        )
        llm, structured = _llm_mock(_LLMVerdict(verdict="exact", evidence="Supported."))
        service = CitationVerificationService(AsyncMock())

        with patch(f"{_MODULE}.build_lightweight_llm", return_value=llm):
            result = await service.verify_draft_citations(content, documents)

        assert result["docs_checked"] == 10
        assert result["docs_skipped"] == 2
        assert result["summary"] == {
            "exact": 10,
            "minor": 0,
            "major": 0,
            "unverified": 0,
        }
        assert len(result["verdicts"]) == 10


@pytest.mark.asyncio
class TestIdentityCorroborationThreshold:
    """F2: a shared surname alone must not rescue an outright title
    mismatch — only corroborate a title that's already plausibly close."""

    async def test_common_surname_does_not_rescue_unrelated_title(self) -> None:
        # title_ratio ~0.27 (well below _TITLE_CORROBORATION_THRESHOLD=0.35)
        document = _make_document(
            title="Attention Is All You Need",
            document_metadata={
                "doi": "10.1000/hijacked",
                "authors": ["Ashish Vaswani"],
            },
        )
        hijacked = CitationCreate(
            document_title="Quantum Computing Basics And Applications",
            authors=["John Vaswani"],  # shared surname only
            doi="10.1000/hijacked",
            metadata_source="crossref",
        )
        service = CitationVerificationService(AsyncMock())

        with (
            patch.object(
                CitationExtractionService,
                "extract_from_crossref",
                AsyncMock(return_value=hijacked),
            ),
            patch(f"{_MODULE}.build_lightweight_llm") as llm_factory_mock,
        ):
            result = await service.verify_draft_citations(
                "The transformer architecture uses self-attention [Doc 1].",
                [document],
            )

        llm_factory_mock.assert_not_called()  # mismatch short-circuits faithfulness
        entry = result["verdicts"][0]
        assert entry["identity"] == "mismatch"
        assert entry["verdict"] == "major"

    async def test_borderline_title_with_shared_surname_still_matches(self) -> None:
        # title_ratio == 0.5 (between the corroboration and outright-match
        # thresholds) plus a shared surname must still resolve as a match.
        document = _make_document(
            title="Attention Is All You Need for Translation",
            document_metadata={
                "doi": "10.1000/borderline",
                "authors": ["Ashish Vaswani"],
            },
            content_summary="Introduces the transformer architecture.",
        )
        borderline = CitationCreate(
            document_title="All You Need Is Better Attention Mechanisms",
            authors=["Ashish Vaswani"],
            doi="10.1000/borderline",
            metadata_source="crossref",
        )
        llm, structured = _llm_mock(
            _LLMVerdict(verdict="exact", evidence="Directly supported.")
        )
        service = CitationVerificationService(AsyncMock())

        with (
            patch.object(
                CitationExtractionService,
                "extract_from_crossref",
                AsyncMock(return_value=borderline),
            ),
            patch(f"{_MODULE}.build_lightweight_llm", return_value=llm),
        ):
            result = await service.verify_draft_citations(
                "Self-attention replaces recurrence entirely [Doc 1].", [document]
            )

        entry = result["verdicts"][0]
        assert entry["identity"] == "match"
        assert entry["verdict"] == "exact"
