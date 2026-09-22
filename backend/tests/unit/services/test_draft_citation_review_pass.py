"""Citation-faithfulness reviewer pass in the draft pipeline (Phase 2, flag default-off).

Builds on PR-B's session mocking (test_draft_bg_session.py). Flag off = merge
inert. Flag on = REVIEWING tick, verifier result persisted into
`generation_params["citation_review"]`, verifier failure never fails the
draft. Test 5 is the adversarial-review acceptance case: identifier
hijacking must still yield MAJOR even when the faithfulness LLM is fooled.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

pytestmark = pytest.mark.unit

from src.models.document import Document
from src.services.research.citation_extraction_service import CitationExtractionService
from src.services.research.citation_verification_service import _LLMVerdict
from src.services.research.draft_generation_service import (
    DraftGenerationService,
    DraftGenerationStatus,
    _generation_status,
)
from src.shared.research_schemas import CitationCreate

_MODULE = "src.services.research.draft_generation_service"
_VERIFIER_MODULE = "src.services.research.citation_verification_service"


@pytest.fixture(autouse=True)
def _no_sleep():
    with patch(f"{_MODULE}.asyncio.sleep", new=AsyncMock()):
        yield


def _make_document(
    *,
    title: str = "Doc A",
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


def _make_bg_session(documents, add_sink: list):
    """Same shape as test_draft_bg_session.py's helper, but records every
    `db.add()`'d object so tests can inspect the persisted GeneratedDraft."""
    session = MagicMock()

    docs_result = MagicMock()
    docs_result.scalars.return_value.all.return_value = documents
    lock_result = MagicMock()
    lock_result.scalar_one_or_none.return_value = uuid4()
    version_result = MagicMock()
    version_result.scalar.return_value = 0
    update_result = MagicMock()

    session.execute = AsyncMock(
        side_effect=[docs_result, lock_result, version_result, update_result]
    )
    session.add = MagicMock(side_effect=add_sink.append)
    session.flush = AsyncMock()
    session.commit = AsyncMock()
    return session


def _patch_session_factory(bg_session):
    patcher = patch(f"{_MODULE}.AsyncSessionLocal")
    session_factory = patcher.start()
    session_factory.return_value.__aenter__ = AsyncMock(return_value=bg_session)
    session_factory.return_value.__aexit__ = AsyncMock(return_value=False)
    return patcher


async def _run(service, task_id, draft_content, project_id=None, user_id=None):
    project_id = project_id or uuid4()
    user_id = user_id or uuid4()
    _generation_status[task_id] = {
        "status": DraftGenerationStatus.PENDING,
        "progress": 0,
        "current_step": "Initializing",
        "project_id": str(project_id),
        "user_id": str(user_id),
    }
    service._build_draft_content = AsyncMock(return_value=(draft_content, False))
    await service._generate_draft_async(
        task_id=task_id,
        project_id=project_id,
        user_id=user_id,
        themes=["theme a"],
        document_ids=None,
        style="academic",
        max_sections=5,
        include_abstract=True,
    )


def _draft_from_sink(add_sink):
    from src.models.generated_draft import GeneratedDraft

    drafts = [obj for obj in add_sink if isinstance(obj, GeneratedDraft)]
    assert len(drafts) == 1
    return drafts[0]


@pytest.mark.asyncio
async def test_flag_off_verifier_never_constructed_no_citation_review_key():
    documents = [_make_document()]
    add_sink: list = []
    bg_session = _make_bg_session(documents, add_sink)
    service = DraftGenerationService(MagicMock())

    statuses_seen = []
    orig_update_status = service._update_status

    def _capture(task_id, status, *args, **kwargs):
        statuses_seen.append(status)
        return orig_update_status(task_id, status, *args, **kwargs)

    service._update_status = _capture

    with (
        patch("src.core.config.settings.DRAFT_CITATION_REVIEW_ENABLED", False),
        patch(f"{_VERIFIER_MODULE}.CitationVerificationService") as verifier_cls,
    ):
        patcher = _patch_session_factory(bg_session)
        try:
            await _run(service, "task-flag-off", "Findings from [Doc 1].")
        finally:
            patcher.stop()

    verifier_cls.assert_not_called()
    assert DraftGenerationStatus.REVIEWING not in statuses_seen
    draft = _draft_from_sink(add_sink)
    assert "citation_review" not in draft.generation_params


@pytest.mark.asyncio
async def test_flag_on_reviewing_tick_and_verdict_persisted():
    documents = [_make_document()]
    add_sink: list = []
    bg_session = _make_bg_session(documents, add_sink)
    service = DraftGenerationService(MagicMock())

    review_blob = {
        "verdicts": [{"doc_index": 1, "verdict": "exact"}],
        "summary": {"exact": 1, "minor": 0, "major": 0, "unverified": 0},
        "docs_checked": 1,
        "docs_skipped": 0,
        "duration_ms": 12,
    }
    ticks: list[tuple[str, int]] = []
    orig_update_status = service._update_status

    def _capture(task_id, status, progress, *args, **kwargs):
        ticks.append((status, progress))
        return orig_update_status(task_id, status, progress, *args, **kwargs)

    service._update_status = _capture

    with (
        patch("src.core.config.settings.DRAFT_CITATION_REVIEW_ENABLED", True),
        patch(f"{_VERIFIER_MODULE}.CitationVerificationService") as verifier_cls,
    ):
        verifier_cls.return_value.verify_draft_citations = AsyncMock(
            return_value=review_blob
        )
        patcher = _patch_session_factory(bg_session)
        try:
            await _run(service, "task-flag-on", "Findings from [Doc 1].")
        finally:
            patcher.stop()

    assert (DraftGenerationStatus.REVIEWING, 85) in ticks
    draft = _draft_from_sink(add_sink)
    assert draft.generation_params["citation_review"] == review_blob
    status = DraftGenerationService.get_status("task-flag-on")
    assert status["status"] == DraftGenerationStatus.COMPLETED


