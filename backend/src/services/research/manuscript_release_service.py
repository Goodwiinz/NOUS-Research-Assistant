"""Immutable candidate and verified manuscript releases (GOO-315).

A candidate packages one exact saved draft version with everything needed to
reproduce it: the content hash, the GOO-307 claim versions and assessments,
snapshotted bibliography records (never re-resolved), the applicable method,
protocol and figure provenance, and every check result. A verified release
promotes exactly one candidate: the snapshot is rebuilt from current data and
must hash-equal, the obligations are re-evaluated fresh (the candidate's
stored checks are never trusted), and the package bytes are copied unchanged.

Lock order for both writers: the route's ``resolve_project`` (Workspace SHARE
-> Collection UPDATE, roles reloaded), then this Collection's
``research_manuscript`` stream. Package bytes are written before the row;
a failed transaction deletes them (nothing committed cites them). Claim
support is GOO-307's: it is read from the live ``draft_releases`` row, never
re-checked here. Nothing here talks to the network: packaging never
authorizes an external submission.

GOO-316: the snapshot captures the current statement set (id, hash,
approvals) when one exists; every new candidate also stores an
``anonymized`` package variant whose every member is byte-scanned for every
identity string (a leak fails the build); a statement-bound candidate gets
an automatic venue check, and verified promotion requires ``statements`` and
``venue`` for that exact package hash. ``package_files`` is a list for
GOO-315 rows and ``{identified, anonymized, anonymized_sha256,
anonymized_storage_key}`` for new rows.
"""

import hashlib
import io
import json
import zipfile
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any, Mapping, Sequence, cast
from uuid import UUID, uuid4

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from src.models.draft_release import DraftRelease
from src.models.generated_draft import GeneratedDraft
from src.models.manuscript_release import ManuscriptRelease
from src.models.manuscript_statements import VenueCheck
from src.models.research_claim import ResearchClaimEvidenceLink
from src.models.research_experiment import (
    ResearchFigure,
    ResearchRunArtifact,
    ResearchRunManifest,
)
from src.models.research_project_role import ResearchProjectRole
from src.models.research_protocol import ProtocolDeviation, ResearchProtocolVersion
from src.models.research_synthesis import SynthesisResult
from src.services.artifacts.storage import get_artifact_storage
from src.services.research import claim_rules, draft_release_service
from src.services.research import manuscript_rules as rules
from src.services.research import (
    peer_review_service,
    release_rules,
    statements_service,
    venue_rules,
)
from src.services.research.bibliography_service import BibliographyService
from src.services.research.draft_generation_service import DraftGenerationService
from src.services.research_decisions import (
    DecisionIdempotencyConflict,
    append_decision,
    decision_request_fingerprint,
    lock_aggregate_stream,
)
from src.services.research_engine import (
    appraisal_service,
    audit_bundle,
    corpus_export,
    experiment_service,
    prisma,
    protocol_methods,
    rerun_service,
    synthesis_service,
)
from src.services.research_engine.contracts import canonical_json_sha256
from src.services.research_engine.identity_service import (
    _replayed_event,
    current_protocol_version_id,
)
from src.services.research_engine.prisma_service import read_inputs
from src.services.research_engine.project_access import ProjectContext
from src.services.research_engine.screening_service import _is_unique_violation
from src.shared.manuscript_release_schemas import (
    CandidateCreate,
    CheckResult,
    ManuscriptReleaseListResponse,
    ManuscriptReleaseResponse,
    MemberCheck,
    PackageFile,
    PackageVariant,
    PromoteRequest,
    ReferenceMapping,
    ReleaseVerification,
)
from src.shared.statements_schemas import VenueCheckCreate, VenueCheckResponse

AGGREGATE_TYPE = "research_manuscript"
SUBJECT_TYPE = "manuscript_release"
CHECKS_SCHEMA = "nous.manuscript-checks/1"
LINEAGE_SCHEMA = "nous.manuscript-figure-lineage/1"
ANONYMIZATION_LEAK = "anonymization_leak"

DRAFT_NOT_FOUND = "Draft not found"
RELEASE_NOT_FOUND = "Release not found"
NO_ANONYMIZED = "This release has no anonymized variant"
CONTENT_CHANGED = "Draft content changed; reload"
CANDIDATE_CHANGED = "Candidate changed; reload"
CANDIDATE_STALE = "Candidate is stale; rebuild"
PACKAGE_CHANGED = "Package bytes changed"
NOT_A_CANDIDATE = "Only a candidate release can be promoted"
OBLIGATIONS_NOT_MET = "Release obligations not met"

Edge = tuple[release_rules.Node, release_rules.Node]


class ObligationsNotMet(Exception):
    """Fresh re-evaluation refused; the route answers 409 with ``failing``."""

    def __init__(self, failing: Sequence[str]) -> None:
        super().__init__("obligations_not_met")
        self.failing = list(failing)


def _cid(context: ProjectContext) -> UUID:
    return cast(UUID, context.collection.id)


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _str(value: Any) -> str | None:
    return None if value is None else str(value)


async def _all(db: AsyncSession, statement: Any) -> list[Any]:
    return list((await db.execute(statement)).scalars().all())


def storage_key(organization_id: Any, release_id: Any, variant: str = "") -> str:
    name = "package.zip" if not variant else f"package.{variant}.zip"
    return f"artifacts/{organization_id}/manuscript-releases/{release_id}/{name}"


def variants(row: Any) -> tuple[list[Any], dict[str, Any] | None]:
    """(identified files, the anonymized variant record or None). GOO-315
    rows store a plain list and have no anonymized variant."""
    files = row.package_files
    if isinstance(files, list):
        return list(files), None
    return list(files["identified"]), {
        "files": list(files["anonymized"]),
        "sha256": files["anonymized_sha256"],
        "storage_key": files["anonymized_storage_key"],
    }


