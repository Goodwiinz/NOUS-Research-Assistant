"""Offline-verifiable project audit bundle (GOO-308).

The bundle has no reader of its own: every part comes from an existing export
or read function called with the caller's ``ProjectContext``, so it can never
expose what its parts withhold (GOO-300's restriction policy, GOO-302's reveal
predicate, GOO-306's no-document-text rule). ``write_zip`` and
``verify_bundle`` are pure; ``build`` expects ``journey.begin_read_snapshot``
to have opened one snapshot before the access check, so every part reads the
same rows. A part that fails (for example GOO-300's 413) fails the whole
bundle: a partial bundle is never sent.
"""

import asyncio
import hashlib
import io
import json
import os
import zipfile
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, cast
from uuid import UUID

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.models.document import Document
from src.models.draft_release import DraftRelease
from src.models.extraction_matrix import ExtractionMatrix, ExtractionObservation
from src.models.generated_draft import GeneratedDraft
from src.models.research_decision import ResearchDecisionStream
from src.models.research_protocol import ResearchProtocol, ResearchProtocolVersion
from src.services.research import claims_service, draft_release_service
from src.services.research import extraction_forms_service as forms
from src.services.research import peer_review_service
from src.services.research.draft_generation_service import DraftGenerationService
from src.services.research_engine import (
    appraisal_service,
    corpus_export,
    evidence_service,
    experiment_service,
    identity_service,
    prisma,
    rerun_service,
    synthesis_service,
)
from src.services.research_engine.contracts import canonical_json_sha256
from src.services.research_engine.prisma_service import load_inputs
from src.services.research_engine.project_access import (
    ProjectContext,
    project_documents_query,
)
from src.services.research_engine.run_lifecycle import _PAUSE_REQUESTED_KEY

SCHEMA = "nous.academic.audit-bundle.v1"
METHODS_SCHEMA = "nous.academic.audit-methods.v1"
EXTRACTION_SCHEMA = "nous.academic.audit-extraction.v1"
RELEASE_CHECKS_SCHEMA = "nous.academic.release-checks.v1"
MANUSCRIPT_RELEASES_SCHEMA = "nous.academic.manuscript-releases.v1"
MANIFEST = "manifest.json"
SUMS = "SHA256SUMS"
_ZIP_TIME = (1980, 1, 1, 0, 0, 0)  # fixed: identical parts, identical members


class BundleError(ValueError):
    """The bundle does not verify; the message names the first mismatch."""


@dataclass(frozen=True)
class Part:
    path: str
    schema: str | None
    data: bytes
    body_sha256: str | None
    empty: bool = False


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _dump(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, indent=2, ensure_ascii=False).encode()


def _package_part(path: str, package: dict[str, Any], empty: bool) -> Part:
    return Part(path, package["schema"], _dump(package), package["body_sha256"], empty)


def _sealed_part(path: str, schema: str, body: Any, empty: bool) -> Part:
    body = json.loads(json.dumps(body, default=str))
    package = {
        "schema": schema,
        "body_sha256": canonical_json_sha256(body),
        "body": body,
    }
    return _package_part(path, package, empty)


# --- parts: each one an existing export or reader ------------------------------


async def _corpus(db: AsyncSession, context: ProjectContext) -> list[Part]:
    package = await corpus_export.build_package(db, context)
    empty = not package["body"]["counts"]["tables"]["reports"]
    return [_package_part("corpus.json", package, empty)]


async def _prisma(db: AsyncSession, context: ProjectContext) -> list[Part]:
    try:
        package = prisma.package(
            prisma.derive_prisma_flow(await load_inputs(db, context))
        )
    except prisma.PrismaInconsistency as error:
        raise HTTPException(
            status_code=500, detail="PRISMA flow inconsistent"
        ) from error
    empty = not package["body"]["counts"]["records_identified"]
    return [_package_part("prisma-flow.json", package, empty)]