@pytest.mark.asyncio
async def test_flag_on_verifier_raises_draft_still_persists_completed():
    documents = [_make_document()]
    add_sink: list = []
    bg_session = _make_bg_session(documents, add_sink)
    service = DraftGenerationService(MagicMock())

    with (
        patch("src.core.config.settings.DRAFT_CITATION_REVIEW_ENABLED", True),
        patch(f"{_VERIFIER_MODULE}.CitationVerificationService") as verifier_cls,
        patch(f"{_MODULE}.logger") as logger_mock,
    ):
        verifier_cls.return_value.verify_draft_citations = AsyncMock(
            side_effect=RuntimeError("crossref outage")
        )
        patcher = _patch_session_factory(bg_session)
        try:
            await _run(service, "task-flag-on-error", "Findings from [Doc 1].")
        finally:
            patcher.stop()

    draft = _draft_from_sink(add_sink)
    assert draft.generation_params["citation_review"] == {"error": "crossref outage"}
    bg_session.commit.assert_awaited_once()
    status = DraftGenerationService.get_status("task-flag-on-error")
    assert status["status"] == DraftGenerationStatus.COMPLETED
    logger_mock.warning.assert_any_call(
        "citation_review_failed",
        task_id="task-flag-on-error",
        error="crossref outage",
    )


@pytest.mark.asyncio
async def test_create_without_citations_fails_before_persistence():
    documents = [_make_document()]
    add_sink: list = []
    bg_session = _make_bg_session(documents, add_sink)
    service = DraftGenerationService(MagicMock())

    with (
        patch("src.core.config.settings.DRAFT_CITATION_REVIEW_ENABLED", True),
        patch(f"{_VERIFIER_MODULE}.CitationVerificationService") as verifier_cls,
    ):
        patcher = _patch_session_factory(bg_session)
        try:
            # Template draft with no [Doc N] markers -> citations_data == [].
            await _run(service, "task-no-citations", "No citations here at all.")
        finally:
            patcher.stop()

    verifier_cls.assert_not_called()
    assert not add_sink
    assert bg_session.commit.await_count == 0
    status = DraftGenerationService.get_status("task-no-citations")
    assert status["status"] == DraftGenerationStatus.FAILED


@pytest.mark.asyncio
async def test_end_to_end_identifier_hijacking_forces_major_despite_llm_exact():
    """Adversarial-review acceptance case: DOI resolves but authors/title
    mismatch must FAIL even when the faithfulness LLM is fooled into saying
    'exact'. Uses the real CitationVerificationService (only CrossRef + the
    LLM are mocked) wired through the actual draft-generation hook."""
    document = _make_document(
        title="Attention Is All You Need",
        document_metadata={"doi": "10.1000/xyz123"},
        content_summary="Introduces the transformer architecture.",
    )
    mismatched = CitationCreate(
        document_title="A Completely Unrelated Paper",
        authors=["Someone Else"],
        doi="10.1000/xyz123",
        metadata_source="crossref",
    )
    add_sink: list = []
    bg_session = _make_bg_session([document], add_sink)
    service = DraftGenerationService(MagicMock())

    structured = AsyncMock()
    structured.ainvoke = AsyncMock(
        return_value=_LLMVerdict(verdict="exact", evidence="Looks fine.")
    )
    llm = MagicMock()
    llm.with_structured_output.return_value = structured

    with (
        patch("src.core.config.settings.DRAFT_CITATION_REVIEW_ENABLED", True),
        patch.object(
            CitationExtractionService,
            "extract_from_crossref",
            AsyncMock(return_value=mismatched),
        ),
        patch(f"{_VERIFIER_MODULE}.build_lightweight_llm", return_value=llm),
    ):
        patcher = _patch_session_factory(bg_session)
        try:
            await _run(
                service,
                "task-hijack",
                "The transformer architecture uses self-attention [Doc 1].",
            )
        finally:
            patcher.stop()

    # The LLM was never even consulted for the final verdict — identity
    # mismatch short-circuits straight to MAJOR.
    structured.ainvoke.assert_not_awaited()

    draft = _draft_from_sink(add_sink)
    review = draft.generation_params["citation_review"]
    entry = review["verdicts"][0]
    assert entry["identity"] == "mismatch"
    assert entry["verdict"] == "major"
    assert "A Completely Unrelated Paper" in entry["evidence"]
    assert review["summary"]["major"] == 1