# --- Snapshot (reads only) ---


async def _draft(db: AsyncSession, collection_id: UUID, draft_id: UUID) -> Any:
    draft = (
        await db.execute(
            select(GeneratedDraft).where(
                GeneratedDraft.id == draft_id,
                GeneratedDraft.project_id == collection_id,
                GeneratedDraft.is_deleted.is_(False),
            )
        )
    ).scalar_one_or_none()
    if draft is None:
        raise HTTPException(status_code=404, detail=DRAFT_NOT_FOUND)
    return draft


async def _claims(
    db: AsyncSession, collection_id: UUID, draft: Any, content_hash: str
) -> tuple[dict[str, Any], rules.ReleaseIn | None, list[dict[str, Any]]]:
    """(snapshot claims, GOO-307's newest release, GOO-307's blockers). A
    verified release for these exact bytes supplies the ids; otherwise the
    gate's own ids are bound, with no release id."""
    graph = await draft_release_service._graph(db, collection_id)
    gate = await draft_release_service._gate(db, collection_id, draft, graph)
    rows = await draft_release_service._releases(db, [draft.id])
    status = draft_release_service._status(rows, graph)
    live = next((r for r in rows if r.stale_at is None), None)
    release = (
        None
        if live is None
        else rules.ReleaseIn(
            id=str(live.id),
            content_hash=str(live.content_hash),
            live=status == "verified",
        )
    )
    blockers = [
        b.model_dump(mode="json")
        for b in draft_release_service.blocker_models(gate.blockers)
    ]
    claims: dict[str, Any]
    if live is not None and status == "verified" and live.content_hash == content_hash:
        claims = {
            "claim_version_ids": list(live.claim_version_ids),
            "assessment_ids": list(live.assessment_ids),
            "interpretation_claim_version_ids": list(
                live.interpretation_claim_version_ids
            ),
            "draft_release_id": str(live.id),
        }
    else:
        claims = {
            "claim_version_ids": [str(i) for i in gate.claim_version_ids],
            "assessment_ids": [str(i) for i in gate.assessment_ids],
            "interpretation_claim_version_ids": [
                str(i) for i in gate.interpretation_ids
            ],
            "draft_release_id": None,
        }
    return claims, release, blockers


async def _references(
    db: AsyncSession, collection_id: UUID, draft: Any
) -> list[dict[str, Any]]:
    """GOO-291's canonical records with their stable ``docN`` keys, plus the
    record type, captured once (GOO-317 serializes them without re-resolving)."""
    service = DraftGenerationService(db)
    citations = await service.get_draft_citations(collection_id, draft.id)
    records, keys = service._canonical_citation_records(citations)
    out = []
    for saved, record, key in zip(citations, records, keys):
        out.append(
            {
                "key": key,
                "type": _str(record.type),
                "title": record.document_title,
                "authors": list(record.authors or []),
                "year": record.year,
                "venue": record.venue,
                "doi": record.doi,
                "arxiv_id": record.arxiv_id,
                "source": "citation" if saved.citation else "document_metadata",
            }
        )
    return out


async def _links(
    db: AsyncSession, collection_id: UUID, version_ids: Sequence[str]
) -> list[Any]:
    """Live figure and synthesis links of the bound claim versions."""
    if not version_ids:
        return []
    links = await _all(
        db,
        select(ResearchClaimEvidenceLink).where(
            ResearchClaimEvidenceLink.collection_id == collection_id,
            ResearchClaimEvidenceLink.claim_version_id.in_(
                [UUID(v) for v in version_ids]
            ),
        ),
    )
    dead = {row.supersedes_link_id for row in links if row.supersedes_link_id}
    return [
        row
        for row in sorted(links, key=lambda r: str(r.id))
        if row.status == "linked"
        and row.id not in dead
        and row.kind in ("figure", "synthesis_result")
    ]


async def _figures(db: AsyncSession, links: Sequence[Any]) -> list[dict[str, Any]]:
    ids = sorted({row.figure_id for row in links if row.kind == "figure"}, key=str)
    out = []
    for figure_id in ids:
        figure: Any = await db.get(ResearchFigure, figure_id)
        manifest: Any = await db.get(ResearchRunManifest, figure.manifest_id)
        artifact = await db.get(ResearchRunArtifact, figure.output_artifact_id)
        assert manifest is not None and artifact is not None  # FK RESTRICT
        out.append(
            {
                "figure_id": str(figure.id),
                "figure_key": figure.figure_key,
                "kind": figure.kind,
                "output_sha256": artifact.sha256,
                "run_id": str(figure.run_id),
                "manifest_id": str(manifest.id),
                "manifest_hash": manifest.manifest_hash,
                "completeness": manifest.completeness,
                "missing": list(manifest.missing),
                "reproduction": await rerun_service.latest_reproduction(
                    db, cast(UUID, figure.run_id)
                ),
            }
        )
    return out


async def _synthesis(db: AsyncSession, links: Sequence[Any]) -> list[dict[str, Any]]:
    ids = sorted(
        {row.synthesis_result_id for row in links if row.kind == "synthesis_result"},
        key=str,
    )
    rows = [cast(Any, await db.get(SynthesisResult, i)) for i in ids]
    return [
        {"result_id": str(r.id), "result_hash": r.result_hash, "status": r.status}
        for r in rows
    ]


