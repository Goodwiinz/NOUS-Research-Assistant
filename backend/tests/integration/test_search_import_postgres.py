"""Real PostgreSQL proof for GOO-300 import idempotency and corpus export.

``identity_factory`` (imported from the GOO-299 suite) drops the identity and
import tables and rebuilds them with revisions ``c9d2e4f6a8b1`` and
``d4e6f8a0b2c3``, so both migrations' constraints are what these tests exercise.

Mutation verification (docs/engineering/testing.md); the full record, with the
observed failures, is in ``docs/testing/agent-orchestration-mutation-checks.md``
(GOO-300 section). Each mutant was restored from a byte-for-byte copy and
rerun green:

- ``corpus_service.import_file`` dedup lookup (``existing = await
  _receipt(db, collection_id, dedup_key=dedup_key)``) replaced by ``None`` ->
  ``-k concurrent_duplicate_import`` fails: the second writer, after waiting on
  the Collection lock, hits ``uq_research_import_receipt_dedup`` and gets 409
  ``import_conflict`` instead of the replayed receipt.
- ``corpus_service.chase_citations`` phase-3 replay recheck removed ->
  ``-k concurrent_identical_chases`` fails the same way (409 instead of replay).
- Export read-only guarantee: a write-back ``receipt.declared = declared``
  injected into ``corpus_export._imports`` -> ``-k downloaded_package`` fails:
  the second export in the same session autoflushes the rewritten declaration
  and its ``body_sha256`` differs from the first.
- ``corpus_export._source`` provenance ``full_text`` strip disabled ->
  ``-k downloaded_package`` fails on the ``RAG-FULLTEXT-SENTINEL`` assertion.
"""

import asyncio
import io
import json
import zipfile
from types import SimpleNamespace
from typing import Any, TypeAlias, cast
from uuid import UUID, uuid4

import pytest
from fastapi import HTTPException, Response, UploadFile
from sqlalchemy import func, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from src.api.research_engine.corpus import (
    chase_citations_route,
    corpus_coverage_route,
    export_corpus_route,
    get_import_route,
    import_search_results_route,
    list_imports_route,
)
from src.models.research_decision import ResearchDecisionEvent, ResearchDecisionStream
from src.models.research_import import ResearchImportReceipt, ResearchImportRecord
from src.models.research_report import (
    ResearchReport,
    ResearchReportIdentifier,
    ResearchReportObservation,
)
from src.models.research_run import ResearchRun
from src.models.research_source import ResearchSource
from src.models.research_step import ResearchStep
from src.models.workspace import Workspace, WorkspaceMember
from src.schemas.research_engine import (
    CitationChaseRequest,
    CoverageRequest,
    ImportDeclaration,
    ReportMergeRequest,
)
from src.services.research_engine import corpus_export, corpus_service, identity_service
from src.services.research_engine.project_access import ResearchAction, resolve_project
from tests.integration.test_report_identity_postgres import (  # noqa: F401
    _seed,
    _wait_until_blocked,
    identity_factory,
)

pytestmark = pytest.mark.integration

PARTIAL = b"""TY  - JOUR
TI  - Record one
DO  - 10.1000/one
N1  - RESTRICTED-RAW-SENTINEL
ER  -

TY  - JOUR
TI  - Record two
DO  - 10.1000/two
ER  -

TY  - JOUR
TI  - Record three
DO  - 10.1000/three
ER  -

TY  - JOUR
AU  - No Title, X
ER  -

TY  - JOUR
TI  - Never ends
"""
DECLARATION = ImportDeclaration(database="Embase (Ovid)", query_text="aspirin")
Factory: TypeAlias = async_sessionmaker[AsyncSession]


async def _import(
    factory: Factory,
    ids: dict[str, UUID],
    data: bytes = PARTIAL,
    declaration: ImportDeclaration = DECLARATION,
    user: str = "O",
) -> tuple[Any, bool]:
    async with factory() as db:
        context = await resolve_project(
            db, ids["collection"], ids[user], ResearchAction.EDIT
        )
        result = await corpus_service.import_file(
            db,
            context,
            ids[user],
            declaration,
            fmt="ris",
            filename="partial.ris",
            data=data,
        )
        await db.commit()
    return result