async def _claims(db: AsyncSession, context: ProjectContext) -> list[Part]:
    data, _ = await claims_service.export_package(db, context, None)
    package = json.loads(data)
    return [_package_part("claims.json", package, not package["body"]["claims"])]


async def _drafts(db: AsyncSession, context: ProjectContext) -> list[Part]:
    """The current draft and every released version: the labelled export
    (``.md``), the stored content whose sha256 is the release/claims
    ``content_hash`` (``.source.md``), and GOO-307's check for each."""
    cid = cast(UUID, context.collection.id)
    released = select(DraftRelease.draft_id).where(DraftRelease.collection_id == cid)
    drafts = (
        (
            await db.execute(
                select(GeneratedDraft)
                .where(
                    GeneratedDraft.project_id == cid,
                    GeneratedDraft.is_deleted.is_(False),
                    GeneratedDraft.is_current.is_(True)
                    | GeneratedDraft.id.in_(released),
                )
                .order_by(GeneratedDraft.version, GeneratedDraft.id)
            )
        )
        .scalars()
        .all()
    )
    service = DraftGenerationService(db)
    parts, checks = [], []
    for draft in drafts:
        name = f"drafts/{draft.id}-v{draft.version}"
        exported = await service.export_draft(cid, cast(UUID, draft.id))
        if "error" in exported:
            raise HTTPException(status_code=500, detail="Draft export failed")
        labelled = cast(str, exported["content"]).encode("utf-8")
        source = cast(str, draft.content).encode("utf-8")
        parts.append(Part(f"{name}.md", None, labelled, _sha(labelled)))
        parts.append(Part(f"{name}.source.md", None, source, _sha(source)))
        check = await draft_release_service.check(
            db, context, cast(UUID, draft.id), cast(int, draft.version)
        )
        checks.append(check.model_dump(mode="json"))
    parts.append(
        _sealed_part(
            "drafts/release-checks.json", RELEASE_CHECKS_SCHEMA, checks, not checks
        )
    )
    return parts


async def _methods(db: AsyncSession, context: ProjectContext) -> list[Part]:
    """Protocol snapshots and run hashes, so both can be recomputed offline."""
    versions = (
        (
            await db.execute(
                select(ResearchProtocolVersion)
                .join(
                    ResearchProtocol,
                    ResearchProtocol.id == ResearchProtocolVersion.protocol_id,
                )
                .where(
                    ResearchProtocol.collection_id == context.collection.id,
                    ResearchProtocolVersion.status.in_(["approved", "superseded"]),
                )
                .order_by(
                    ResearchProtocolVersion.created_at, ResearchProtocolVersion.id
                )
            )
        )
        .scalars()
        .all()
    )
    runs = []
    for run in await corpus_export._runs(db, context):
        manifest = dict(run.reproducibility_manifest or {})
        manifest.pop(_PAUSE_REQUESTED_KEY, None)
        runs.append(
            {
                "id": run.id,
                "status": run.status,
                "protocol_version_id": run.protocol_version_id,
                "effective_plan_hash": run.effective_plan_hash,
                "conformance_status": run.conformance_status,
                "blueprint_version": run.blueprint_version,
                "reproducibility_manifest": manifest,
            }
        )
    body = {
        "protocol_versions": [
            {
                "id": v.id,
                "status": v.status,
                "content_hash": v.content_hash,
                "question_version_id": v.question_version_id,
                "blueprint_id": v.blueprint_id,
                "snapshot": v.snapshot,
                "execution_plan": v.execution_plan,
            }
            for v in versions
        ],
        "runs": runs,
    }
    return [
        _sealed_part("methods.json", METHODS_SCHEMA, body, not versions and not runs)
    ]


