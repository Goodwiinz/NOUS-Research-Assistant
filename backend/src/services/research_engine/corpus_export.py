"""Versioned, restriction-filtered, reconstructable corpus export (GOO-300).

``build_package`` and ``coverage`` only read. ``verify_package`` is pure: it
rebuilds record -> final report, the literal provider requests/limits/failures
per execution, and re-runs the identity ledger's transition rules over the
packaged decisions, without a database.
"""

import asyncio
import hashlib
import io
import json
import zipfile
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Literal, cast
from uuid import UUID

from fastapi import HTTPException
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.models.research_blueprint import ResearchBlueprint
from src.models.research_decision import ResearchDecisionEvent
from src.models.research_import import ResearchImportReceipt, ResearchImportRecord
from src.models.research_protocol import ResearchProtocolVersion
from src.models.research_report import (
    ResearchReport,
    ResearchReportIdentifier,
    ResearchReportObservation,
    ResearchStudy,
)
from src.models.research_run import ResearchRun
from src.models.research_source import ResearchSource
from src.models.research_step import ResearchStep
from src.schemas.research_engine import COVERAGE_STATEMENT, CoverageResponse
from src.services.research_decisions.ledger import (
    DecisionReplayError,
    _validate_identity_transitions,
)
from src.services.research_engine import identity_service
from src.services.research_engine.connectors.registry import CONNECTOR_CAPABILITIES
from src.services.research_engine.corpus_service import (
    RESTRICTED_PARSED_FIELDS,
    visible_parsed,
)
from src.services.research_engine.project_access import ProjectContext
from src.services.research_engine.report_identity import report_identifiers

PACKAGE_SCHEMA = "nous.academic.corpus-export.v1"
MAX_EXPORT_RECORDS = 50_000  # over -> 413 export_too_large, never truncated
# Exported free text (allowed raw records + source abstracts); over -> 413.
MAX_EXPORT_BYTES = 256 * 1024 * 1024
# verify_package refuses a corpus.json larger than this (zip-bomb guard).
MAX_PACKAGE_BYTES = 1024 * 1024 * 1024
_JOURNAL_KEY = "_search_receipts_v1"
_NOT_DECLARED = "not_declared"
_NO_DATE_FILTER = "no date filter (connector capability date_filter=false)"

# ponytail: static table; per-source licence metadata when a provider exposes it.
# Provider abstracts leave only for CC0 metadata sources; full text never leaves;
# workspace documents leave as id/title only; restricted imports keep raw text,
# abstracts and tag fields in the database.
_CC0_ABSTRACT_PROVIDERS = frozenset({"openalex", "arxiv"})
_RAG_STORE_FIELDS = ["abstract", "authors", "content_hash", "external_id", "metadata"]
_RESTRICTED_RECORD_FIELDS = ["records[].raw"] + [
    f"records[].parsed.{field}" for field in RESTRICTED_PARSED_FIELDS
]
_CAPABILITIES = {
    name: capability
    for capability in CONNECTOR_CAPABILITIES
    for name in (capability.connector_id, *capability.aliases)
}


class CorpusPackageError(ValueError):
    """A package cannot be verified; nothing about it should be trusted."""


@dataclass(frozen=True)
class Reconstruction:
    body_sha256: str
    record_to_report: dict[str, str]
    executions: dict[str, dict[str, Any]]
    rejected: dict[str, str]


def _json(value: Any) -> Any:
    return json.loads(json.dumps(value, default=str))


