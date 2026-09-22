"""`_generate_draft_async` must own its own session, not the caller's.

`generate_draft` fire-and-forgets `_generate_draft_async` in the background and
returns immediately; the caller (`_tool_create_draft` / the REST 202 path) then
exits its own session scope before the background task's first DB call runs.
Using `self.db` inside the background task therefore raced a session the
caller had already closed. `_generate_draft_async` now opens its own
`AsyncSessionLocal()` and never touches `self.db` (the ctor-injected,
request-scoped session used only by the request-scoped methods).
"""

from __future__ import annotations

from typing import List
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

pytestmark = pytest.mark.unit

from src.models.draft_citation import DraftCitation
from src.models.generated_draft import GeneratedDraft
from src.services.research.draft_generation_service import (
    DraftGenerationService,
    DraftGenerationStatus,
    _generation_status,
)


@pytest.fixture(autouse=True)
def _no_sleep():
    with patch(
        "src.services.research.draft_generation_service.asyncio.sleep",
        new=AsyncMock(),
    ):
        yield


def _make_bg_session(documents):
    """A MagicMock session with just the async methods the background task
    calls actually mocked as AsyncMock (add() is sync on AsyncSession)."""
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
    session.flush = AsyncMock()
    session.commit = AsyncMock()
    return session


def _patch_session_factory(bg_session):
    patcher = patch("src.services.research.draft_generation_service.AsyncSessionLocal")
    session_factory = patcher.start()
    session_factory.return_value.__aenter__ = AsyncMock(return_value=bg_session)
    session_factory.return_value.__aexit__ = AsyncMock(return_value=False)
    return patcher


async def _run(service, task_id, project_id=None, user_id=None):
    project_id = project_id or uuid4()
    user_id = user_id or uuid4()
    _generation_status[task_id] = {
        "status": DraftGenerationStatus.PENDING,
        "progress": 0,
        "current_step": "Initializing",
        "project_id": str(project_id),
        "user_id": str(user_id),
    }
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


@pytest.mark.asyncio
async def test_background_task_never_touches_ctor_session():
    ctor_session = MagicMock()  # the caller's (already-closed) session
    documents = [MagicMock(id=uuid4(), title="Doc A")]
    bg_session = _make_bg_session(documents)

    service = DraftGenerationService(ctor_session)
    service._build_draft_content = AsyncMock(
        return_value=("Some draft body [Doc 1].", False)
    )

    patcher = _patch_session_factory(bg_session)
    try:
        await _run(service, "task-own-session")
    finally:
        patcher.stop()

    ctor_session.execute.assert_not_called()
    bg_session.execute.assert_awaited()
    bg_session.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_happy_path_completes_through_patched_session():
    documents = [MagicMock(id=uuid4(), title="Doc A")]
    bg_session = _make_bg_session(documents)

    service = DraftGenerationService(MagicMock())
    service._build_draft_content = AsyncMock(
        return_value=("Findings from [Doc 1].", False)
    )

    patcher = _patch_session_factory(bg_session)
    try:
        await _run(service, "task-happy")
    finally:
        patcher.stop()

    status = DraftGenerationService.get_status("task-happy")
    assert status["status"] == DraftGenerationStatus.COMPLETED
    bg_session.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_create_persists_visible_doc_two_index():
    documents = [
        MagicMock(id=uuid4(), title="Doc A"),
        MagicMock(id=uuid4(), title="Doc B"),
    ]
    bg_session = _make_bg_session(documents)
    service = DraftGenerationService(MagicMock())
    service._build_draft_content = AsyncMock(
        return_value=("Findings from [Doc 2].", False)
    )

    patcher = _patch_session_factory(bg_session)
    try:
        await _run(service, "task-doc-two")
    finally:
        patcher.stop()

    citation = next(
        call.args[0]
        for call in bg_session.add.call_args_list
        if isinstance(call.args[0], DraftCitation)
    )
    assert citation.citation_index == 2
    assert citation.document_id == documents[1].id


