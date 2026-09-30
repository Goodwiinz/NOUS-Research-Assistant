"""CLI: backfill project report identities from existing research sources (GOO-299).

Examples (from ``backend/``):
  python -m scripts.backfill_report_identities            # dry run (default)
  python -m scripts.backfill_report_identities --apply

Sources are grouped by their canonical project (Collection) through
run -> blueprint -> engine project, in ``created_at, id`` order, and passed to
``identity_service.observe_sources``: identity is decided by identifiers only,
never by title. ``rag_store`` sources are skipped. Sources whose engine project
has no Collection are counted as ``mapping_required`` and left alone.

Each Collection is one transaction. A dry run executes the same code and rolls
it back, so its counts are exactly what ``--apply`` would write. Reruns are
no-ops: already-observed sources are skipped and ``UNIQUE(source_id)`` holds.
Original ``research_sources`` rows are never modified.
"""

from __future__ import annotations

import argparse
import asyncio
import json
from collections import defaultdict
from typing import Any
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from src.models.research_blueprint import ResearchBlueprint
from src.models.research_project import ResearchProject
from src.models.research_report import ResearchReport
from src.models.research_run import ResearchRun
from src.models.research_source import ResearchSource
from src.services.research_engine.identity_service import observe_sources


async def backfill(
    factory: async_sessionmaker[AsyncSession], *, apply: bool
) -> dict[str, int]:
    async with factory() as db:
        rows = (
            await db.execute(
                select(
                    ResearchSource.id,
                    ResearchSource.connector_type,
                    ResearchProject.collection_id,
                )
                .join(ResearchRun, ResearchRun.id == ResearchSource.run_id)
                .join(
                    ResearchBlueprint, ResearchBlueprint.id == ResearchRun.blueprint_id
                )
                .join(
                    ResearchProject, ResearchProject.id == ResearchBlueprint.project_id
                )
                .order_by(ResearchSource.created_at, ResearchSource.id)
            )
        ).all()
    # ponytail: one collection's sources are loaded at once; batch per collection
    # if a single project ever holds more sources than fit in memory.
    by_collection: dict[UUID, list[UUID]] = defaultdict(list)
    skipped_rag_store = mapping_required = 0
    for source_id, connector_type, collection_id in rows:
        if collection_id is None:
            mapping_required += 1
        elif connector_type == "rag_store":
            skipped_rag_store += 1  # observe_sources skips these too
        else:
            by_collection[collection_id].append(source_id)

    reports = observations = 0
    for collection_id, source_ids in by_collection.items():
        async with factory() as db:
            by_id: dict[Any, ResearchSource] = {
                source.id: source
                for source in (
                    await db.execute(
                        select(ResearchSource).where(ResearchSource.id.in_(source_ids))
                    )
                )
                .scalars()
                .all()
            }
            before = await _report_count(db, collection_id)
            created = await observe_sources(
                db,
                collection_id=collection_id,
                sources=[by_id[source_id] for source_id in source_ids],
            )
            reports += await _report_count(db, collection_id) - before
            observations += len(created)
            if apply:
                await db.commit()
            else:
                await db.rollback()
    return {
        "collections": len(by_collection),
        "sources": len(rows),
        "would_create_reports": reports,
        "would_create_observations": observations,
        "skipped_rag_store": skipped_rag_store,
        "mapping_required": mapping_required,
    }


async def _report_count(db: AsyncSession, collection_id: UUID) -> int:
    return int(
        (
            await db.execute(
                select(func.count()).where(
                    ResearchReport.collection_id == collection_id
                )
            )
        ).scalar_one()
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--apply",
        action="store_true",
        help="commit per collection (default is a rolled-back dry run)",
    )
    args = parser.parse_args()

    from src.core.database import AsyncSessionLocal

    result = asyncio.run(backfill(AsyncSessionLocal, apply=args.apply))
    print(json.dumps({"mode": "apply" if args.apply else "dry-run", **result}))


if __name__ == "__main__":
    main()
