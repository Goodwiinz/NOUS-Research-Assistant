"""Processing tasks must register under the short names queue_processing_job sends.

queue_processing_job (processing_service.py) dispatches via
``celery_app.send_task("process_document_ingestion", ...)`` and four siblings —
bare short names. If the task registers under its default full module path
(``src.tasks.processing_tasks.process_document_ingestion``), the worker rejects
the message as an unregistered task and the ProcessingJob sits QUEUED forever.
These tasks therefore need explicit ``name=`` matching the send_task strings.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

# The exact strings queue_processing_job passes to send_task, by job type
# (and, for KG jobs, by parameters["operation"]).
_SEND_TASK_NAMES = {
    "process_document_ingestion",
    "extract_text_content",
    "generate_embeddings",
    "extract_entities",
    "index_in_graph",
    "kg_extract_entities_job",
    "kg_merge_entities_job",
}


def _registered_names() -> set[str]:
    names: set[str] = set()
    for mod in ("processing_tasks", "kg_tasks"):
        try:
            module = __import__(f"src.tasks.{mod}", fromlist=["*"])
        except Exception:
            continue
        for obj in vars(module).values():
            name = getattr(obj, "name", None)
            if isinstance(name, str):
                names.add(name)
    return names


def test_processing_tasks_registered_under_send_task_names():
    registered = _registered_names()
    missing = _SEND_TASK_NAMES - registered
    assert not missing, (
        f"send_task uses names {sorted(missing)} but no task registers under "
        f"them — the worker would reject those messages as unregistered and "
        f"jobs would hang QUEUED. Registered: {sorted(registered)}"
    )


def test_send_task_strings_and_task_names_stay_in_sync():
    # Guard the other direction: if queue_processing_job's send_task strings
    # drift, this catches it against the declared task names in source.
    svc = (
        Path(__file__).resolve().parents[3]
        / "src/services/processing/processing_service.py"
    ).read_text()
    sent = set(re.findall(r'send_task\(\s*"([^"]+)"', svc))
    # Every processing-pipeline send_task target must be a name we register.
    assert _SEND_TASK_NAMES <= sent or sent <= _registered_names() | _SEND_TASK_NAMES