@pytest.mark.asyncio
async def test_two_session_windows_first_closed_before_build_draft_content():
    """F3: the docs-fetch session (window 1) must be closed before the ~60s
    _build_draft_content LLM call runs, and window 2 (reviewer/version/
    persist/commit) must open only after that call returns — a pooled
    connection must never sit idle across the LLM call."""
    documents = [MagicMock(id=uuid4(), title="Doc A")]
    call_order: List[str] = []

    docs_result = MagicMock()
    docs_result.scalars.return_value.all.return_value = documents
    window1_session = MagicMock()
    window1_session.execute = AsyncMock(return_value=docs_result)

    version_result = MagicMock()
    version_result.scalar.return_value = 0
    lock_result = MagicMock()
    lock_result.scalar_one_or_none.return_value = uuid4()
    update_result = MagicMock()
    window2_session = MagicMock()
    window2_session.execute = AsyncMock(
        side_effect=[lock_result, version_result, update_result]
    )
    window2_session.flush = AsyncMock()
    window2_session.commit = AsyncMock()

    class _RecordingSessionCtx:
        def __init__(self, session: MagicMock, label: str) -> None:
            self._session = session
            self._label = label

        async def __aenter__(self) -> MagicMock:
            call_order.append(f"enter:{self._label}")
            return self._session

        async def __aexit__(self, *exc_info: object) -> bool:
            call_order.append(f"exit:{self._label}")
            return False

    session_factory = MagicMock(
        side_effect=[
            _RecordingSessionCtx(window1_session, "w1"),
            _RecordingSessionCtx(window2_session, "w2"),
        ]
    )

    service = DraftGenerationService(MagicMock())

    async def _fake_build_draft_content(*args: object, **kwargs: object):
        call_order.append("build_draft_content")
        return "Findings from [Doc 1].", False

    service._build_draft_content = AsyncMock(side_effect=_fake_build_draft_content)

    with patch(
        "src.services.research.draft_generation_service.AsyncSessionLocal",
        session_factory,
    ):
        await _run(service, "task-two-windows")

    assert call_order == [
        "enter:w1",
        "exit:w1",
        "build_draft_content",
        "enter:w2",
        "exit:w2",
    ]
    window2_session.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_create_locks_project_row_before_version_allocation():
    documents = [MagicMock(id=uuid4(), title="Doc A")]
    bg_session = _make_bg_session(documents)
    service = DraftGenerationService(MagicMock())
    service._build_draft_content = AsyncMock(return_value=("Body [Doc 1].", False))

    patcher = _patch_session_factory(bg_session)
    try:
        await _run(service, "task-project-lock")
    finally:
        patcher.stop()

    statements = [str(call.args[0]) for call in bg_session.execute.await_args_list]
    lock_index = next(
        index
        for index, sql in enumerate(statements)
        if "FROM collections" in sql and "FOR UPDATE" in sql
    )
    version_index = next(
        index
        for index, sql in enumerate(statements)
        if "max(generated_drafts.version)" in sql
    )
    assert lock_index < version_index


@pytest.mark.asyncio
async def test_empty_documents_fails_without_committing():
    bg_session = _make_bg_session([])

    service = DraftGenerationService(MagicMock())

    patcher = _patch_session_factory(bg_session)
    try:
        await _run(service, "task-empty")
    finally:
        patcher.stop()

    status = DraftGenerationService.get_status("task-empty")
    assert status["status"] == DraftGenerationStatus.FAILED
    bg_session.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_long_themes_do_not_overflow_title_column():
    """GeneratedDraft.title is String(255). Themes are LLM-authored and can be
    full sentences, so the `Literature Review - <themes>` join must be clamped
    or the insert dies at the FINALIZING phase and the draft never appears.
    """
    documents = [MagicMock(id=uuid4(), title="Doc A")]
    bg_session = _make_bg_session(documents)

    service = DraftGenerationService(MagicMock())
    service._build_draft_content = AsyncMock(return_value=("Body [Doc 1].", False))

    long_themes = [
        "Synthesize the six imported papers explicitly: Attention Is All You "
        "Need (arXiv:1706.03762; document_id 74c232ea-e007-4a73-b8ed-df0f88bae654)"
        + " and related work" * 10,
        "Second theme " * 30,
        "Third theme " * 30,
    ]

    patcher = _patch_session_factory(bg_session)
    try:
        _generation_status["task-long-title"] = {
            "status": DraftGenerationStatus.PENDING,
            "progress": 0,
            "current_step": "Initializing",
            "project_id": str(uuid4()),
            "user_id": str(uuid4()),
        }
        await service._generate_draft_async(
            task_id="task-long-title",
            project_id=uuid4(),
            user_id=uuid4(),
            themes=long_themes,
            document_ids=None,
            style="academic",
            max_sections=5,
            include_abstract=True,
        )
    finally:
        patcher.stop()

    drafts = [
        call.args[0]
        for call in bg_session.add.call_args_list
        if isinstance(call.args[0], GeneratedDraft)
    ]
    assert drafts, "no draft was persisted"
    assert len(drafts[0].title) <= 255
    assert drafts[0].title.startswith("Literature Review - ")
    # Themes themselves are untouched — only the display title is clamped.
    assert drafts[0].themes == long_themes
