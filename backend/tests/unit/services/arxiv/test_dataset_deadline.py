"""Deadline propagation tests for ArXiv evaluation dataset generation."""

import asyncio
import importlib.util
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.services.arxiv import arxiv_service as arxiv_module
from src.services.arxiv.arxiv_service import ArXivIngestionService

PAPER = {
    "id": "2401.00001",
    "title": "Bounded research",
    "abstract": "A paper about bounded provider requests.",
    "categories": ["cs.AI"],
    "authors": ["Researcher"],
}


@pytest.mark.asyncio
async def test_question_generation_forwards_remaining_deadline(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    provider = SimpleNamespace(
        is_chat_available=MagicMock(return_value=True),
        chat_completion=AsyncMock(
            return_value={
                "content": (
                    '{"questions": [{"question": "Why?", '
                    '"difficulty": "easy", '
                    '"expected_answer_type": "contribution"}]}'
                )
            }
        ),
    )
    monkeypatch.setattr(arxiv_module, "azure_openai_service", provider)
    monkeypatch.setattr(arxiv_module.time, "monotonic", lambda: 100.0)
    service = ArXivIngestionService({"arxiv_download_dir": str(tmp_path)})

    questions = await service._generate_questions_for_paper(
        PAPER,
        1,
        deadline=107.5,
    )

    assert questions[0]["source"] == "llm_generated"
    assert provider.chat_completion.await_args.kwargs["timeout"] == 7.5


@pytest.mark.asyncio
async def test_question_generation_rejects_expired_deadline(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    provider = SimpleNamespace(
        is_chat_available=MagicMock(return_value=True),
        chat_completion=AsyncMock(),
    )
    monkeypatch.setattr(arxiv_module, "azure_openai_service", provider)
    monkeypatch.setattr(arxiv_module.time, "monotonic", lambda: 108.0)
    service = ArXivIngestionService({"arxiv_download_dir": str(tmp_path)})

    with pytest.raises(asyncio.TimeoutError):
        await service._generate_questions_for_paper(
            PAPER,
            1,
            deadline=107.5,
        )

    provider.chat_completion.assert_not_awaited()


@pytest.mark.asyncio
async def test_background_dataset_forwards_absolute_deadline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The API wrapper must carry its absolute deadline to the service."""
    core_path = Path(__file__).parents[4] / "src" / "api" / "arxiv" / "core.py"
    spec = importlib.util.spec_from_file_location(
        "_arxiv_core_deadline_test", core_path
    )
    assert spec is not None and spec.loader is not None
    core = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(core)

    create_dataset = AsyncMock(return_value={"test_cases": []})

    class FakeArXivService:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def search_papers(self, **_kwargs):
            return [PAPER]

        create_evaluation_dataset = create_dataset

    monkeypatch.setattr(core, "ArXivIngestionService", FakeArXivService)
    monkeypatch.setattr(core.time, "monotonic", lambda: 300.0)

    await core._create_arxiv_dataset(
        task_id="task-1",
        query="bounded research",
        num_papers=1,
        questions_per_paper=1,
        difficulty_levels=["easy"],
        user_id="user-1",
        organization_id="org-1",
        deadline=321.0,
    )

    assert create_dataset.await_args.kwargs["deadline"] == 321.0
