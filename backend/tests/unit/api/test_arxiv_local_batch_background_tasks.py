"""Regression: the local-paper batch endpoint must forward a real BackgroundTasks.

`process_batch_local_papers` previously called
`extract_features_from_local_pdfs(..., background_tasks=None, ...)`. When the
request's `extraction_options` enabled `update_knowledge_graph`, the inner
handler does `background_tasks.add_task(...)` per paper — `None.add_task` raised
AttributeError, which the broad `except` reported as a failed batch, silently
dropping the KG update. The endpoint now injects a real BackgroundTasks and
threads it through.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import BackgroundTasks

import src.api.arxiv.arxiv_local as arxiv_local

pytestmark = pytest.mark.unit


def _fake_pdf(stem: str = "2304.04290"):
    f = MagicMock()
    f.stem = stem
    f.stat.return_value.st_mtime = 123.0
    return f


@pytest.mark.asyncio
async def test_batch_forwards_real_background_tasks_not_none():
    bt = BackgroundTasks()
    inner = AsyncMock(
        return_value=MagicMock(processed_count=1, results=[{"paper_id": "x"}])
    )

    with (
        patch.object(arxiv_local, "ARXIV_DATA_PATH") as data_path,
        patch.object(arxiv_local, "_extract_features_from_local_pdfs", inner),
    ):
        data_path.glob.return_value = [_fake_pdf()]

        await arxiv_local.process_batch_local_papers.__wrapped__(
            background_tasks=bt,
            batch_size=10,
            offset=0,
            request={"extraction_options": {"update_knowledge_graph": True}},
            current_user={"organization_id": "org-1"},
        )

    inner.assert_awaited_once()
    # The injected BackgroundTasks must be threaded through — never None.
    assert inner.await_args.kwargs["background_tasks"] is bt


@pytest.mark.asyncio
async def test_batch_signature_requires_background_tasks():
    import inspect

    params = inspect.signature(arxiv_local.process_batch_local_papers).parameters
    assert "background_tasks" in params
    assert params["background_tasks"].annotation is BackgroundTasks
    # No default → FastAPI injects a real instance per request.
    assert params["background_tasks"].default is inspect.Parameter.empty
