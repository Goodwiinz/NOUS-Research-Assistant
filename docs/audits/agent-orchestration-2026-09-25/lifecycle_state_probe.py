import asyncio, sys
from uuid import uuid4
from datetime import datetime, timezone
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[3] / 'backend'))
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
from src.models.agent_run import AgentRun
from src.services.agent import agent_run_service as svc
from src.shared.enums import JobStatus

async def main():
    engine = create_async_engine('sqlite+aiosqlite:///:memory:')
    async with engine.begin() as conn:
        await conn.run_sync(AgentRun.__table__.create)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    job_id = str(uuid4())
    async with factory() as db:
        await svc.upsert_run(db, job_id=job_id, status=JobStatus.STOPPING, user_id=uuid4())
        run = await db.get(AgentRun, job_id)
        run.cancel_requested_at = datetime.now(timezone.utc)
        await db.commit()
        await svc.upsert_run(db, job_id=job_id, status=JobStatus.COMPLETED)
        print('Stop acknowledgement bypass:', run.status, 'cancel_requested=', bool(run.cancel_requested_at))
    job2 = str(uuid4())
    async with factory() as db:
        await svc.upsert_run(db, job_id=job2, status=JobStatus.RUNNING, user_id=uuid4())
    async with factory() as delayed_db, factory() as finisher_db:
        loaded, release = asyncio.Event(), asyncio.Event()
        class DelayedRead:
            def __getattr__(self, name):
                return getattr(delayed_db, name)
            async def get(self, *args, **kwargs):
                row = await delayed_db.get(*args, **kwargs)
                loaded.set()
                await release.wait()
                return row
        delayed = asyncio.create_task(svc.upsert_run(DelayedRead(), job_id=job2, status=JobStatus.AWAITING_CONFIRMATION))
        await loaded.wait()
        await svc.upsert_run(finisher_db, job_id=job2, status=JobStatus.COMPLETED)
        release.set()
        await delayed
    async with factory() as verify:
        actual = await verify.get(AgentRun, job2)
        print('Terminal resurrected by stale projection:', actual.status)
    await engine.dispose()
asyncio.run(main())