async def _count(db: AsyncSession, model: Any) -> int:
    return cast(
        int, (await db.execute(select(func.count()).select_from(model))).scalar_one()
    )


async def _observed_source(factory: Factory, run_id: UUID, **extra: Any) -> UUID:
    """One provider snapshot observed into GOO-299 identity (DOI 10.1000/one)."""
    async with factory() as db:
        source = ResearchSource(
            run_id=run_id,
            connector_type="openalex",
            title="Provider copy of record one",
            metadata_={"identifiers": {"doi": "10.1000/one"}, **extra},
        )
        db.add(source)
        await db.flush()
        await identity_service.observe_sources(
            db,
            collection_id=cast(UUID, (await _collection_of(db, run_id))),
            sources=[source],
        )
        await db.commit()
        return cast(UUID, source.id)


async def _collection_of(db: AsyncSession, run_id: UUID) -> UUID:
    return cast(
        UUID,
        (
            await db.execute(
                text("""SELECT p.collection_id FROM research_runs r
                    JOIN research_blueprints b ON b.id = r.blueprint_id
                    JOIN research_projects p ON p.id = b.project_id
                    WHERE r.id = :id"""),
                {"id": run_id},
            )
        ).scalar_one(),
    )


@pytest.mark.asyncio
async def test_import_commit_reopen_and_reimport(identity_factory: Factory) -> None:
    ids = await _seed(identity_factory)
    source_id = await _observed_source(identity_factory, ids["run"])

    first, created = await _import(identity_factory, ids)
    assert created is True
    assert (first.accepted_count, first.rejected_count) == (3, 2)

    async with identity_factory() as db:  # a new session: what was committed
        receipt = await db.get(ResearchImportReceipt, first.id)
        assert receipt is not None and receipt.declared["query_text"] == "aspirin"
        records = (
            (
                await db.execute(
                    select(ResearchImportRecord)
                    .where(ResearchImportRecord.receipt_id == first.id)
                    .order_by(ResearchImportRecord.record_index)
                )
            )
            .scalars()
            .all()
        )
        assert [r.status for r in records] == ["accepted"] * 3 + ["rejected"] * 2
        assert all(r.report_id for r in records[:3])
        assert [r.rejection_reason for r in records[3:]] == [
            "missing_title",
            "unterminated_record",
        ]
        assert records[4].raw == "TY  - JOUR\nTI  - Never ends\n"
        observed_report = (
            await db.execute(
                select(ResearchReportObservation.report_id).where(
                    ResearchReportObservation.source_id == source_id
                )
            )
        ).scalar_one()
        # Shares 10.1000/one with the provider snapshot: same report.
        assert records[0].report_id == observed_report
        counts = (
            await _count(db, ResearchImportReceipt),
            await _count(db, ResearchImportRecord),
            await _count(db, ResearchReport),
        )

    again, created_again = await _import(identity_factory, ids)
    assert (again.id, again.replayed, created_again) == (first.id, True, False)
    async with identity_factory() as db:
        assert (
            await _count(db, ResearchImportReceipt),
            await _count(db, ResearchImportRecord),
            await _count(db, ResearchReport),
        ) == counts

    edited, _ = await _import(identity_factory, ids, PARTIAL + b"ER  - \n")
    assert (edited.version, edited.previous_receipt_id) == (2, first.id)

    with pytest.raises(HTTPException) as conflict:
        await _import(
            identity_factory,
            ids,
            declaration=ImportDeclaration(database="Embase (Ovid)", query_text="x"),
        )
    assert conflict.value.status_code == 409
    assert cast(dict, conflict.value.detail)["code"] == "import_declaration_conflict"


