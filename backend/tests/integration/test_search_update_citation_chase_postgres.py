"""GOO-319: a required scheduled citation chase retains and reuses receipts.

Native PostgreSQL and real update/import services; provider responses and the
approved protocol are controlled fixtures, not live OpenAlex evidence.
"""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timedelta
from typing import Any, cast

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from src.schemas.research_engine import SearchScheduleCreate
from src.services.research_engine import search_update_rules as rules
from src.services.research_engine import search_update_service as updates
from src.services.research_engine.connectors.base import SearchTrace
from src.services.research_engine.project_access import ResearchAction
from tests.integration import test_review_versions_postgres as proof
from tests.integration.test_screening_queue_postgres import (  # noqa: F401
    screening_factory,
)

pytestmark = [pytest.mark.integration, pytest.mark.requires_postgres]


class _Citations:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    async def citations(
        self,
        work_id: str,
        direction: str,
        max_results: int,
        *,
        search_trace: SearchTrace,
        into: list[dict[str, Any]] | None = None,
    ) -> list[dict[str, Any]]:
        self.calls.append((work_id, direction))
        works = [
            {
                "id": f"https://openalex.org/W{9000000 + len(self.calls)}",
                "display_name": f"Citation found {direction}",
            }
        ]
        if into is not None:
            into.extend(works)
        await search_trace.record_page(
            endpoint="https://openalex.invalid/works",
            params={"direction": direction, "seed": work_id},
            provider_count=len(works),
            has_more=False,
        )
        return works


async def test_required_scheduled_citation_chase_reloads_and_reuses_receipts(
    screening_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    method: dict[str, Any] = deepcopy(proof.SNAPSHOT)
    requirement = {"required": True, "directions": ["backward", "forward"]}
    method["sources_search"]["citation_chasing"] = requirement
    monkeypatch.setattr(proof, "SNAPSHOT", method)
    world = await proof._world(screening_factory)
    await proof._corpus(world)
    citations = _Citations()
    world.connectors["openalex"] = citations
    schedule, _ = await proof._as(
        world,
        "O",
        ResearchAction.SUPERVISE,
        lambda db, ctx: updates.create_schedule(
            db,
            ctx,
            world.ids["O"],
            SearchScheduleCreate(
                source_run_id=world.ids["run"],
                step_id="search",
                strategy_version=world.strategy["strategy_version"],
                cron=proof.CRON,
                timezone=proof.TZ,
                idempotency_key="required-chase",
            ),
        ),
    )
    local = datetime.strptime(cast(str, schedule.next_fire_local), rules.LOCAL_FORMAT)
    zone = rules.parse_schedule(proof.CRON, proof.TZ)[1]
    world.crossref.docs = [
        proof._doc(k) for k in ("r1", "r2", "r3", "r4", "r5", "r6", "n1")
    ]
    async with screening_factory() as db:
        executions = await updates.claim_due(
            db, rules.to_utc(local, zone) + timedelta(minutes=1)
        )
    assert len(executions) == 1
    execution = executions[0]
    async with screening_factory() as db:
        assert (
            await updates.run_execution(
                db,
                execution,
                lambda _org: world.connectors,
                worker="required-chase-proof",
            )
            == "succeeded"
        )

    # A new request reloads the retained execution result, not an in-memory reply.
    delta = await proof._delta(world, execution)
    chasing = delta.citation_chasing
    assert chasing["required"] and chasing["status"] == "performed"
    assert chasing["directions"] == ["backward", "forward"]
    assert chasing["seeds_over_cap"] == 0
    assert len(chasing["chases"]) == 2
    assert {direction for _, direction in citations.calls} == {"backward", "forward"}
    assert len(citations.calls) == 2
    receipts = {chase["receipt_id"] for chase in chasing["chases"]}
    async with screening_factory() as db:
        await db.execute(text("SET TRANSACTION READ ONLY"))
        rows = (
            await db.execute(
                text(
                    "SELECT id, declared, observed FROM research_import_receipts "
                    "WHERE collection_id = :id AND kind = 'citation_chase'"
                ),
                {"id": world.p1},
            )
        ).all()
    assert {str(row.id) for row in rows} == receipts
    assert {row.declared["direction"] for row in rows} == {"backward", "forward"}
    assert all(
        row.observed["protocol_version_id"] == str(world.protocol) for row in rows
    )
    assert all(row.observed["protocol_requires"] == requirement for row in rows)
    assert all(row.observed["error_type"] is None for row in rows)

    # Retry precisely the same scheduled chase identities in another session.
    seeds = sorted({chase["seed_report_id"] for chase in chasing["chases"]})
    async with screening_factory() as db:
        replay = await updates._chase(
            db,
            {
                "collection_id": world.p1,
                "owner_id": world.ids["O"],
                "execution_id": execution,
            },
            seeds,
            world.connectors,
        )
    assert {chase["receipt_id"] for chase in replay["chases"]} == receipts
    assert len(citations.calls) == 2
    assert (
        await proof._scalar(
            screening_factory,
            "SELECT count(*) FROM research_import_receipts "
            "WHERE collection_id = :id AND kind = 'citation_chase'",
            id=world.p1,
        )
        == 2
    )
