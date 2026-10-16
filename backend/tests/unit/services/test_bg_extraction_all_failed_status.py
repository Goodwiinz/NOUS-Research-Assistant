"""run_background_extraction must not report "completed" when every document failed.

The worker catches each per-document failure (``bump("failed")``) and then
unconditionally wrote ``{"status": "completed"}`` after the loop. With a
revoked key, a quota error or a failing append, the status read back by
``GET /research/extraction-tasks/{task_id}`` was ``completed`` with
``completed == 0`` and ``failed == total`` — the UI cleared its banner and
showed an empty grid with no error. Mirrors the honest-outcome rule in
``kg_extract_entities_job`` (processing_tasks.py): nothing recorded and
something errored is a failed run, a partial run stays completed.

Driven with fakes: an in-memory session that answers the worker's three
queries, a chat-completions stub, and no-op persistence hooks. Redis is
stubbed out so the status lives in the L1 dict only.

Mutation check (docs/engineering/testing.md): remove the
``completed == 0 and failed > 0`` branch before the final
``set_extraction_status`` in ``run_background_extraction`` and
``test_all_documents_failed_marks_run_failed`` fails with
``status == "completed"``; ``test_partial_success_stays_completed`` must keep
passing either way.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import pytest

from src.services.research import extraction_matrix_service as ems

pytestmark = pytest.mark.unit


class _Result:
    def __init__(self, first: Any = None, scalar: Any = None) -> None:
        self._first, self._scalar = first, scalar

    def first(self) -> Any:
        return self._first

    def scalar_one_or_none(self) -> Any:
        return self._scalar


class _Session:
    """Answers the worker's queries in order: (matrix, version), then per
    document: not-yet-observed, then the scoped document."""

    def __init__(self, results: list[_Result]) -> None:
        self._results = list(results)
        self.commits = 0
        self.rollbacks = 0

    async def __aenter__(self) -> "_Session":
        return self

    async def __aexit__(self, *_: Any) -> None:
        return None

    async def execute(self, _stmt: Any) -> _Result:
        return self._results.pop(0)

    async def commit(self) -> None:
        self.commits += 1

    async def rollback(self) -> None:
        self.rollbacks += 1


class _LLM:
    """Chat-completions stub: raises for document texts in ``fail_for``."""

    def __init__(self, fail_for: set[str]) -> None:
        self.fail_for, self.calls = fail_for, 0
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self.create))

    async def create(self, **kwargs: Any) -> Any:
        self.calls += 1
        prompt = kwargs["messages"][0]["content"]
        for text in self.fail_for:
            if text in prompt:
                raise RuntimeError("Error code: 401 - invalid api key")
        message = SimpleNamespace(content=json.dumps({"Sample size": {"value": 1}}))
        return SimpleNamespace(choices=[SimpleNamespace(message=message)])


def _wire(
    monkeypatch: pytest.MonkeyPatch, texts: list[str], llm: _LLM
) -> tuple[list[Any], dict[str, str], "_Session", list[Any]]:
    matrix = SimpleNamespace(project_id=uuid4())
    version = SimpleNamespace(fields=[{"name": "Sample size", "type": "text"}])
    docs = [
        SimpleNamespace(id=uuid4(), content_text=text, checksum_sha256=None)
        for text in texts
    ]
    results = [_Result(first=(matrix, version))]
    for doc in docs:
        results += [_Result(first=None), _Result(scalar=doc)]
    session = _Session(results)
    appended: list[Any] = []

    async def no_redis() -> None:
        return None

    async def fake_read_whole_text(
        text: str, fields: Any, read: Any
    ) -> tuple[dict[str, Any], list[Any], bool]:
        await read(text)  # one LLM call per document
        return {}, [], False

    async def fake_lock(_db: Any, _project_id: Any) -> None:
        return None

    async def fake_append(_db: Any, **kwargs: Any) -> None:
        appended.append(kwargs["document"])

    monkeypatch.setattr("src.services.agent.job_store.get_redis", no_redis)
    monkeypatch.setattr(ems, "AsyncSessionLocal", lambda: session)
    monkeypatch.setattr(ems, "read_whole_text", fake_read_whole_text)
    monkeypatch.setattr(ems, "lock_active_project", fake_lock)
    monkeypatch.setattr(ems, "append_machine_observations", fake_append)
    monkeypatch.setattr(
        ems.ExtractionMatrixService,
        "_get_openai_client",
        staticmethod(lambda: (llm, "stub-model")),
    )
    pinned = {str(doc.id): ems.document_source_hash(doc) for doc in docs}
    return docs, pinned, session, appended


async def _run(docs: list[Any], pinned: dict[str, str], task_id: str) -> dict:
    ems._extraction_status.pop(task_id, None)
    await ems.ExtractionMatrixService().run_background_extraction(
        matrix_id=uuid4(),
        document_ids=[doc.id for doc in docs],
        columns=[],
        task_id=task_id,
        form_version_id=uuid4(),
        initiated_by_user_id=uuid4(),
        source_hashes=pinned,
    )
    return ems._extraction_status[task_id]


@pytest.mark.asyncio
async def test_all_documents_failed_marks_run_failed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    llm = _LLM(fail_for={"alpha text", "beta text"})
    docs, pinned, session, appended = _wire(
        monkeypatch, ["alpha text", "beta text"], llm
    )

    status = await _run(docs, pinned, "task-all-failed")

    assert llm.calls == 2
    assert (status["total"], status["completed"], status["failed"]) == (2, 0, 2)
    assert appended == [] and session.commits == 0 and session.rollbacks == 2
    assert status["status"] == "failed"
    assert status["error"] == "2 of 2 documents failed; nothing was recorded"


@pytest.mark.asyncio
async def test_partial_success_stays_completed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    llm = _LLM(fail_for={"alpha text"})
    docs, pinned, session, appended = _wire(
        monkeypatch, ["alpha text", "beta text"], llm
    )

    status = await _run(docs, pinned, "task-partial")

    assert (status["total"], status["completed"], status["failed"]) == (2, 1, 1)
    assert appended == [docs[1]] and session.commits == 1
    assert status["status"] == "completed"
    assert status["error"] is None