async def _protocol_lineage(
    db: AsyncSession, collection_id: UUID
) -> tuple[Any, list[Any]]:
    """(current approved version or None, it and its ancestors)."""
    current_id = await current_protocol_version_id(db, collection_id)
    if current_id is None:
        return None, []
    current = await db.get(ResearchProtocolVersion, UUID(current_id))
    lineage = []
    version = current
    while version is not None:
        lineage.append(version)
        parent = version.parent_version_id
        version = (
            None if parent is None else await db.get(ResearchProtocolVersion, parent)
        )
    return current, lineage


async def _prisma(db: AsyncSession, collection_id: UUID) -> tuple[str, Any]:
    """(state, package or None); ``read_inputs`` because the writer already
    holds the Collection lock (``load_inputs`` would refuse)."""
    try:
        package = prisma.package(
            prisma.derive_prisma_flow(await read_inputs(db, collection_id))
        )
    except prisma.PrismaInconsistency:
        return "inconsistent", None
    if not package["body"]["counts"]["records_identified"]:
        return "none", None
    return "consistent", package


async def build_snapshot(
    db: AsyncSession, context: ProjectContext, draft: Any
) -> dict[str, Any]:
    """The release snapshot of one exact draft version from current data.
    Reads only; rebuilding it unchanged gives the same ``snapshot_hash``."""
    collection_id = _cid(context)
    content_hash = claim_rules.content_hash(draft.content)
    claims, _release, _blockers = await _claims(db, collection_id, draft, content_hash)
    links = await _links(
        db,
        collection_id,
        [*claims["claim_version_ids"], *claims["interpretation_claim_version_ids"]],
    )
    current, lineage = await _protocol_lineage(db, collection_id)
    lineage_ids = {v.id for v in lineage}
    runs = [
        run
        for run in await corpus_export._runs(db, context)
        if run.protocol_version_id is not None
        and run.protocol_version_id in lineage_ids
    ]
    deviations = (
        await _all(
            db,
            select(ProtocolDeviation)
            .where(
                ProtocolDeviation.collection_id == collection_id,
                ProtocolDeviation.protocol_version_id.in_(list(lineage_ids)),
            )
            .order_by(ProtocolDeviation.created_at, ProtocolDeviation.id),
        )
        if lineage_ids
        else []
    )
    prisma_state, prisma_package = await _prisma(db, collection_id)
    snapshot: dict[str, Any] = {
        "schema": rules.SNAPSHOT_SCHEMA,
        "draft": {
            "id": str(draft.id),
            "version": draft.version,
            "title": draft.title,
            "content_hash": content_hash,
        },
        "claims": claims,
        "references": await _references(db, collection_id, draft),
        "figures": await _figures(db, links),
        "synthesis": await _synthesis(db, links),
        "protocol": {
            "protocol_version_id": _str(current.id if current else None),
            "content_hash": current.content_hash if current else None,
            "question_version_id": _str(
                current.question_version_id if current else None
            ),
        },
        "runs": [
            {
                "id": str(run.id),
                "protocol_version_id": str(run.protocol_version_id),
                "effective_plan_hash": run.effective_plan_hash,
                "conformance_status": run.conformance_status,
            }
            for run in runs
        ],
        "deviations": [
            {
                "id": str(d.id),
                "protocol_version_id": str(d.protocol_version_id),
                "run_id": _str(d.run_id),
                "disposition": d.disposition,
            }
            for d in deviations
        ],
        "prisma_state": prisma_state,
        "prisma_body_sha256": prisma_package["body_sha256"] if prisma_package else None,
        "peer_review": await peer_review_service.open_obligations(
            db, collection_id, cast(UUID, draft.id)
        ),
    }
    # GOO-316: absent (not null) without a statement set, so a GOO-315
    # snapshot rebuilds to the same hash.
    statements = await statements_service.snapshot_binding(db, collection_id)
    if statements is not None:
        snapshot["statements"] = statements
    return snapshot


