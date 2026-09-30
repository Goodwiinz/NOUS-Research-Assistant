"""Versioned, restriction-filtered, reconstructable corpus export (GOO-300).

SQLite unit coverage; the persisted-rows round trip on PostgreSQL lives in
``tests/integration/test_search_import_postgres.py``.
"""

import io
import json
import zipfile
from collections.abc import AsyncIterator
from datetime import datetime
from types import SimpleNamespace
from typing import Any, cast
from uuid import UUID, uuid4

import pytest
from fastapi import HTTPException
from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from src.models.base import Base
from src.models.research_blueprint import ResearchBlueprint
from src.models.research_decision import ResearchDecisionEvent, ResearchDecisionStream
from src.models.research_import import ResearchImportReceipt, ResearchImportRecord
from src.models.research_project_role import ResearchProjectRole
from src.models.research_protocol import ResearchProtocol, ResearchProtocolVersion
from src.models.research_report import (
    ResearchReport,
    ResearchReportIdentifier,
    ResearchReportObservation,
    ResearchStudy,
)
from src.models.research_run import ResearchRun
from src.models.research_source import ResearchSource
from src.models.research_step import ResearchStep
from src.schemas.research_engine import ImportDeclaration, ReportMergeRequest
from src.services.research_engine import corpus_export, corpus_service, identity_service
from src.services.research_engine.project_access import ProjectContext

RESTRICTED_RAW = "RESTRICTED-RAW-SENTINEL"
RESTRICTED_ABSTRACT = "RESTRICTED-ABSTRACT-SENTINEL"
RAG_FULL_TEXT = "RAG-FULLTEXT-SENTINEL"
PUBLIC_FULL_TEXT = "OPENALEX-FULLTEXT-SENTINEL"
PUBMED_ABSTRACT = "PUBMED-ABSTRACT-SENTINEL"
SENTINELS = (
    RESTRICTED_RAW,
    RESTRICTED_ABSTRACT,
    RAG_FULL_TEXT,
    PUBLIC_FULL_TEXT,
    PUBMED_ABSTRACT,
)

RIS = (
    "TY  - JOUR\nTI  - Imported one\nDO  - 10.1000/imp1\n"
    f"AB  - {RESTRICTED_ABSTRACT}\nN1  - {RESTRICTED_RAW}\nER  - \n\n"
    "TY  - JOUR\nTI  - Imported two\nDO  - 10.1000/imp2\nER  - \n\n"
    "TY  - JOUR\nAU  - Untitled\nER  - \n"
).encode()


@pytest.fixture
async def db() -> AsyncIterator[AsyncSession]:
    tables = [
        model.__table__  # type: ignore[attr-defined]
        for model in (
            ResearchProtocol,
            ResearchProtocolVersion,
            ResearchDecisionStream,
            ResearchDecisionEvent,
            ResearchBlueprint,
            ResearchRun,
            ResearchStep,
            ResearchSource,
            ResearchStudy,
            ResearchReport,
            ResearchReportIdentifier,
            ResearchReportObservation,
            ResearchImportReceipt,
            ResearchImportRecord,
        )
    ]
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    event.listen(
        engine.sync_engine,
        "connect",
        lambda conn, _record: conn.create_function(
            "now", 0, lambda: datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S.%f")
        ),
    )
    async with engine.begin() as connection:
        await connection.run_sync(
            lambda sync: Base.metadata.create_all(sync, tables=tables)
        )
    async with async_sessionmaker(engine, expire_on_commit=False)() as session:
        yield session
    await engine.dispose()


def _journal(ok: str, timed_out: str) -> dict[str, Any]:
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
                                    "endpoint": f"https://{provider}.test/search",
                                    "params": {"search": "aspirin"},
                                }
                            },
                        }
                    ],
                }
            },
            "provider_attempts": {
                "a1": {
                    "requested_limit": 50,
                    "completion": "exhausted" if status == "ok" else "failed",
                    "error_type": None if status == "ok" else "TimeoutError",
                    "returned_count": 1 if status == "ok" else 0,
                    "started_at": "2026-09-29T10:00:00+00:00",
                }
            },
        }

    return {
        "schema_version": 1,
        "strategies": {"s1": {"strategy_version": "s1", "query": "aspirin"}},
        "executions": {
            ok: execution(ok, "openalex", "ok"),
            timed_out: execution(timed_out, "pubmed", "timed_out"),
        },
    }