async def _extraction(db: AsyncSession, context: ProjectContext) -> list[Part]:
    """Every matrix's form versions and accepted tips with their cited
    observations; no document text (no ``document`` is passed to the views)."""
    cid = cast(UUID, context.collection.id)
    allowed = sorted(
        (await db.execute(project_documents_query(cid).with_only_columns(Document.id)))
        .scalars()
        .all(),
        key=str,
    )
    matrices = []
    for matrix in (
        await db.execute(
            select(ExtractionMatrix)
            .where(
                ExtractionMatrix.project_id == cid,
                ExtractionMatrix.is_deleted.is_(False),
            )
            .order_by(ExtractionMatrix.created_at, ExtractionMatrix.id)
        )
    ).scalars():
        versions = await forms.list_versions(db, context, cast(UUID, matrix.id))
        _, cells = await forms.cell_view(db, matrix, allowed)
        stale = {
            (c["document_id"], c["field_id"]): c["stale"]
            for c in cells
            if c["source"] == "accepted"
        }
        tips = [
            t
            for t in await forms._tips(db, [v.id for v in versions])
            if t.document_id in allowed
        ]
        cited = {UUID(str(i)) for t in tips for i in t.observation_ids or []}
        observations = (
            (
                await db.execute(
                    select(ExtractionObservation).where(
                        ExtractionObservation.id.in_(cited)
                    )
                )
            )
            .scalars()
            .all()
        )
        matrices.append(
            {
                "id": matrix.id,
                "name": matrix.name,
                "form_versions": [v.model_dump(mode="json") for v in versions],
                "accepted_tips": sorted(
                    (
                        forms._accepted(t).model_dump(mode="json")
                        | {"stale": stale.get((str(t.document_id), str(t.field_id)))}
                        for t in tips
                    ),
                    key=lambda t: (t["document_id"], t["field_id"], t["id"]),
                ),
                "observations": sorted(
                    (
                        forms._observation(o).model_dump(mode="json")
                        for o in observations
                    ),
                    key=lambda o: o["id"],
                ),
            }
        )
    return [_sealed_part("extraction.json", EXTRACTION_SCHEMA, matrices, not matrices)]


async def _appraisal(db: AsyncSession, context: ProjectContext) -> list[Part]:
    """GOO-309's export with no viewer: only revealed results' rows."""
    package = await appraisal_service.export_package(db, context, viewer_id=None)
    body = package["body"]
    return [
        _sealed_part(
            "appraisal.json", appraisal_service.EXPORT_SCHEMA, body, not body["results"]
        )
    ]


async def _evidence(db: AsyncSession, context: ProjectContext) -> list[Part]:
    """GOO-310's export: every table version, chain and certainty row."""
    package = await evidence_service.export_package(db, context)
    body = package["body"]
    empty = not any(o["tables"] for o in body["outcomes"])
    return [_sealed_part("evidence.json", evidence_service.EXPORT_SCHEMA, body, empty)]


async def _synthesis(db: AsyncSession, context: ProjectContext) -> list[Part]:
    """GOO-311's export: every result, failed and stale ones included."""
    package = await synthesis_service.export_package(db, context)
    body = package["body"]
    return [
        _sealed_part(
            "synthesis.json",
            synthesis_service.EXPORT_SCHEMA,
            body,
            not body["results"],
        )
    ]


async def _experiments(db: AsyncSession, context: ProjectContext) -> list[Part]:
    """GOO-312: every run manifest and figure version (no storage keys)."""
    body = await experiment_service.export_body(db, context)
    empty = not body["manifests"] and not body["figures"]
    return [
        _sealed_part("experiments.json", experiment_service.EXPORT_SCHEMA, body, empty)
    ]


async def _reproduction(db: AsyncSession, context: ProjectContext) -> list[Part]:
    """GOO-313: every rerun with its rule, hash and attempts (no storage keys)."""
    body = await rerun_service.export_body(db, context)
    return [
        _sealed_part(
            "reproduction.json", rerun_service.EXPORT_SCHEMA, body, not body["reruns"]
        )
    ]


async def _peer_review(db: AsyncSession, context: ProjectContext) -> list[Part]:
    """GOO-314: every peer-review round with responses and decisions."""
    body = await peer_review_service.export_body(db, context)
    return [
        _sealed_part(
            "peer_review.json",
            peer_review_service.EXPORT_SCHEMA,
            body,
            not body["rounds"],
        )
    ]