async def check_inputs(
    db: AsyncSession,
    context: ProjectContext,
    snapshot: Mapping[str, Any],
    packaged: Any = None,
) -> rules.CheckInputs:
    """Fresh inputs for ``manuscript_rules.evaluate``: GOO-307's live release
    and blockers, protocol amendments, synthesis/appraisal currency and
    peer-review rounds are read now; the rest comes from the snapshot.
    GOO-316: the bound set's approvals and ``packaged``'s venue checks (none
    before packaging) are read now too."""
    collection_id = _cid(context)
    draft_ref = snapshot["draft"]
    draft = await _draft(db, collection_id, UUID(draft_ref["id"]))
    _claims_out, release, blockers = await _claims(
        db, collection_id, draft, draft_ref["content_hash"]
    )
    protocol_id = snapshot["protocol"]["protocol_version_id"]
    versions: list[Any] = []
    synthesis_required = appraisal_required = False
    synthesis_current = appraisal_complete = False
    if protocol_id is not None:
        current = cast(Any, await db.get(ResearchProtocolVersion, UUID(protocol_id)))
        versions = await _all(
            db,
            select(ResearchProtocolVersion).where(
                ResearchProtocolVersion.protocol_id == current.protocol_id
            ),
        )
        method = cast(Mapping[str, Any], current.snapshot or {})
        try:
            selection = protocol_methods.synthesis_selection(method)
            synthesis_required = True
        except ValueError:
            selection = None
        try:
            protocol_methods.appraisal_method(method)
            appraisal_required = True
        except ValueError:
            pass
        if selection is not None:
            graph = await draft_release_service._graph(db, collection_id)
            results = await synthesis_service._results(db, collection_id)
            tip = synthesis_service._tip(results, selection[2], selection[3])
            synthesis_current = (
                tip is not None
                and tip.status == "computed"
                and str(tip.protocol_version_id) == protocol_id
                and release_rules.node("synthesis", tip.id) not in graph.stale_nodes()
            )
        if appraisal_required:
            statuses = await appraisal_service.current_appraisals(db, collection_id)
            appraisal_complete = bool(statuses) and all(
                s.value in ("agreed", "adjudicated") for s in statuses.values()
            )
    rounds = await peer_review_service.list_rounds(db, context)
    binding = snapshot.get("statements")
    unapproved: tuple[str, ...] = ()
    venue: tuple[rules.VenueIn, ...] = ()
    if binding:
        unapproved = await statements_service.unapproved_authors(db, binding)
    if binding and packaged is not None:
        venue = tuple(
            rules.VenueIn(
                str(v.id),
                str(v.package_sha256),
                int(v.profile_version),
                str(v.status),
                tuple(
                    name
                    for name, state in (v.result.get("rules") or {}).items()
                    if state != "pass"
                ),
            )
            for v in await statements_service.venue_checks_for(db, packaged.id)
        )
    return rules.CheckInputs(
        content_hash=draft_ref["content_hash"],
        draft_release=release,
        claim_blockers=tuple(blockers),
        protocol_version_id=protocol_id,
        runs=tuple(
            rules.RunIn(r["id"], str(r["conformance_status"])) for r in snapshot["runs"]
        ),
        deviations=tuple(
            rules.DeviationIn(
                d["id"], d["protocol_version_id"], d["run_id"], d["disposition"]
            )
            for d in snapshot["deviations"]
        ),
        versions=tuple(
            rules.VersionIn(
                str(v.id), _str(v.parent_version_id), v.change_kind, v.status
            )
            for v in versions
        ),
        synthesis_required=synthesis_required,
        synthesis_current=synthesis_current,
        appraisal_required=appraisal_required,
        appraisal_complete=appraisal_complete,
        has_review_rounds=bool(rounds.rounds),
        open_obligations=tuple(snapshot["peer_review"]),
        figures=tuple(
            rules.FigureIn(f["figure_key"], f["completeness"], f["reproduction"])
            for f in snapshot["figures"]
        ),
        prisma=snapshot["prisma_state"],
        statements_bound=bool(binding),
        unapproved_authors=unapproved,
        package_sha256=None if packaged is None else packaged.package_sha256,
        venue_checks=venue,
    )


# --- Package ---


def references_bib(snapshot: Mapping[str, Any]) -> str:
    """``references.bib`` from the snapshot records alone (never live)."""
    references = list(snapshot.get("references") or [])
    records = [
        SimpleNamespace(
            document_title=r.get("title"),
            authors=r.get("authors") or None,
            year=r.get("year"),
            venue=r.get("venue"),
            doi=r.get("doi"),
            arxiv_id=r.get("arxiv_id"),
            abstract=None,
        )
        for r in references
    ]
    return BibliographyService.format_bibtex(
        cast(list[Any], records), keys=[str(r["key"]) for r in references]
    )


async def _parts(
    db: AsyncSession,
    context: ProjectContext,
    draft: Any,
    snapshot: Mapping[str, Any],
    checks: Mapping[str, Any],
) -> list[audit_bundle.Part]:
    collection_id = _cid(context)
    exported = await DraftGenerationService(db).export_draft(
        collection_id, cast(UUID, draft.id)
    )
    if "error" in exported:
        raise HTTPException(status_code=500, detail="Draft export failed")
    labelled = cast(str, exported["content"]).encode("utf-8")
    source = cast(str, draft.content).encode("utf-8")
    bib = references_bib(snapshot).encode("utf-8")
    parts = [
        audit_bundle.Part("manuscript.md", None, labelled, _sha(labelled)),
        audit_bundle.Part("manuscript.source.md", None, source, _sha(source)),
        audit_bundle.Part("references.bib", None, bib, _sha(bib)),
        audit_bundle._sealed_part(
            "snapshot.json", rules.SNAPSHOT_SCHEMA, dict(snapshot), False
        ),
        audit_bundle._sealed_part("checks.json", CHECKS_SCHEMA, dict(checks), False),
        *await audit_bundle._methods(db, context),
    ]
    if snapshot.get("statements"):
        parts.append(
            audit_bundle._sealed_part(
                "statements.json",
                venue_rules.STATEMENTS_PART_SCHEMA,
                await statements_service.package_body(db, snapshot["statements"]),
                False,
            )
        )
    if snapshot["prisma_state"] == "consistent":
        _state, package = await _prisma(db, collection_id)
        parts.append(audit_bundle._package_part("prisma-flow.json", package, False))
    if snapshot["synthesis"]:
        parts += await audit_bundle._synthesis(db, context)
    if snapshot["figures"]:
        lineage = []
        for figure in snapshot["figures"]:
            view = await experiment_service.lineage(
                db, context, UUID(figure["figure_id"])
            )
            data, artifact = await experiment_service.read_artifact(
                db, SimpleNamespace(id=view.run_id), view.output.id
            )
            suffix = str(artifact.name).rsplit(".", 1)
            ext = suffix[1] if len(suffix) == 2 else "bin"
            path = f"figures/{figure['figure_key']}.{ext}"
            parts.append(audit_bundle.Part(path, None, data, _sha(data)))
            lineage.append(view.model_dump(mode="json"))
        parts.append(
            audit_bundle._sealed_part(
                "figures/lineage.json", LINEAGE_SCHEMA, lineage, False
            )
        )
    return parts


def _members(data: bytes) -> dict[str, bytes]:
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        return {name: archive.read(name) for name in archive.namelist()}


def package_files(data: bytes) -> list[dict[str, Any]]:
    return [
        {"path": path, "sha256": _sha(member), "bytes": len(member)}
        for path, member in sorted(_members(data).items())
    ]


# --- Responses and derived status ---