def _canonical(body: Any) -> bytes:
    return json.dumps(
        body, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")


def seal(body: dict[str, Any], exported_at: str) -> dict[str, Any]:
    body = _json(body)
    return {
        "schema": PACKAGE_SCHEMA,
        "exported_at": exported_at,
        "body_sha256": hashlib.sha256(_canonical(body)).hexdigest(),
        "body": body,
    }


def _row(obj: Any, *columns: str) -> dict[str, Any]:
    return {column: getattr(obj, column) for column in columns}


# --- pure helpers over the package body ---------------------------------------


def _executions(
    searches: list[dict[str, Any]], imports: list[dict[str, Any]]
) -> dict[str, dict[str, Any]]:
    """Requests, limit, completion and failure per provider execution / chase."""
    out: dict[str, dict[str, Any]] = {}
    for search in searches:
        journal = search.get("executions") or {}
        by_execution: dict[str, dict[str, Any]] = {}
        for step in search.get("search_steps") or []:
            providers = (step.get("coverage") or {}).get("providers") or {}
            for provider, receipt in providers.items():
                key = str(
                    receipt.get("execution_id")
                    or f"{search['run_id']}:{step['step_id']}:{provider}"
                )
                by_execution[key] = {**receipt, "provider": provider}
        for key in sorted(set(journal) | set(by_execution)):
            execution = journal.get(key) or {}
            receipt = by_execution.get(key, {})
            attempts = list((execution.get("provider_attempts") or {}).values())
            final = attempts[-1] if attempts else {}
            pages = sorted(
                (execution.get("pages") or {}).values(),
                key=lambda page: int(page.get("page_index", 0)),
            )
            requests = [
                attempt["page"].get("request")
                for page in pages
                for attempt in page.get("attempts") or []
            ] or [page.get("request") for page in receipt.get("pages") or []]
            out[key] = {
                "kind": "provider_search",
                "run_id": search["run_id"],
                "provider": execution.get("provider") or receipt.get("provider"),
                "status": execution.get("status") or receipt.get("status"),
                "completion": receipt.get("completion") or final.get("completion"),
                "limit": receipt.get("limit", final.get("requested_limit")),
                "returned": receipt.get("returned", final.get("returned_count")),
                "error_type": receipt.get("error_type") or final.get("error_type"),
                "started_at": final.get("started_at") or receipt.get("started_at"),
                "requests": requests,
            }
    for receipt in imports:
        if receipt["kind"] != "citation_chase":
            continue
        observed = receipt["observed"]
        trace = observed.get("trace") or {}
        out[str(trace.get("execution_id") or receipt["id"])] = {
            "kind": "citation_chase",
            "receipt_id": receipt["id"],
            "provider": observed.get("provider"),
            "status": trace.get("status"),
            "completion": observed.get("completion"),
            "limit": trace.get("requested_limit"),
            "returned": trace.get("returned_count"),
            "error_type": observed.get("error_type"),
            "started_at": observed.get("started_at"),
            "requests": [page.get("request") for page in trace.get("pages") or []],
        }
    return out


def _date_window(provider: str | None) -> str:
    capability = _CAPABILITIES.get(provider or "")
    if capability is None or not capability.date_filter:
        return _NO_DATE_FILTER
    return "see search strategy"


def _coverage(
    index: dict[tuple[str, str], str],
    known: list[dict[str, str]],
    executions: dict[str, dict[str, Any]],
    imports: list[dict[str, Any]],
    used_connectors: set[str],
    requirement: Any,
) -> CoverageResponse:
    found: list[dict[str, Any]] = []
    missing: list[dict[str, str]] = []
    for item in known:
        for kind, value in item.items():
            ids = report_identifiers({kind: value})
            normalized = ids.get(kind, value)
            hit = next((index[(k, v)] for k, v in ids.items() if (k, v) in index), None)
            if hit is None:
                missing.append({"kind": kind, "value": normalized})
            else:
                found.append({"kind": kind, "value": normalized, "report_id": hit})
    total = len(found) + len(missing)
    searched: list[dict[str, Any]] = []
    for execution_id, execution in executions.items():
        if execution["kind"] == "provider_search":
            searched.append(
                {
                    "type": "provider",
                    "provider": execution["provider"],
                    "run_id": execution["run_id"],
                    "execution_id": execution_id,
                    "executed_at": execution["started_at"],
                    "status": execution["status"],
                    "completion": execution["completion"],
                    "date_window": _date_window(execution["provider"]),
                }
            )
    performed: set[str] = set()
    for receipt in imports:
        declared = receipt["declared"]
        if receipt["kind"] == "file_import":
            search_date = declared.get("search_date") or _NOT_DECLARED
            searched.append(
                {
                    "type": "import",
                    "receipt_id": receipt["id"],
                    "database": declared.get("database"),
                    "search_date": search_date,
                    "date_window": search_date,
                }
            )
        else:
            performed.add(declared["direction"])
            searched.append(
                {
                    "type": "citation_chase",
                    "receipt_id": receipt["id"],
                    "provider": receipt["observed"].get("provider"),
                    "direction": declared["direction"],
                    "executed_at": receipt["observed"].get("started_at"),
                    "completion": receipt["observed"].get("completion"),
                }
            )
    requirement = requirement if isinstance(requirement, dict) else {}
    directions = [str(d) for d in requirement.get("directions") or []]
    used = {
        _CAPABILITIES[c].connector_id for c in used_connectors if c in _CAPABILITIES
    }
    return CoverageResponse(
        found=found,
        missing=missing,
        recall=(len(found) / total) if total else None,
        searched=searched,
        not_searched=sorted(
            c.connector_id for c in CONNECTOR_CAPABILITIES if c.connector_id not in used
        ),
        citation_chasing={
            "required": bool(requirement.get("required")),
            "directions": directions,
            "performed": sorted(performed),
            "missing_directions": [d for d in directions if d not in performed],
        },
    )


# --- database reads ------------------------------------------------------------


def _too_large() -> HTTPException:
    return HTTPException(
        status_code=413,
        detail={
            "code": "export_too_large",
            "message": "This project is too large to export as one package.",
        },
    )


async def _runs(db: AsyncSession, context: ProjectContext) -> list[Any]:
    if context.engine is None:
        return []
    return list(
        (
            await db.execute(
                select(ResearchRun)
                .join(
                    ResearchBlueprint, ResearchBlueprint.id == ResearchRun.blueprint_id
                )
                .where(
                    ResearchBlueprint.project_id == context.engine.id,
                    ResearchRun.is_deleted.is_(False),
                )
                .order_by(ResearchRun.created_at, ResearchRun.id)
            )
        )
        .scalars()
        .all()
    )


async def _searches(db: AsyncSession, runs: list[Any]) -> list[dict[str, Any]]:
    steps = (
        (
            await db.execute(
                select(ResearchStep)
                .where(
                    ResearchStep.run_id.in_([run.id for run in runs]),
                    ResearchStep.step_type == "search",
                )
                .order_by(ResearchStep.step_index, ResearchStep.id)
            )
        )
        .scalars()
        .all()
    )
    searches = []
    for run in runs:
        journal = (
            cast(dict[str, Any], run.reproducibility_manifest or {}).get(_JOURNAL_KEY)
            or {}
        )
        searches.append(
            {
                "run_id": run.id,
                "run_status": run.status,
                "conformance_status": run.conformance_status,
                "protocol_version_id": run.protocol_version_id,
                "effective_plan_hash": run.effective_plan_hash,
                "strategies": journal.get("strategies") or {},
                "executions": journal.get("executions") or {},
                "search_steps": [
                    {
                        "step_id": step.id,
                        "step_index": step.step_index,
                        "coverage": cast(dict[str, Any], step.output or {}).get(
                            "coverage"
                        ),
                    }
                    for step in steps
                    if step.run_id == run.id
                ],
            }
        )
    return cast(list[dict[str, Any]], _json(searches))


def _source(row: Any, omissions: list[dict[str, Any]]) -> dict[str, Any]:
    base = _row(row, "id", "run_id", "connector_type", "title")
    if row.connector_type == "rag_store":
        omissions.append(
            {
                "scope": "source",
                "id": str(row.id),
                "fields": _RAG_STORE_FIELDS + ["url"],
                "reason": "workspace_document",
            }
        )
        return base
    withheld: set[str] = set()
    metadata = deepcopy(cast(dict[str, Any], row.metadata_ or {}))
    provenance = []
    for entry in metadata.get("provenance") or []:
        entry = dict(entry)
        if entry.pop("full_text", None):
            withheld.add("metadata.provenance[].full_text")
        if entry.get("connector_type") not in _CC0_ABSTRACT_PROVIDERS and entry.get(
            "abstract"
        ):
            entry.pop("abstract")
            withheld.add("metadata.provenance[].abstract")
        provenance.append(entry)
    if "provenance" in metadata:
        metadata["provenance"] = provenance
    abstract = row.abstract if row.connector_type in _CC0_ABSTRACT_PROVIDERS else None
    if row.abstract and abstract is None:
        withheld.add("abstract")
    if withheld:
        omissions.append(
            {
                "scope": "source",
                "id": str(row.id),
                "fields": sorted(withheld),
                "reason": "not_redistributable",
            }
        )
    return {
        **base,
        **_row(row, "external_id", "authors", "url", "content_hash"),
        "abstract": abstract,
        "metadata": metadata,
    }


async def _imports(
    db: AsyncSession,
    collection_id: UUID,
    omissions: list[dict[str, Any]] | None,
    *,
    with_records: bool = True,
) -> list[dict[str, Any]]:
    receipts = (
        (
            await db.execute(
                select(ResearchImportReceipt)
                .where(ResearchImportReceipt.collection_id == collection_id)
                .order_by(ResearchImportReceipt.created_at, ResearchImportReceipt.id)
            )
        )
        .scalars()
        .all()
    )
    records = (
        (
            await db.execute(
                select(ResearchImportRecord)
                .where(ResearchImportRecord.collection_id == collection_id)
                .order_by(ResearchImportRecord.record_index, ResearchImportRecord.id)
            )
        )
        .scalars()
        .all()
        if with_records
        else []
    )
    out = []
    for receipt in receipts:
        rid = str(receipt.id)
        declared = dict(cast(dict[str, Any], receipt.declared))
        allowed = declared.get("redistribution") == "allowed"
        if receipt.kind == "file_import":
            missing = [k for k in ("query_text", "search_date") if not declared.get(k)]
            for key in missing:
                declared[key] = _NOT_DECLARED
            if missing and omissions is not None:
                omissions.append(
                    {
                        "scope": "import_receipt",
                        "id": rid,
                        "fields": missing,
                        "reason": _NOT_DECLARED,
                    }
                )
        if not allowed and omissions is not None:
            omissions.append(
                {
                    "scope": "import_receipt",
                    "id": rid,
                    "fields": _RESTRICTED_RECORD_FIELDS,
                    "reason": "redistribution_restricted",
                }
            )
        out.append(
            {
                **_row(
                    receipt,
                    "id",
                    "kind",
                    "version",
                    "previous_receipt_id",
                    "observed",
                    "parsed_count",
                    "accepted_count",
                    "rejected_count",
                    "actor_user_id",
                    "created_at",
                ),
                "declared": declared,
                "records": [
                    {
                        **_row(
                            record,
                            "id",
                            "record_index",
                            "status",
                            "rejection_reason",
                            "report_id",
                            "match_method",
                            "evidence",
                        ),
                        "parsed": visible_parsed(
                            cast(dict[str, Any], record.parsed), allowed
                        ),
                        "raw": record.raw if allowed else None,
                    }
                    for record in records
                    if record.receipt_id == receipt.id
                ],
            }
        )
    return cast(list[dict[str, Any]], _json(out))


async def _all(db: AsyncSession, query: Any) -> list[Any]:
    return list((await db.execute(query)).scalars().all())


async def _identifier_index(
    db: AsyncSession, collection_id: UUID
) -> dict[tuple[str, str], str]:
    rows = (
        await db.execute(
            select(
                ResearchReportIdentifier.kind,
                ResearchReportIdentifier.value,
                ResearchReportIdentifier.report_id,
            ).where(ResearchReportIdentifier.collection_id == collection_id)
        )
    ).all()
    return {(kind, value): str(report_id) for kind, value, report_id in rows}


async def _protocol(db: AsyncSession, collection_id: UUID) -> dict[str, Any]:
    version_id = await identity_service.current_protocol_version_id(db, collection_id)
    version = (
        await db.get(ResearchProtocolVersion, UUID(version_id)) if version_id else None
    )
    snapshot = cast(dict[str, Any], version.snapshot or {}) if version else {}
    return {
        "current_approved_version_id": version_id,
        "content_hash": version.content_hash if version else None,
        "citation_chasing": (snapshot.get("sources_search") or {}).get(
            "citation_chasing"
        ),
    }


async def _used_connectors(db: AsyncSession, runs: list[Any]) -> set[str]:
    return set(
        (
            await db.execute(
                select(ResearchSource.connector_type)
                .where(ResearchSource.run_id.in_([run.id for run in runs]))
                .distinct()
            )
        )
        .scalars()
        .all()
    )


async def _scalar(db: AsyncSession, query: Any) -> int:
    return int((await db.execute(query)).scalar_one() or 0)


async def _check_size(
    db: AsyncSession, run_ids: list[Any], collection_id: UUID, *, bytes_too: bool
) -> None:
    """413 before loading anything large: row count, then exported text bytes."""
    rows = await _scalar(
        db,
        select(func.count())
        .select_from(ResearchSource)
        .where(ResearchSource.run_id.in_(run_ids)),
    ) + await _scalar(
        db,
        select(func.count())
        .select_from(ResearchImportRecord)
        .where(ResearchImportRecord.collection_id == collection_id),
    )
    if rows > MAX_EXPORT_RECORDS:
        raise _too_large()
    if not bytes_too:
        return
    allowed = [
        receipt_id
        for receipt_id, declared in (
            await db.execute(
                select(ResearchImportReceipt.id, ResearchImportReceipt.declared).where(
                    ResearchImportReceipt.collection_id == collection_id
                )
            )
        ).all()
        if (declared or {}).get("redistribution") == "allowed"
    ]
    size = await _scalar(
        db,
        select(func.sum(func.octet_length(ResearchImportRecord.raw))).where(
            ResearchImportRecord.receipt_id.in_(allowed)
        ),
    ) + await _scalar(
        db,
        select(func.sum(func.octet_length(ResearchSource.abstract))).where(
            ResearchSource.run_id.in_(run_ids)
        ),
    )
    if size > MAX_EXPORT_BYTES:
        raise _too_large()


async def coverage(
    db: AsyncSession, context: ProjectContext, known: list[dict[str, str]]
) -> CoverageResponse:
    """Read-only: what was searched, imported and chased; never exhaustive.

    Loads receipts only (no record raw/parsed/evidence) and applies the same
    row-count guard as the export.
    """
    collection_id = cast(UUID, context.collection.id)
    runs = await _runs(db, context)
    await _check_size(db, [run.id for run in runs], collection_id, bytes_too=False)
    imports = await _imports(db, collection_id, None, with_records=False)
    executions = _executions(await _searches(db, runs), imports)
    return _coverage(
        await _identifier_index(db, collection_id),
        known,
        executions,
        imports,
        await _used_connectors(db, runs)
        | {str(e["provider"]) for e in executions.values()},
        (await _protocol(db, collection_id))["citation_chasing"],
    )


async def build_package(db: AsyncSession, context: ProjectContext) -> dict[str, Any]:
    """Read-only, deterministic project corpus package (performs no writes)."""
    collection_id = cast(UUID, context.collection.id)
    runs = await _runs(db, context)
    run_ids = [run.id for run in runs]
    await _check_size(db, run_ids, collection_id, bytes_too=True)

    omissions: list[dict[str, Any]] = []
    searches = await _searches(db, runs)
    sources = [
        _source(row, omissions)
        for row in await _all(
            db,
            select(ResearchSource)
            .where(ResearchSource.run_id.in_(run_ids))
            .order_by(ResearchSource.created_at, ResearchSource.id),
        )
    ]
    imports = await _imports(db, collection_id, omissions)
    reports = await _all(
        db,
        select(ResearchReport)
        .where(ResearchReport.collection_id == collection_id)
        .order_by(ResearchReport.created_at, ResearchReport.id),
    )
    identifiers = await _all(
        db,
        select(ResearchReportIdentifier)
        .where(ResearchReportIdentifier.collection_id == collection_id)
        .order_by(ResearchReportIdentifier.kind, ResearchReportIdentifier.value),
    )
    observations = await _all(
        db,
        select(ResearchReportObservation)
        .where(ResearchReportObservation.collection_id == collection_id)
        .order_by(ResearchReportObservation.created_at, ResearchReportObservation.id),
    )
    studies = await _all(
        db,
        select(ResearchStudy)
        .where(ResearchStudy.collection_id == collection_id)
        .order_by(ResearchStudy.created_at, ResearchStudy.id),
    )
    decisions = [
        event.model_dump(mode="json")
        for event in await identity_service.history(db, collection_id=collection_id)
    ]
    protocol = await _protocol(db, collection_id)
    executions = _executions(searches, imports)
    body = {
        "project": {
            "collection_id": collection_id,
            "name": context.collection.name,
        },
        "protocol": protocol,
        "searches": searches,
        "sources": sources,
        "imports": imports,
        "identities": {
            "reports": [
                _row(
                    r,
                    "id",
                    "title_snapshot",
                    "merged_into_report_id",
                    "study_id",
                    "study_link_status",
                    "study_link_rationale",
                    "created_at",
                )
                for r in reports
            ],
            "identifiers": [_row(i, "report_id", "kind", "value") for i in identifiers],
            "observations": [
                _row(o, "report_id", "source_id", "match_method", "evidence")
                for o in observations
            ],
            "studies": [_row(s, "id", "label", "created_at") for s in studies],
            "decisions": decisions,
        },
        "counts": {
            "tables": {
                "runs": len(runs),
                "sources": len(sources),
                "import_receipts": len(imports),
                "import_records": sum(len(i["records"]) for i in imports),
                "reports": len(reports),
                "identifiers": len(identifiers),
                "observations": len(observations),
                "studies": len(studies),
                "decisions": len(decisions),
            },
            "receipts": {
                i["id"]: {
                    "parsed": i["parsed_count"],
                    "accepted": i["accepted_count"],
                    "rejected": i["rejected_count"],
                }
                for i in imports
            },
            "providers": {
                key: {
                    "provider": e["provider"],
                    "returned": e["returned"],
                    "limit": e["limit"],
                }
                for key, e in executions.items()
            },
        },
        "omissions": omissions,
        "failures": [
            {"execution_id": key, **{k: v for k, v in e.items() if k != "requests"}}
            for key, e in executions.items()
            if e["status"] != "ok"
        ],
        "coverage": _coverage(
            await _identifier_index(db, collection_id),
            [],
            executions,
            imports,
            await _used_connectors(db, runs)
            | {str(e["provider"]) for e in executions.values()},
            protocol["citation_chasing"],
        ).model_dump(mode="json"),
    }
    # Canonical JSON + hashing of a large body must not block the event loop.
    return await asyncio.to_thread(seal, body, datetime.now(timezone.utc).isoformat())


def render(
    package: dict[str, Any], fmt: Literal["json", "zip"]
) -> tuple[bytes, str, str]:
    """(bytes, media type, filename). ponytail: RIS/CSV renderings when needed."""
    data = json.dumps(package, sort_keys=True, indent=2, ensure_ascii=False).encode()
    name = f"corpus-{package['body']['project']['collection_id']}"
    if fmt == "json":
        return data, "application/json", f"{name}.json"
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, mode="w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("corpus.json", data)
        zf.writestr(
            "README.txt",
            f"{PACKAGE_SCHEMA}\n\n{COVERAGE_STATEMENT}\n\n"
            "corpus.json holds the package; body_sha256 is the SHA-256 of its "
            "body as canonical JSON (sorted keys, no whitespace). It proves "
            "integrity, not authenticity: anyone can recompute it.\n",
        )
    return buffer.getvalue(), "application/zip", f"{name}.zip"


def verify_package(data: bytes) -> Reconstruction:
    """Check the body hash and rebuild identity and search evidence (pure).

    Any unreadable, oversized or malformed package raises
    ``CorpusPackageError``; nothing else escapes.
    """
    try:
        if data[:2] == b"PK":
            with zipfile.ZipFile(io.BytesIO(data)) as archive:
                if archive.getinfo("corpus.json").file_size > MAX_PACKAGE_BYTES:
                    raise CorpusPackageError("corpus.json is too large to verify")
                data = archive.read("corpus.json")
        elif len(data) > MAX_PACKAGE_BYTES:
            raise CorpusPackageError("package is too large to verify")
        package = json.loads(data)
    except (zipfile.BadZipFile, KeyError, ValueError) as exc:
        if isinstance(exc, CorpusPackageError):
            raise
        raise CorpusPackageError("package is not a readable corpus export") from exc
    if not isinstance(package, dict) or package.get("schema") != PACKAGE_SCHEMA:
        raise CorpusPackageError("unsupported package schema")
    body = package.get("body")
    digest = hashlib.sha256(_canonical(body)).hexdigest()
    if digest != package.get("body_sha256"):
        raise CorpusPackageError("body_sha256 does not match the package body")
    try:
        return _reconstruct(cast(dict[str, Any], body), digest)
    except CorpusPackageError:
        raise
    except (KeyError, TypeError, AttributeError, ValueError) as exc:
        raise CorpusPackageError("package body has an unexpected shape") from exc


def _reconstruct(body: dict[str, Any], digest: str) -> Reconstruction:
    identities = body["identities"]
    merged_into = {r["id"]: r["merged_into_report_id"] for r in identities["reports"]}

    def final(report_id: str) -> str:
        seen = set()
        while merged_into.get(report_id) and report_id not in seen:
            seen.add(report_id)
            report_id = merged_into[report_id]
        return report_id

    record_to_report = {
        o["source_id"]: final(o["report_id"]) for o in identities["observations"]
    }
    rejected: dict[str, str] = {}
    for receipt in body["imports"]:
        for record in receipt["records"]:
            if record["report_id"]:
                record_to_report[record["id"]] = final(record["report_id"])
            if record["status"] == "rejected":
                rejected[record["id"]] = record["rejection_reason"]
    try:
        _validate_identity_transitions(
            [
                ResearchDecisionEvent(event_type=d["event_type"], payload=d["payload"])
                for d in identities["decisions"]
            ],
            UUID(body["project"]["collection_id"]),
        )
    except DecisionReplayError as exc:
        raise CorpusPackageError("identity decisions do not replay") from exc
    return Reconstruction(
        body_sha256=digest,
        record_to_report=record_to_report,
        executions=_executions(body["searches"], body["imports"]),
        rejected=rejected,
    )