async def _manuscript_releases(db: AsyncSession, context: ProjectContext) -> list[Part]:
    """GOO-315: every manuscript release's snapshot, checks and package
    hashes (never the package bytes). Local import: that service reuses
    this module's writer."""
    from src.services.research import manuscript_release_service

    body = await manuscript_release_service.export_body(db, context)
    return [
        _sealed_part(
            "manuscript-releases.json",
            MANUSCRIPT_RELEASES_SCHEMA,
            body,
            not body["releases"],
        )
    ]


async def gather_parts(db: AsyncSession, context: ProjectContext) -> list[Part]:
    """The builders, in a fixed order (looked up at call time). PRISMA reads
    last: without the caller's snapshot, ``load_inputs`` would open its own
    and expire ``context`` for every builder after it."""
    parts: list[Part] = []
    for build in (
        _corpus,
        _claims,
        _drafts,
        _methods,
        _extraction,
        _appraisal,
        _evidence,
        _synthesis,
        _experiments,
        _reproduction,
        _peer_review,
        _manuscript_releases,
        _prisma,
    ):
        parts.extend(await build(db, context))
    return parts


async def _stream_heads(db: AsyncSession, cid: UUID) -> dict[str, int]:
    rows = (
        await db.execute(
            select(
                ResearchDecisionStream.aggregate_type,
                ResearchDecisionStream.aggregate_id,
                ResearchDecisionStream.next_seq,
            ).where(ResearchDecisionStream.collection_id == cid)
        )
    ).all()
    return dict(sorted((f"{t}:{a}", int(n) - 1) for t, a, n in rows))


async def build(db: AsyncSession, context: ProjectContext) -> tuple[bytes, str]:
    """(zip bytes, manifest sha256) for one snapshot of the project."""
    cid = cast(UUID, context.collection.id)
    heads = await _stream_heads(db, cid)
    protocol_version_id = await identity_service.current_protocol_version_id(db, cid)
    parts = await gather_parts(db, context)
    return await asyncio.to_thread(
        write_zip,
        parts,
        project_id=str(cid),
        generated_at=datetime.now(timezone.utc).isoformat(),
        deployment_sha=os.getenv("GIT_SHA"),
        protocol_version_id=protocol_version_id,
        stream_heads=heads,
    )


# --- pure: write and verify ------------------------------------------------------


def write_zip(
    parts: list[Part],
    *,
    project_id: str,
    generated_at: str,
    deployment_sha: str | None,
    protocol_version_id: str | None,
    stream_heads: dict[str, int],
    schema: str = SCHEMA,
) -> tuple[bytes, str]:
    """(zip, manifest sha256). Sorted members and a fixed timestamp, so
    identical parts give identical member bytes (``generated_at`` aside).
    ``schema``: GOO-315's manuscript package reuses this writer."""
    paths = [p.path for p in parts]
    if len(set(paths)) != len(paths) or {MANIFEST, SUMS} & set(paths):
        raise ValueError("duplicate or reserved part path")
    manifest = _dump(
        {
            "schema": schema,
            "project_id": project_id,
            "generated_at": generated_at,
            "deployment_sha": deployment_sha,
            "protocol_version_id": protocol_version_id,
            "stream_heads": stream_heads,
            "parts": [
                {
                    "path": p.path,
                    "schema": p.schema,
                    "sha256": _sha(p.data),
                    "bytes": len(p.data),
                    "body_sha256": p.body_sha256,
                    "status": "empty" if p.empty else "ok",
                }
                for p in sorted(parts, key=lambda p: p.path)
            ],
        }
    )
    members = {p.path: p.data for p in parts} | {MANIFEST: manifest}
    # sha256sum format: "<hex>  <path>"; `sha256sum -c SHA256SUMS` checks it.
    members[SUMS] = "".join(
        f"{_sha(data)}  {path}\n" for path, data in sorted(members.items())
    ).encode()
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, mode="w") as archive:
        for path in sorted(members):
            info = zipfile.ZipInfo(path, date_time=_ZIP_TIME)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o644 << 16
            archive.writestr(info, members[path])
    return buffer.getvalue(), _sha(manifest)