async def graph_part(
    db: AsyncSession, collection_id: UUID
) -> tuple[list[Edge], set[release_rules.Node]]:
    """``release -> manuscript`` for verified rows: a verified manuscript
    release goes stale exactly when GOO-307's walk reaches its draft release.
    Called by ``draft_release_service._graph``."""
    pairs = (
        await db.execute(
            select(ManuscriptRelease.id, ManuscriptRelease.draft_release_id).where(
                ManuscriptRelease.collection_id == collection_id,
                ManuscriptRelease.stage == "verified",
            )
        )
    ).all()
    edges: list[Edge] = [
        (
            release_rules.node("release", draft_release_id),
            release_rules.node("manuscript", release_id),
        )
        for release_id, draft_release_id in pairs
    ]
    return edges, set()


def _response(
    row: Any,
    status: str = "candidate",
    cause: str | None = None,
    venue: Mapping[str, Any] | None = None,
) -> ManuscriptReleaseResponse:
    """``venue``: GOO-316's live venue state for a statement-bound row (the
    stored candidate checks predate its package, so theirs is ``unknown``)."""
    checks = dict(row.checks) if venue is None else {**row.checks, "venue": venue}
    _files, anonymized = variants(row)
    response = cast(
        ManuscriptReleaseResponse, ManuscriptReleaseResponse.model_validate(row)
    )
    return cast(
        ManuscriptReleaseResponse,
        response.model_copy(
            update={
                "status": status,
                "stale_cause": cause,
                "checks": {k: CheckResult.model_validate(v) for k, v in checks.items()},
                "failing_obligations": rules.failing_obligations(
                    checks, rules.obligations(row.snapshot)
                ),
                "anonymized_files": (
                    None
                    if anonymized is None
                    else [PackageFile.model_validate(f) for f in anonymized["files"]]
                ),
                "anonymized_sha256": (
                    None if anonymized is None else anonymized["sha256"]
                ),
            }
        ),
    )


async def _venue_states(
    db: AsyncSession, rows: Sequence[Any]
) -> dict[Any, dict[str, Any]]:
    """The live ``venue`` result of each statement-bound row, evaluated on
    the candidate's venue checks for the row's exact package hash."""
    bound = [r for r in rows if r.snapshot.get("statements")]
    if not bound:
        return {}
    checked = [r.candidate_release_id or r.id for r in bound]
    by_release: dict[Any, list[Any]] = {}
    for check in await _all(
        db,
        select(VenueCheck)
        .where(VenueCheck.release_id.in_(checked))
        .order_by(VenueCheck.created_at, VenueCheck.id),
    ):
        by_release.setdefault(check.release_id, []).append(check)
    out = {}
    for row in bound:
        inputs = rules.CheckInputs(
            content_hash=row.content_hash,
            statements_bound=True,
            package_sha256=row.package_sha256,
            venue_checks=tuple(
                rules.VenueIn(
                    str(v.id),
                    str(v.package_sha256),
                    int(v.profile_version),
                    str(v.status),
                    tuple(
                        name
                        for name, state in (v.result.get("rules") or {}).items()
                        if state != "pass"
                    ),
                )
                for v in by_release.get(row.candidate_release_id or row.id, [])
            ),
        )
        out[row.id] = rules.evaluate(inputs)["venue"]
    return out


async def _responses(
    db: AsyncSession, collection_id: UUID, rows: Sequence[Any]
) -> list[ManuscriptReleaseResponse]:
    venue = await _venue_states(db, rows)
    verified = [r for r in rows if r.stage == "verified"]
    if not verified:
        return [_response(r, venue=venue.get(r.id)) for r in rows]
    stale_nodes = (await draft_release_service._graph(db, collection_id)).stale_nodes()
    stamped = {
        r.id
        for r in await _all(
            db,
            select(DraftRelease).where(
                DraftRelease.id.in_([r.draft_release_id for r in verified]),
                DraftRelease.stale_at.is_not(None),
            ),
        )
    }
    out = []
    for row in rows:
        cause = None
        if row.stage == "verified":
            if row.draft_release_id in stamped:
                cause = "draft_release_invalidated"
            elif release_rules.node("manuscript", row.id) in stale_nodes:
                cause = "upstream_changed"
        status = (
            "candidate"
            if row.stage == "candidate"
            else ("stale" if cause else "verified")
        )
        out.append(_response(row, status, cause, venue.get(row.id)))
    return out


async def list_releases(
    db: AsyncSession, context: ProjectContext
) -> ManuscriptReleaseListResponse:
    """VIEW: every release, newest last, with derived status and checks."""
    collection_id = _cid(context)
    rows = await _all(
        db,
        select(ManuscriptRelease)
        .where(ManuscriptRelease.collection_id == collection_id)
        .order_by(ManuscriptRelease.created_at, ManuscriptRelease.id),
    )
    return ManuscriptReleaseListResponse(
        releases=await _responses(db, collection_id, rows)
    )


async def _release(db: AsyncSession, collection_id: UUID, release_id: UUID) -> Any:
    row = (
        await db.execute(
            select(ManuscriptRelease).where(
                ManuscriptRelease.id == release_id,
                ManuscriptRelease.collection_id == collection_id,
            )
        )
    ).scalar_one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail=RELEASE_NOT_FOUND)
    return row


async def _one(
    db: AsyncSession, collection_id: UUID, row: Any
) -> ManuscriptReleaseResponse:
    return (await _responses(db, collection_id, [row]))[0]


# --- Writers ---