async def _seed(db: AsyncSession) -> tuple[ProjectContext, dict[str, Any]]:
    collection_id, engine_id = uuid4(), uuid4()
    context = cast(
        ProjectContext,
        SimpleNamespace(
            collection=SimpleNamespace(id=collection_id, name="Aspirin review"),
            engine=SimpleNamespace(id=engine_id),
            effective_roles=frozenset({ResearchProjectRole.ADJUDICATOR}),
        ),
    )
    blueprint = ResearchBlueprint(id=uuid4(), project_id=engine_id, name="b")
    db.add(blueprint)
    await db.flush()
    run = ResearchRun(
        id=uuid4(),
        blueprint_id=blueprint.id,
        blueprint_version=1,
        status="completed",
        reproducibility_manifest={"_search_receipts_v1": _journal("ex-ok", "ex-to")},
    )
    db.add(run)
    await db.flush()
    db.add(
        ResearchStep(
            run_id=run.id,
            step_index=0,
            step_type="search",
            output={
                "coverage": {
                    "providers": {
                        "openalex": {
                            "execution_id": "ex-ok",
                            "status": "ok",
                            "completion": "exhausted",
                            "returned": 1,
                            "limit": 50,
                        },
                        "pubmed": {
                            "execution_id": "ex-to",
                            "status": "timed_out",
                            "completion": "failed",
                            "returned": 0,
                            "limit": 50,
                            "error_type": "TimeoutError",
                        },
                    },
                    "partial": True,
                    "exhaustive": False,
                }
            },
        )
    )

    def provenance(connector: str, **extra: Any) -> dict[str, Any]:
        return {"connector_type": connector, "title": "t", "full_text": None, **extra}

    db.add_all(
        [
            ResearchSource(
                run_id=run.id,
                connector_type="openalex",
                title="Open paper",
                abstract="open abstract",
                metadata_={
                    "identifiers": {"doi": "10.1000/imp2"},
                    "provenance": [
                        provenance("openalex", full_text=PUBLIC_FULL_TEXT),
                        provenance("pubmed", abstract=PUBMED_ABSTRACT),
                    ],
                },
            ),
            ResearchSource(
                run_id=run.id,
                connector_type="pubmed",
                title="Pubmed paper",
                abstract=PUBMED_ABSTRACT,
                metadata_={"identifiers": {"pmid": "1"}},
            ),
            ResearchSource(
                run_id=run.id,
                connector_type="rag_store",
                title="Workspace doc",
                abstract="local summary",
                metadata_={
                    "provenance": [provenance("rag_store", full_text=RAG_FULL_TEXT)]
                },
            ),
        ]
    )
    await db.flush()
    restricted, _ = await corpus_service.import_file(
        db,
        context,
        uuid4(),
        ImportDeclaration(database="Embase", query_text="aspirin"),
        fmt="ris",
        filename="embase.ris",
        data=RIS,
    )
    legacy, _ = await corpus_service.import_file(
        db,
        context,
        uuid4(),
        ImportDeclaration(database="Legacy DB"),
        fmt="csv",
        filename="old.csv",
        data=b"title,doi\nLegacy paper,10.1000/legacy\n",
    )
    return context, {"run": run, "restricted": restricted, "legacy": legacy}


def _unzip(data: bytes) -> dict[str, bytes]:
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        return {name: archive.read(name) for name in archive.namelist()}


@pytest.mark.asyncio
async def test_restricted_raw_and_full_text_never_serialized(
    db: AsyncSession,
) -> None:
    context, _ = await _seed(db)
    package = await corpus_export.build_package(db, context)

    json_bytes, media_type, filename = corpus_export.render(package, "json")
    zip_bytes, zip_type, zip_name = corpus_export.render(package, "zip")
    files = _unzip(zip_bytes)

    assert (media_type, zip_type) == ("application/json", "application/zip")
    assert filename.endswith(".json") and zip_name.endswith(".zip")
    assert set(files) == {"corpus.json", "README.txt"}
    for blob in (json_bytes, files["corpus.json"]):
        for sentinel in SENTINELS:
            assert sentinel.encode() not in blob, sentinel
    assert b"not exhaustive" in files["README.txt"]
    body = package["body"]
    withheld = {(o["scope"], field) for o in body["omissions"] for field in o["fields"]}
    assert {
        ("source", "metadata.provenance[].full_text"),
        ("source", "metadata.provenance[].abstract"),
        ("source", "abstract"),
        ("import_receipt", "records[].raw"),
        ("import_receipt", "records[].parsed.fields"),
    } <= withheld
    rag = next(s for s in body["sources"] if s["connector_type"] == "rag_store")
    assert set(rag) == {"id", "run_id", "connector_type", "title"}
    open_source = next(s for s in body["sources"] if s["connector_type"] == "openalex")
    assert open_source["abstract"] == "open abstract"


@pytest.mark.asyncio
async def test_body_hash_stable_and_verify_reconstructs(db: AsyncSession) -> None:
    context, seeded = await _seed(db)
    first = await corpus_export.build_package(db, context)
    second = await corpus_export.build_package(db, context)
    assert first["body_sha256"] == second["body_sha256"]
    assert first["schema"] == corpus_export.PACKAGE_SCHEMA

    rebuilt = corpus_export.verify_package(corpus_export.render(first, "zip")[0])

    assert rebuilt.body_sha256 == first["body_sha256"]
    assert rebuilt.executions["ex-ok"]["requests"] == [
        {
            "method": "GET",
            "endpoint": "https://openalex.test/search",
            "params": {"search": "aspirin"},
        }
    ]
    assert rebuilt.executions["ex-ok"]["limit"] == 50
    assert rebuilt.executions["ex-to"]["status"] == "timed_out"
    assert rebuilt.executions["ex-to"]["error_type"] == "TimeoutError"
    assert sorted(rebuilt.rejected.values()) == ["missing_title"]
    records = [
        r for i in first["body"]["imports"] for r in i["records"] if r["report_id"]
    ]
    assert {r["id"]: r["report_id"] for r in records}.items() <= (
        rebuilt.record_to_report.items()
    )
    failures = first["body"]["failures"]
    assert [f["execution_id"] for f in failures] == ["ex-to"]
    assert first["body"]["counts"]["receipts"][str(seeded["restricted"].id)] == {
        "parsed": 3,
        "accepted": 2,
        "rejected": 1,
    }