def _read(data: bytes) -> dict[str, bytes]:
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            return {name: archive.read(name) for name in archive.namelist()}
    except (zipfile.BadZipFile, ValueError) as exc:
        raise BundleError("not a readable zip") from exc


def _json(members: dict[str, bytes], path: str) -> Any:
    try:
        return json.loads(members[path])
    except (KeyError, ValueError) as exc:
        raise BundleError(f"{path} is missing or not JSON") from exc


def verify_bundle(data: bytes, *, schema: str = SCHEMA) -> dict[str, Any]:
    """Check SHA256SUMS, each part's body hash, the draft hashes against the
    claims package and release checks, and GOO-300's corpus package (pure).
    Another ``schema`` (GOO-315's manuscript package) gets the generic member
    checks only: it has no claims, release-check or corpus part."""
    members = _read(data)
    sums: dict[str, str] = {}
    for line in members.get(SUMS, b"").decode().splitlines():
        digest, sep, path = line.partition("  ")
        if not sep:
            raise BundleError("malformed SHA256SUMS line")
        sums[path] = digest
    if set(sums) != set(members) - {SUMS}:
        raise BundleError("SHA256SUMS does not list exactly the bundle members")
    for path, digest in sums.items():
        if _sha(members[path]) != digest:
            raise BundleError(f"{path} does not match SHA256SUMS")

    manifest = _json(members, MANIFEST)
    if manifest.get("schema") != schema:
        raise BundleError("unsupported bundle schema")
    listed = {p["path"]: p for p in manifest["parts"]}
    if set(listed) != set(members) - {SUMS, MANIFEST}:
        raise BundleError("manifest does not list exactly the bundle parts")
    for path, entry in listed.items():
        member = members[path]
        if entry["sha256"] != _sha(member) or entry["bytes"] != len(member):
            raise BundleError(f"{path} does not match the manifest")
        if path.endswith(".json"):
            package = _json(members, path)
            body_sha = canonical_json_sha256(package.get("body"))
            if body_sha != package.get("body_sha256"):
                raise BundleError(f"{path} body does not match its body_sha256")
        else:
            body_sha = _sha(member)
        if entry["body_sha256"] != body_sha:
            raise BundleError(f"{path} body_sha256 does not match the manifest")
    if schema != SCHEMA:
        return {
            "manifest_sha256": _sha(members[MANIFEST]),
            "project_id": manifest["project_id"],
            "parts": sorted(listed),
        }

    claims = _json(members, "claims.json")["body"]
    checks = _json(members, "drafts/release-checks.json")["body"]
    claimed = {d["id"]: d["content_hash"] for d in claims["drafts"]}
    checked = {f"{c['draft_id']}-v{c['draft_version']}": c for c in checks}
    for path, member in members.items():
        if not path.endswith(".source.md"):
            continue
        name = path.removeprefix("drafts/").removesuffix(".source.md")
        check = checked.get(name)
        if check is None or check["content_hash"] != _sha(member):
            raise BundleError(f"{path} does not match its release check")
        draft_id = name.rsplit("-v", 1)[0]
        if draft_id in claimed and claimed[draft_id] != _sha(member):
            raise BundleError(f"{path} does not match the claims package")
    try:
        corpus = corpus_export.verify_package(members["corpus.json"])
    except corpus_export.CorpusPackageError as exc:
        raise BundleError(f"corpus.json: {exc}") from exc
    return {
        "manifest_sha256": _sha(members[MANIFEST]),
        "project_id": manifest["project_id"],
        "parts": sorted(listed),
        "corpus_body_sha256": corpus.body_sha256,
    }
