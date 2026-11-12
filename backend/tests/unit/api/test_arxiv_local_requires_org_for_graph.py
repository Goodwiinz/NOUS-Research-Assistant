"""D2: arxiv_local must not schedule a knowledge-graph update without a tenant.

Graph writes fail closed without an organization, so scheduling the
background task for an org-less user would write paper/topic entities and then
have every topic link refused. The handler skips the update and says so.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from fastapi import BackgroundTasks

import src.api.arxiv.arxiv_local as arxiv_local

pytestmark = pytest.mark.unit


def _fake_pdf(stem: str = "2304.04290") -> MagicMock:
    f = MagicMock()
    f.stem = stem
    f.name = f"{stem}.pdf"
    f.stat.return_value.st_mtime = 123.0
    f.stat.return_value.st_size = 1024
    return f


def _request() -> arxiv_local.LocalExtractionRequest:
    return arxiv_local.LocalExtractionRequest(
        paper_ids=None,
        extract_entities=False,
        extract_topics=False,
        extract_keyphrases=False,
        extract_summaries=False,
        process_full_content=False,
        update_knowledge_graph=True,
    )


async def _run(
    current_user: dict,
) -> tuple[arxiv_local.LocalExtractionResponse, BackgroundTasks]:
    bt = BackgroundTasks()
    with patch.object(arxiv_local, "ARXIV_DATA_PATH") as data_path:
        data_path.glob.return_value = [_fake_pdf()]
        response = await arxiv_local._extract_features_from_local_pdfs(
            request=_request(), background_tasks=bt, current_user=current_user
        )
    return response, bt


@pytest.mark.asyncio
async def test_org_less_user_gets_no_graph_task_and_is_told() -> None:
    response, bt = await _run({"id": "u1", "organization_id": None})

    assert response.processed_count == 1
    assert bt.tasks == []
    assert "knowledge graph not updated" in response.message


@pytest.mark.asyncio
async def test_user_with_org_still_schedules_graph_task() -> None:
    response, bt = await _run({"id": "u1", "organization_id": "org-1"})

    assert response.processed_count == 1
    assert len(bt.tasks) == 1
    assert bt.tasks[0].args[-1] == "org-1"
    assert "knowledge graph not updated" not in response.message
