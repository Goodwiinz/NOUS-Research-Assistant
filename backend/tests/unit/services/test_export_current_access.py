"""GOO-348: thread export must recheck *current* workspace access.

``ExportService._load_thread`` authorised on author ids alone
(``thread.created_by_id`` / ``conversation.created_by_id``). A former member
who created a thread in a private workspace kept exporting it — including
messages appended after their removal — even though
``workspace_access.user_can_access_workspace`` rejects them.

Every export path (single, streaming, batch, preview) goes through
``_load_thread``, so the guard lives there.

Mutation check: drop the ``user_can_access_workspace`` call in
``ExportService._load_thread`` and
``pytest -q backend/tests/unit/services/test_export_current_access.py``
fails on the former-member cases.
"""

from __future__ import annotations

import datetime
import io
import zipfile
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.models.chat_message import MessageRole
from src.services.research.export_service import ExportOptions, ExportService
from src.shared.export_schemas import ExportFormat

pytestmark = pytest.mark.unit

AUTHOR = "u-author"
OWNER = "u-owner"


def _thread(*, is_public: bool = False, author_is_member: bool = False) -> Any:
    msg = MagicMock()
    msg.id = "m-1"
    msg.is_deleted = False
    msg.superseded_by_message_id = None
    msg.role = MessageRole.USER
    msg.content = "B-only secret appended after removal"
    msg.created_at = datetime.datetime(2026, 1, 1)
    msg.model_name = "gpt"
    msg.token_usage = None
    msg.token_count = 1
    msg.latency_ms = 1
    msg.feedback_rating = None
    msg.feedback_text = None
    msg.citations = []
    msg.has_attachments = False

    thread = MagicMock()
    thread.id = "t-1"
    thread.created_by_id = AUTHOR
    thread.title = "Thread"
    thread.summary = None
    thread.status = None
    thread.created_at = thread.updated_at = thread.last_message_at = msg.created_at
    thread.message_count = 1
    thread.token_count = 0
    thread.conversation_id = "c-1"
    thread.messages = [msg]

    conversation = thread.conversation
    conversation.is_deleted = False
    conversation.created_by_id = AUTHOR
    workspace = conversation.workspace
    workspace.is_deleted = False
    workspace.is_public = is_public
    workspace.owner_id = OWNER
    workspace.is_member = MagicMock(return_value=author_is_member)
    return thread


def _service(thread: Any) -> ExportService:
    db = AsyncMock()
    result = MagicMock()
    result.unique.return_value.scalar_one_or_none.return_value = thread
    db.execute = AsyncMock(return_value=result)
    return ExportService(db)


@pytest.mark.asyncio
async def test_former_member_author_cannot_load_private_thread() -> None:
    svc = _service(_thread(author_is_member=False))
    assert await svc._load_thread("t-1", AUTHOR, ExportOptions()) is None


@pytest.mark.asyncio
async def test_former_member_author_single_export_denied() -> None:
    svc = _service(_thread(author_is_member=False))
    with pytest.raises(ValueError):
        await svc.export_thread("t-1", AUTHOR, ExportFormat.MARKDOWN, ExportOptions())


@pytest.mark.asyncio
async def test_former_member_author_batch_export_has_no_thread_bytes() -> None:
    svc = _service(_thread(author_is_member=False))
    content, _, _ = await svc.export_batch(
        ["t-1", "t-1"], AUTHOR, ExportFormat.MARKDOWN, ExportOptions()
    )
    with zipfile.ZipFile(io.BytesIO(content)) as zf:
        assert zf.namelist() == []
    assert b"B-only secret" not in content


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "is_public,author_is_member",
    [(False, True), (True, False)],
    ids=["current-member", "public-workspace"],
)
async def test_current_access_author_still_exports(
    is_public: bool, author_is_member: bool
) -> None:
    svc = _service(_thread(is_public=is_public, author_is_member=author_is_member))
    out = await svc._load_thread("t-1", AUTHOR, ExportOptions())
    assert out is not None
    assert out.messages[0].content == "B-only secret appended after removal"