@pytest.mark.asyncio
async def test_concurrent_duplicate_import_yields_one_receipt(
    identity_factory: Factory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Session 2 waits on the Collection lock, then replays session 1's receipt."""
    ids = await _seed(identity_factory)
    paused, release = asyncio.Event(), asyncio.Event()

    async def run(db: AsyncSession, pause: bool) -> Any:
        context = await resolve_project(
            db, ids["collection"], ids["O"], ResearchAction.EDIT
        )
        receipt, _created = await corpus_service.import_file(
            db,
            context,
            ids["O"],
            DECLARATION,
            fmt="ris",
            filename="partial.ris",
            data=PARTIAL,
        )
        if pause:
            paused.set()
            await release.wait()
        await db.commit()
        return receipt

    first, second, observer = identity_factory(), identity_factory(), identity_factory()
    async with first, second, observer:
        pid = cast(
            int, (await second.execute(text("SELECT pg_backend_pid()"))).scalar_one()
        )
        winner = asyncio.create_task(run(first, True))
        await asyncio.wait_for(paused.wait(), 5)
        loser = asyncio.create_task(run(second, False))
        try:
            await _wait_until_blocked(observer, pid)
            release.set()
            results = await asyncio.gather(winner, loser)
        finally:
            release.set()
            for task in (winner, loser):
                if not task.done():
                    task.cancel()
            await asyncio.gather(winner, loser, return_exceptions=True)

    assert results[0].id == results[1].id
    assert [r.replayed for r in results] == [False, True]
    async with identity_factory() as db:
        assert await _count(db, ResearchImportReceipt) == 1
        assert await _count(db, ResearchImportRecord) == 5


class _BarrierConnector:
    """Returns only once both chases are past phase 1 (both saw no receipt)."""

    def __init__(self) -> None:
        self.calls = 0
        self.both = asyncio.Event()

    async def citations(
        self,
        work_id: str,
        direction: str,
        max_results: int,
        *,
        search_trace: Any,
        into: list[dict[str, Any]] | None = None,
    ) -> list[dict[str, Any]]:
        self.calls += 1
        if self.calls == 2:
            self.both.set()
        await asyncio.wait_for(self.both.wait(), 5)
        works = [{"id": "https://openalex.org/W9", "display_name": "Cited"}]
        if into is not None:
            into.extend(works)
        return works


@pytest.mark.asyncio
async def test_concurrent_identical_chases_yield_one_receipt(
    identity_factory: Factory,
) -> None:
    ids = await _seed(identity_factory)
    seed = uuid4()
    async with identity_factory() as db:
        db.add(
            ResearchReport(id=seed, collection_id=ids["collection"], title_snapshot="S")
        )
        await db.flush()
        db.add(
            ResearchReportIdentifier(
                collection_id=ids["collection"],
                report_id=seed,
                kind="openalex",
                value="W1",
            )
        )
        await db.commit()
    connector = _BarrierConnector()
    request = CitationChaseRequest(
        seed_report_id=seed, direction="backward", idempotency_key="same-key"
    )

    async def chase() -> Any:
        async with identity_factory() as db:
            receipt, _created = await corpus_service.chase_citations(
                db,
                project_id=ids["collection"],
                user_id=ids["O"],
                data=request,
                connector=connector,
            )
            await db.commit()
            return receipt

    results = await asyncio.gather(chase(), chase())

    assert connector.calls == 2  # both passed the phase-1 replay check
    assert results[0].id == results[1].id
    assert sorted(r.replayed for r in results) == [False, True]
    async with identity_factory() as db:
        assert await _count(db, ResearchImportReceipt) == 1
        assert await _count(db, ResearchImportRecord) == 1


def _journal() -> dict[str, Any]:
    def execution(ex_id: str, provider: str, status: str) -> dict[str, Any]:
        return {
            "execution_id": ex_id,
            "step_id": "search-1",
            "provider": provider,
            "strategy_version": "s1",
            "status": status,
            "pages": {
                f"{ex_id}-p0": {
                    "page_id": f"{ex_id}-p0",
                    "page_index": 0,
                    "attempts": [
                        {
                            "attempt_id": "a1",
                            "page": {
                                "request": {
                                    "method": "GET",
                                    "endpoint": f"https://{provider}.test/q",
                                    "params": {"search": "aspirin", "per_page": 25},
                                }
                            },
                        }
                    ],
                }
            },
            "provider_attempts": {
                "a1": {
                    "requested_limit": 25,
                    "completion": "exhausted" if status == "ok" else "failed",
                    "error_type": None if status == "ok" else "TimeoutError",
                    "started_at": "2026-09-29T10:00:00+00:00",
                }
            },
        }

    return {
        "schema_version": 1,
        "strategies": {"s1": {"strategy_version": "s1"}},
        "executions": {
            "ex-ok": execution("ex-ok", "openalex", "ok"),
            "ex-to": execution("ex-to", "pubmed", "timed_out"),
        },
    }


async def _snapshot(db: AsyncSession) -> tuple[Any, ...]:
    models = (
        ResearchImportReceipt,
        ResearchImportRecord,
        ResearchReport,
        ResearchReportIdentifier,
        ResearchReportObservation,
        ResearchSource,
        ResearchDecisionEvent,
    )
    next_seq = (
        (await db.execute(select(ResearchDecisionStream.next_seq))).scalars().all()
    )
    return (*[await _count(db, m) for m in models], sorted(next_seq))


@pytest.mark.asyncio
async def test_downloaded_package_matches_persisted_rows(
    identity_factory: Factory,
) -> None:
    ids = await _seed(identity_factory)
    collection_id = ids["collection"]
    async with identity_factory() as db:
        await db.execute(
            update(ResearchRun)
            .where(ResearchRun.id == ids["run"])
            .values(
                status="completed",
                reproducibility_manifest={"_search_receipts_v1": _journal()},
            )
        )
        db.add(
            ResearchStep(
                run_id=ids["run"],
                step_index=0,
                step_type="search",
                output={
                    "coverage": {
                        "providers": {
                            "openalex": {
                                "execution_id": "ex-ok",
                                "status": "ok",
                                "returned": 1,
                                "limit": 25,
                            },
                            "pubmed": {
                                "execution_id": "ex-to",
                                "status": "timed_out",
                                "returned": 0,
                                "limit": 25,
                                "error_type": "TimeoutError",
                            },
                        },
                        "exhaustive": False,
                    }
                },
            )
        )
        db.add(
            ResearchSource(
                run_id=ids["run"],
                connector_type="rag_store",
                title="Workspace doc",
                metadata_={
                    "provenance": [
                        {
                            "connector_type": "rag_store",
                            "full_text": "RAG-FULLTEXT-SENTINEL",
                        }
                    ]
                },
            )
        )
        await db.commit()
    await _observed_source(
        identity_factory,
        ids["run"],
        provenance=[
            {"connector_type": "openalex", "full_text": "OA-FULLTEXT-SENTINEL"}
        ],
    )
    await _import(identity_factory, ids)  # restricted: sentinel in an N1 tag
    async with identity_factory() as db:
        context = await resolve_project(db, collection_id, ids["O"])
        legacy, _ = await corpus_service.import_file(
            db,
            context,
            ids["O"],
            ImportDeclaration(database="Legacy DB"),
            fmt="csv",
            filename="old.csv",
            data=b"title,doi\nLegacy paper,10.1000/two\n",
        )
        await db.commit()
    async with identity_factory() as db:
        reports = await identity_service.list_reports(db, collection_id=collection_id)
        by_title = {r.title_snapshot: r for r in reports}
        context = await resolve_project(
            db, collection_id, ids["A"], ResearchAction.ADJUDICATE
        )
        await identity_service.merge_reports(
            db,
            context,
            ids["A"],
            ReportMergeRequest(
                surviving_report_id=by_title["Provider copy of record one"].id,
                merged_report_ids=[by_title["Record two"].id],
                rationale="same study report",
                idempotency_key="merge-1",
            ),
        )
        await db.commit()

    async with identity_factory() as db:
        before = await _snapshot(db)
        history = [
            e.model_dump(mode="json")
            for e in await identity_service.history(db, collection_id=collection_id)
        ]
        final = {
            str(r.id): r.merged_into_report_id
            for r in (await db.execute(select(ResearchReport))).scalars().all()
        }

        def resolve(report_id: Any) -> str:
            current = str(report_id)
            while final.get(current):
                current = str(final[current])
            return current

        expected_links = {
            str(o.source_id): resolve(o.report_id)
            for o in (await db.execute(select(ResearchReportObservation))).scalars()
        } | {
            str(r.id): resolve(r.report_id)
            for r in (await db.execute(select(ResearchImportRecord))).scalars()
            if r.report_id
        }
        rows = {
            "sources": await _count(db, ResearchSource),
            "import_receipts": await _count(db, ResearchImportReceipt),
            "import_records": await _count(db, ResearchImportRecord),
            "reports": await _count(db, ResearchReport),
            "identifiers": await _count(db, ResearchReportIdentifier),
            "observations": await _count(db, ResearchReportObservation),
            "decisions": await _count(db, ResearchDecisionEvent),
        }

    user = SimpleNamespace(id=ids["O"])
    async with identity_factory() as db:  # one session, two exports
        json_response = await export_corpus_route(
            collection_id, export_format="json", current_user=user, db=db  # type: ignore[arg-type]
        )
        zip_response = await export_corpus_route(
            collection_id, export_format="zip", current_user=user, db=db  # type: ignore[arg-type]
        )
        assert not (db.new or db.dirty or db.deleted)
    for response in (json_response, zip_response):
        assert response.headers["content-disposition"].startswith("attachment;")
        for sentinel in (
            b"RESTRICTED-RAW-SENTINEL",
            b"RAG-FULLTEXT-SENTINEL",
            b"OA-FULLTEXT-SENTINEL",
        ):
            assert sentinel not in response.body
    with zipfile.ZipFile(io.BytesIO(zip_response.body)) as archive:
        zipped = archive.read("corpus.json")
    for sentinel in (b"RESTRICTED-RAW-SENTINEL", b"RAG-FULLTEXT-SENTINEL"):
        assert sentinel not in zipped

    package = json.loads(json_response.body)
    assert json.loads(zipped)["body_sha256"] == package["body_sha256"]
    rebuilt = corpus_export.verify_package(zip_response.body)
    body = package["body"]
    assert rebuilt.record_to_report == expected_links
    assert body["identities"]["decisions"] == history
    assert rebuilt.executions["ex-ok"]["requests"] == [
        {
            "method": "GET",
            "endpoint": "https://openalex.test/q",
            "params": {"search": "aspirin", "per_page": 25},
        }
    ]
    assert (
        rebuilt.executions["ex-ok"]["limit"],
        rebuilt.executions["ex-to"]["limit"],
    ) == (25, 25)
    assert [f["execution_id"] for f in body["failures"]] == ["ex-to"]
    assert body["failures"][0]["error_type"] == "TimeoutError"
    assert {k: body["counts"]["tables"][k] for k in rows} == rows
    legacy_entry = next(i for i in body["imports"] if i["id"] == str(legacy.id))
    assert legacy_entry["declared"]["query_text"] == "not_declared"
    assert body["coverage"]["exhaustive"] is False

    async with identity_factory() as db:
        assert await _snapshot(db) == before
    async with identity_factory() as db:
        again = await export_corpus_route(
            collection_id, export_format="json", current_user=user, db=db  # type: ignore[arg-type]
        )
    assert json.loads(again.body)["body_sha256"] == package["body_sha256"]


def _upload(data: bytes = PARTIAL) -> UploadFile:
    return UploadFile(file=io.BytesIO(data), filename="partial.ris")


async def _route_import(factory: Factory, collection_id: UUID, user_id: UUID) -> int:
    async with factory() as db:
        response = Response()
        await import_search_results_route(
            collection_id,
            response,
            file=_upload(),
            import_format="ris",
            declaration=DECLARATION.model_dump_json(),
            current_user=SimpleNamespace(id=user_id),  # type: ignore[arg-type]
            db=db,
        )
        return int(response.status_code)


async def _route_export(factory: Factory, collection_id: UUID, user_id: UUID) -> int:
    async with factory() as db:
        response = await export_corpus_route(
            collection_id,
            export_format="json",
            current_user=SimpleNamespace(id=user_id),  # type: ignore[arg-type]
            db=db,
        )
        return int(response.status_code)


async def _route_read(
    factory: Factory, kind: str, collection_id: UUID, user_id: UUID, receipt_id: UUID
) -> int:
    user: Any = SimpleNamespace(id=user_id)
    async with factory() as db:
        if kind == "list":
            await list_imports_route(collection_id, current_user=user, db=db)
        elif kind == "get":
            await get_import_route(collection_id, receipt_id, current_user=user, db=db)
        else:
            await corpus_coverage_route(
                collection_id, CoverageRequest(known=[]), current_user=user, db=db
            )
    return 200


async def _route_chase(factory: Factory, collection_id: UUID, user_id: UUID) -> int:
    async with factory() as db:
        response = Response()
        await chase_citations_route(
            collection_id,
            CitationChaseRequest(
                seed_report_id=uuid4(),
                direction="backward",
                max_results=5,
                idempotency_key="tenancy",
            ),
            response,
            current_user=SimpleNamespace(id=user_id),  # type: ignore[arg-type]
            db=db,
        )
        return int(response.status_code)


async def _status(call: Any) -> int:
    try:
        return cast(int, await call)
    except HTTPException as exc:
        return int(exc.status_code)


@pytest.mark.asyncio
async def test_tenancy(identity_factory: Factory) -> None:
    ids = await _seed(identity_factory)
    collection = ids["collection"]
    receipt, _ = await _import(identity_factory, ids)

    assert await _status(_route_import(identity_factory, collection, ids["F"])) == 404
    assert await _status(_route_export(identity_factory, collection, ids["F"])) == 404
    assert await _status(_route_import(identity_factory, collection, ids["V"])) == 404
    assert await _status(_route_export(identity_factory, collection, ids["V"])) == 200
    # Every other corpus route: reads need VIEW (V yes, F no); chase needs EDIT.
    for user, read_status, chase_status in (("F", 404, 404), ("V", 200, 404)):
        for read in ("list", "get", "coverage"):
            assert (
                await _status(
                    _route_read(
                        identity_factory, read, collection, ids[user], receipt.id
                    )
                )
                == read_status
            ), (user, read)
        assert (
            await _status(_route_chase(identity_factory, collection, ids[user]))
            == chase_status
        ), user

    # A foreign project's receipt id is indistinguishable from a missing one.
    other = await _seed(identity_factory)
    async with identity_factory() as db:
        context = await resolve_project(db, other["collection"], other["O"])
        with pytest.raises(HTTPException) as foreign:
            await corpus_service.get_receipt(
                db,
                collection_id=cast(UUID, context.collection.id),
                receipt_id=receipt.id,
            )
    assert foreign.value.status_code == 404

    async with identity_factory() as db:
        await db.execute(
            update(WorkspaceMember)
            .where(WorkspaceMember.user_id == ids["V"])
            .values(is_deleted=True)
        )
        await db.commit()
    assert await _status(_route_export(identity_factory, collection, ids["V"])) == 404

    async with identity_factory() as db:
        await db.execute(
            text("UPDATE collections SET research_status='archived' WHERE id=:id"),
            {"id": collection},
        )
        await db.commit()
    assert await _status(_route_import(identity_factory, collection, ids["O"])) == 409
    assert await _status(_route_export(identity_factory, collection, ids["O"])) == 200

    async with identity_factory() as db:
        await db.execute(
            update(Workspace)
            .where(Workspace.id == ids["workspace"])
            .values(is_deleted=True)
        )
        await db.commit()
    assert await _status(_route_export(identity_factory, collection, ids["O"])) == 404