async def _begin(
    db: AsyncSession,
    context: ProjectContext,
    operation: str,
    data: Any,
    actor_id: UUID,
    **ids: Any,
) -> tuple[str, str, dict[str, Any] | None]:
    """Lock the stream, then return (key, fingerprint, replayed payload)."""
    stream = await lock_aggregate_stream(
        db,
        collection_id=_cid(context),
        aggregate_type=AGGREGATE_TYPE,
        aggregate_id=_cid(context),
    )
    key = f"{operation}:{data.idempotency_key}"
    fingerprint = decision_request_fingerprint(
        {
            "operation": operation,
            "actor_user_id": str(actor_id),
            **{name: str(value) for name, value in ids.items()},
            "request": data.model_dump(mode="json", exclude={"idempotency_key"}),
        }
    )
    replay = await _replayed_event(db, stream, key, fingerprint)
    return key, fingerprint, None if replay is None else dict(replay.payload)


async def _append(
    db: AsyncSession,
    context: ProjectContext,
    *,
    event_type: str,
    row: Any,
    actor_id: UUID,
    payload: dict[str, Any],
    key: str,
    fingerprint: str,
) -> None:
    payload = {
        "collection_id": str(_cid(context)),
        "release_id": str(row.id),
        **payload,
    }
    try:
        await append_decision(
            db,
            collection_id=_cid(context),
            aggregate_type=AGGREGATE_TYPE,
            aggregate_id=_cid(context),
            event_type=event_type,
            event_schema_version=1,
            actor_user_id=actor_id,
            actor_role=cast(str, row.actor_role),
            subject_type=SUBJECT_TYPE,
            subject_id=cast(UUID, row.id),
            subject_version_id=None,
            subject_hash=decision_request_fingerprint(payload),
            reason=None,
            payload=payload,
            idempotency_key=key,
            request_fingerprint=fingerprint,
        )
    except DecisionIdempotencyConflict as exc:
        raise HTTPException(status_code=409, detail="Idempotency conflict") from exc


async def _forget(keys: Sequence[str]) -> None:
    storage = get_artifact_storage()
    for key in keys:
        await storage.delete(key)


async def _commit_or_forget(db: AsyncSession, keys: Sequence[str]) -> None:
    try:
        await db.commit()
    except BaseException:
        await _forget(keys)
        raise


async def _anonymized(
    db: AsyncSession,
    context: ProjectContext,
    parts: Sequence[audit_bundle.Part],
    snapshot: Mapping[str, Any],
    meta: Mapping[str, Any],
) -> bytes:
    """GOO-316's anonymized variant of the same parts. Every member of the
    written zip is byte-scanned for every identity string; any hit fails
    the build (422 ``anonymization_leak:<member>``), so the redaction is
    never just trusted."""
    names = await statements_service.identities(db, context)
    body: Mapping[str, Any] = {}
    if snapshot.get("statements"):
        packaged = await statements_service.package_body(db, snapshot["statements"])
        body = packaged["statements"]
    redacted = venue_rules.anonymize({p.path: p.data for p in parts}, body, names)
    anonymous = [
        audit_bundle.Part(
            p.path,
            p.schema,
            redacted[p.path],
            (
                json.loads(redacted[p.path])["body_sha256"]
                if p.body_sha256 is not None and p.path.endswith(".json")
                else _sha(redacted[p.path])
            ),
            p.empty,
        )
        for p in parts
    ]
    package, _manifest_sha = audit_bundle.write_zip(anonymous, **meta)
    leaks = venue_rules.scan_leaks(_members(package), names)
    if leaks:
        raise HTTPException(status_code=422, detail=f"{ANONYMIZATION_LEAK}:{leaks[0]}")
    return package