@pytest.mark.asyncio
async def test_verify_follows_merges_and_replays_decisions(db: AsyncSession) -> None:
    context, _ = await _seed(db)
    reports = await identity_service.list_reports(
        db, collection_id=cast(UUID, context.collection.id)
    )
    by_title = {r.title_snapshot: r for r in reports}
    survivor, loser = by_title["Imported one"], by_title["Imported two"]
    await identity_service.merge_reports(
        db,
        context,
        uuid4(),
        ReportMergeRequest(
            surviving_report_id=survivor.id,
            merged_report_ids=[loser.id],
            rationale="same",
            idempotency_key="m1",
        ),
    )
    package = await corpus_export.build_package(db, context)
    # Point the moved record back at the loser, as an old export would have.
    moved = loser.imported_records[0].import_record_id
    for receipt in package["body"]["imports"]:
        for record in receipt["records"]:
            if record["id"] == str(moved):
                record["report_id"] = str(loser.id)
    package = corpus_export.seal(package["body"], package["exported_at"])

    rebuilt = corpus_export.verify_package(corpus_export.render(package, "json")[0])

    assert rebuilt.record_to_report[str(moved)] == str(survivor.id)
    assert [d["event_type"] for d in package["body"]["identities"]["decisions"]] == [
        "identity.report_merged"
    ]


@pytest.mark.asyncio
async def test_verify_rejects_tampered_body(db: AsyncSession) -> None:
    context, _ = await _seed(db)
    package = await corpus_export.build_package(db, context)
    data = json.loads(corpus_export.render(package, "json")[0])
    data["body"]["project"]["name"] = "tampered"

    with pytest.raises(corpus_export.CorpusPackageError, match="body_sha256"):
        corpus_export.verify_package(json.dumps(data).encode())


@pytest.mark.asyncio
async def test_coverage_never_claims_exhaustive_and_names_unsearched_connectors(
    db: AsyncSession,
) -> None:
    context, _ = await _seed(db)
    result = await corpus_export.coverage(
        db,
        context,
        [{"doi": "https://doi.org/10.1000/IMP1"}, {"doi": "10.1000/missing"}],
    )

    assert result.exhaustive is False
    assert "not exhaustive" in result.statement
    assert [f["value"] for f in result.found] == ["10.1000/imp1"]
    assert result.missing == [{"kind": "doi", "value": "10.1000/missing"}]
    assert result.recall == 0.5
    assert result.not_searched == ["arxiv", "crossref", "semantic_scholar"]
    kinds = {entry["type"] for entry in result.searched}
    assert kinds == {"provider", "import"}
    provider = next(e for e in result.searched if e.get("provider") == "openalex")
    assert provider["date_window"] == (
        "no date filter (connector capability date_filter=false)"
    )


@pytest.mark.asyncio
async def test_legacy_import_without_query_reports_not_declared(
    db: AsyncSession,
) -> None:
    context, seeded = await _seed(db)
    body = (await corpus_export.build_package(db, context))["body"]

    legacy = next(i for i in body["imports"] if i["id"] == str(seeded["legacy"].id))
    assert legacy["declared"]["query_text"] == "not_declared"
    assert legacy["declared"]["search_date"] == "not_declared"
    assert {
        "scope": "import_receipt",
        "id": str(seeded["legacy"].id),
        "fields": ["query_text", "search_date"],
        "reason": "not_declared",
    } in body["omissions"]
    searched = await corpus_export.coverage(db, context, [])
    entry = next(
        e for e in searched.searched if e.get("receipt_id") == str(seeded["legacy"].id)
    )
    assert entry["date_window"] == "not_declared"


@pytest.mark.asyncio
async def test_export_too_large_is_413(
    db: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    context, _ = await _seed(db)
    monkeypatch.setattr(corpus_export, "MAX_EXPORT_RECORDS", 3)

    with pytest.raises(HTTPException) as large:
        await corpus_export.build_package(db, context)

    assert large.value.status_code == 413
    assert cast(dict, large.value.detail)["code"] == "export_too_large"


@pytest.mark.asyncio
async def test_project_without_engine_exports_imports_only(db: AsyncSession) -> None:
    context, _ = await _seed(db)
    bare = cast(
        ProjectContext,
        SimpleNamespace(collection=context.collection, engine=None),
    )
    body = (await corpus_export.build_package(db, bare))["body"]
    assert body["searches"] == [] and body["sources"] == []
    assert len(body["imports"]) == 2
