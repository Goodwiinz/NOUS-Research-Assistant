"""Shared machine-handoff test helpers (not a test module).

Used by test_handoff.py and test_account_runs.py so neither imports the
other's module. The rows land in the SQLite schema from test_runs.py's ``db``
fixture.
"""

from __future__ import annotations

from types import SimpleNamespace
from uuid import UUID, uuid4

from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

from src.models.agent_run import AgentRun
from src.models.thread import Thread
from src.services.agent.agent_submission_service import accept_submission
from src.services.agent.schemas import AgentExecuteRequest, AgentMessage
from src.shared.enums import JobStatus
from tests.unit.services.harness.test_runs import ORG, THREAD, USER

OWNER = SimpleNamespace(id=USER, organization_id=ORG)


async def parent_run(
    db: AsyncSession,
    status: JobStatus = JobStatus.RUNNING,
    thread_id: UUID = THREAD,
) -> str:
    """A NOUS chat run on ``thread_id``, moved to ``status``."""
    accepted = await accept_submission(
        db,
        current_user=OWNER,
        request=AgentExecuteRequest(
            messages=[
                AgentMessage(role="user", content="fix it", client_message_id=uuid4())
            ],
            thread_id=str(thread_id),
        ),
        thread=await db.get(Thread, thread_id),
    )
    await db.execute(
        update(AgentRun)
        .where(AgentRun.job_id == accepted.run_id)
        .values(status=status.value)
    )
    await db.commit()
    return accepted.run_id