async def create_candidate(
    db: AsyncSession, context: ProjectContext, actor_id: UUID, data: CandidateCreate
) -> tuple[ManuscriptReleaseResponse, bool]:
    """EDIT: package one exact draft version as a candidate; commits once.
    An identical snapshot already packaged for this draft is returned."""
    collection_id = _cid(context)
    key, fingerprint, replay = await _begin(db, context, "candidate", data, actor_id)
    if replay is not None:
        row = await _release(db, collection_id, UUID(replay["release_id"]))
        return await _one(db, collection_id, row), True
    draft = await _draft(db, collection_id, data.draft_id)
    content_hash = claim_rules.content_hash(draft.content)
    if content_hash != data.expected_content_hash:
        raise HTTPException(status_code=409, detail=CONTENT_CHANGED)
    snapshot = await build_snapshot(db, context, draft)
    digest = rules.snapshot_hash(snapshot)
    existing = (
        await db.execute(
            select(ManuscriptRelease).where(
                ManuscriptRelease.collection_id == collection_id,
                ManuscriptRelease.draft_id == draft.id,
                ManuscriptRelease.stage == "candidate",
                ManuscriptRelease.snapshot_hash == digest,
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        return await _one(db, collection_id, existing), True
    checks = rules.evaluate(await check_inputs(db, context, snapshot))
    release_id, created_at = uuid4(), datetime.now(timezone.utc)
    parts = await _parts(db, context, draft, snapshot, checks)
    meta: dict[str, Any] = {
        "project_id": str(collection_id),
        "generated_at": created_at.isoformat(),
        "deployment_sha": None,  # reproducible from the same inputs
        "protocol_version_id": snapshot["protocol"]["protocol_version_id"],
        "stream_heads": {},
        "schema": rules.PACKAGE_SCHEMA,
    }
    package, _manifest_sha = audit_bundle.write_zip(parts, **meta)
    anonymous = await _anonymized(db, context, parts, snapshot, meta)
    package_sha256, anonymized_sha256 = _sha(package), _sha(anonymous)
    object_key = storage_key(context.organization_id, release_id)
    anonymized_key = storage_key(context.organization_id, release_id, "anonymized")
    keys = [object_key, anonymized_key]
    storage = get_artifact_storage()
    await storage.put(object_key, package, "application/zip")
    await storage.put(anonymized_key, anonymous, "application/zip")
    try:
        row = ManuscriptRelease(
            id=release_id,
            collection_id=collection_id,
            draft_id=draft.id,
            draft_version=draft.version,
            content_hash=content_hash,
            stage="candidate",
            snapshot=snapshot,
            snapshot_hash=digest,
            checks=checks,
            checks_hash=canonical_json_sha256(checks),
            package_files={
                "identified": package_files(package),
                "anonymized": package_files(anonymous),
                "anonymized_sha256": anonymized_sha256,
                "anonymized_storage_key": anonymized_key,
            },
            package_sha256=package_sha256,
            package_storage_key=object_key,
            created_by_id=actor_id,
            actor_role="editor",
            created_at=created_at,
        )
        db.add(row)
        await db.flush()
        await _append(
            db,
            context,
            event_type="manuscript.candidate_created",
            row=row,
            actor_id=actor_id,
            payload={
                "draft_id": str(draft.id),
                "draft_version": draft.version,
                "content_hash": content_hash,
                "snapshot_hash": digest,
                "checks_hash": row.checks_hash,
                "package_sha256": package_sha256,
            },
            key=key,
            fingerprint=fingerprint,
        )
        if snapshot.get("statements"):
            # GOO-316: the automatic venue check of this exact package.
            await statements_service.record_venue_check(
                db,
                context,
                actor_id,
                row,
                _members(package),
                (_members(anonymous), anonymized_sha256),
                key=f"venue:candidate:{release_id}",
            )
    except BaseException:
        await _forget(keys)
        raise
    await _commit_or_forget(db, keys)
    return await _one(db, collection_id, row), False


async def _verified_for(db: AsyncSession, candidate_id: UUID) -> Any:
    return (
        await db.execute(
            select(ManuscriptRelease).where(
                ManuscriptRelease.candidate_release_id == candidate_id
            )
        )
    ).scalar_one_or_none()


async def _stored_bytes(row: Any) -> bytes:
    """A row's identified package, or an anonymized variant record's bytes."""
    key = row["storage_key"] if isinstance(row, dict) else row.package_storage_key
    data = await get_artifact_storage().get(cast(str, key))
    if data is None:
        raise HTTPException(status_code=404, detail=RELEASE_NOT_FOUND)
    return data


async def promote(
    db: AsyncSession,
    context: ProjectContext,
    actor_id: UUID,
    candidate_id: UUID,
    data: PromoteRequest,
) -> tuple[ManuscriptReleaseResponse, bool]:
    """RELEASE: promote one exact candidate to verified; commits once.
    ``ObligationsNotMet`` lists every failing obligation."""
    collection_id = _cid(context)
    key, fingerprint, replay = await _begin(
        db, context, "promote", data, actor_id, candidate_id=candidate_id
    )
    if replay is not None:
        row = await _release(db, collection_id, UUID(replay["release_id"]))
        return await _one(db, collection_id, row), True
    candidate = await _release(db, collection_id, candidate_id)
    if candidate.stage != "candidate":
        raise HTTPException(status_code=409, detail=NOT_A_CANDIDATE)
    if (candidate.snapshot_hash, candidate.content_hash) != (
        data.expected_snapshot_hash,
        data.expected_content_hash,
    ):
        raise HTTPException(status_code=409, detail=CANDIDATE_CHANGED)
    existing = await _verified_for(db, candidate_id)
    if existing is not None:
        return await _one(db, collection_id, existing), True
    draft = await _draft(db, collection_id, cast(UUID, candidate.draft_id))
    snapshot = await build_snapshot(db, context, draft)
    if rules.snapshot_hash(snapshot) != candidate.snapshot_hash:
        raise HTTPException(status_code=409, detail=CANDIDATE_STALE)
    checks = rules.evaluate(await check_inputs(db, context, snapshot, candidate))
    obligations = rules.obligations(snapshot)
    failing = rules.failing_obligations(checks, obligations)
    if failing:
        raise ObligationsNotMet(failing)
    package = await _stored_bytes(candidate)
    if _sha(package) != candidate.package_sha256:
        raise HTTPException(status_code=409, detail=PACKAGE_CHANGED)
    _identified, anonymized = variants(candidate)
    anonymous = None if anonymized is None else await _stored_bytes(anonymized)
    if anonymized is not None and _sha(cast(bytes, anonymous)) != anonymized["sha256"]:
        raise HTTPException(status_code=409, detail=PACKAGE_CHANGED)
    draft_release_id = snapshot["claims"]["draft_release_id"]
    release_id = uuid4()
    object_key = storage_key(context.organization_id, release_id)
    keys = [object_key]
    files: Any = candidate.package_files
    storage = get_artifact_storage()
    await storage.put(object_key, package, "application/zip")
    if anonymized is not None:
        anonymized_key = storage_key(context.organization_id, release_id, "anonymized")
        keys.append(anonymized_key)
        await storage.put(anonymized_key, cast(bytes, anonymous), "application/zip")
        files = {**candidate.package_files, "anonymized_storage_key": anonymized_key}
    try:
        row = ManuscriptRelease(
            id=release_id,
            collection_id=collection_id,
            draft_id=candidate.draft_id,
            draft_version=candidate.draft_version,
            content_hash=candidate.content_hash,
            stage="verified",
            candidate_release_id=candidate.id,
            draft_release_id=UUID(draft_release_id),
            snapshot=candidate.snapshot,
            snapshot_hash=candidate.snapshot_hash,
            checks=checks,
            checks_hash=canonical_json_sha256(checks),
            package_files=files,
            package_sha256=candidate.package_sha256,
            package_storage_key=object_key,
            created_by_id=actor_id,
            actor_role=(
                "adjudicator"
                if ResearchProjectRole.ADJUDICATOR in context.effective_roles
                else "supervisor"
            ),
        )
        db.add(row)
        try:
            await db.flush()
        except IntegrityError as error:
            # A promotion that raced past the existing-row check.
            await db.rollback()
            if not _is_unique_violation(error):
                raise
            winner = await _verified_for(db, candidate_id)
            if winner is None:
                raise
            await _forget(keys)
            return await _one(db, collection_id, winner), True
        await _append(
            db,
            context,
            event_type="manuscript.verified",
            row=row,
            actor_id=actor_id,
            payload={
                "candidate_release_id": str(candidate.id),
                "draft_release_id": draft_release_id,
                "content_hash": row.content_hash,
                "snapshot_hash": row.snapshot_hash,
                "package_sha256": row.package_sha256,
                "obligations": {k: checks[k]["state"] for k in obligations},
            },
            key=key,
            fingerprint=fingerprint,
        )
    except BaseException:
        await _forget(keys)
        raise
    await _commit_or_forget(db, keys)
    return await _one(db, collection_id, row), False


# --- Package reads ---


async def package_bytes(
    db: AsyncSession,
    context: ProjectContext,
    release_id: UUID,
    variant: PackageVariant = "identified",
) -> tuple[bytes, str]:
    """VIEW: the stored package (or GOO-316's anonymized variant, 404 for a
    GOO-315 row), re-hashed before it leaves."""
    row = await _release(db, _cid(context), release_id)
    if variant == "anonymized":
        _files, anonymized = variants(row)
        if anonymized is None:
            raise HTTPException(status_code=404, detail=NO_ANONYMIZED)
        source, expected = anonymized, str(anonymized["sha256"])
    else:
        source, expected = row, str(row.package_sha256)
    data = await _stored_bytes(source)
    if _sha(data) != expected:
        raise HTTPException(status_code=500, detail=PACKAGE_CHANGED)
    return data, expected


async def run_venue_check(
    db: AsyncSession,
    context: ProjectContext,
    actor_id: UUID,
    release_id: UUID,
    data: VenueCheckCreate,
) -> VenueCheckResponse:
    """EDIT: ``generic-icmje-credit/1`` against the stored package bytes of
    one release (both variants re-hashed first); commits once."""
    key, fingerprint, replay = await statements_service._begin(
        db, context, "venue", data, actor_id, release_id=release_id
    )
    if replay is not None:
        replayed = await db.get(VenueCheck, UUID(replay["venue_check_id"]))
        return statements_service.venue_response(replayed)
    row = await _release(db, _cid(context), release_id)
    package, _sha256 = await package_bytes(db, context, release_id)
    _files, anonymized = variants(row)
    anonymous = None
    if anonymized is not None:
        data_anon, sha = await package_bytes(db, context, release_id, "anonymized")
        anonymous = (_members(data_anon), sha)
    check = await statements_service.record_venue_check(
        db,
        context,
        actor_id,
        row,
        _members(package),
        anonymous,
        key=key,
        fingerprint=fingerprint,
    )
    await db.commit()
    return statements_service.venue_response(check)


async def verify(
    db: AsyncSession, context: ProjectContext, release_id: UUID
) -> ReleaseVerification:
    """VIEW: recompute every hash and the ``docN`` reference mapping from the
    stored bytes alone; later draft, citation or source edits change nothing."""
    row = await _release(db, _cid(context), release_id)
    data = await _stored_bytes(row)
    members = _members(data)
    identified, _anonymized = variants(row)
    listed = {f["path"]: f["sha256"] for f in identified}
    checks = [
        MemberCheck(
            path=path,
            sha256=sha,
            ok=_sha(members.get(path, b"")) == sha and path in members,
        )
        for path, sha in sorted(listed.items())
    ]
    bundle_error = None
    try:
        audit_bundle.verify_bundle(data, schema=rules.PACKAGE_SCHEMA)
    except audit_bundle.BundleError as error:
        bundle_error = str(error)
    mapping: list[ReferenceMapping] = []
    references_ok = False
    try:
        snapshot = json.loads(members["snapshot.json"])["body"]
        bib = members["references.bib"].decode("utf-8")
        titles = {r["key"]: r.get("title") for r in snapshot.get("references") or []}
        mapping = [
            ReferenceMapping(key=k, title=titles.get(k), entry_sha256=sha)
            for k, sha in rules.reference_mapping(snapshot, bib)
        ]
        references_ok = references_bib(snapshot) == bib
    except (KeyError, ValueError):
        mapping = []
    return ReleaseVerification(
        release_id=cast(UUID, row.id),
        package_sha256=cast(str, row.package_sha256),
        package_sha256_ok=_sha(data) == row.package_sha256,
        members=checks,
        bundle_ok=bundle_error is None and set(listed) == set(members),
        bundle_error=bundle_error,
        references_ok=references_ok,
        reference_mapping=mapping,
    )


async def export_body(db: AsyncSession, context: ProjectContext) -> dict[str, Any]:
    """The audit bundle's ``manuscript-releases.json`` body: each row's
    snapshot, checks and package hashes (never the package bytes or key)."""
    listing = await list_releases(db, context)
    rows = {
        r.id: r
        for r in await _all(
            db,
            select(ManuscriptRelease).where(
                ManuscriptRelease.collection_id == _cid(context)
            ),
        )
    }
    return {
        "project_id": str(_cid(context)),
        "releases": [
            release.model_dump(mode="json") | {"snapshot": rows[release.id].snapshot}
            for release in listing.releases
        ],
    }
