"""Run manifests, retained run artifacts and figure records (GOO-312).

The only writer of ``research_run_manifests``, ``research_run_artifacts`` and
``research_figures`` (``test_experiment_boundary`` enforces it). All three are
insert-only (a database trigger refuses UPDATE and DELETE).

``record_manifest`` runs inside the run's locked terminal-status transaction
and never commits: the terminal status and the manifest commit or roll back
together. Lock order there: the run row (``with_for_update`` in the stream
route), then this Collection's ``research_experiment`` stream.

``register_figure``: the route's ``resolve_project`` (Workspace SHARE ->
Collection UPDATE), then ``research_experiment``, then (inside
``invalidate_dependents``, last) ``research_release``. It commits once.

Bytes live in private artifact storage under content-addressed keys
(``manifest_rules.artifact_key``); a key never reaches a manifest, a ledger
payload or a response model. Staleness is derived on read through GOO-307's
graph walk: a changed or deleted input document stales the runs that read it,
their figures and the links and releases that cite those figures.
"""

import os
from typing import Any, Callable, Mapping, Sequence, cast
from uuid import UUID, uuid4

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.concurrency import run_in_threadpool

from src.models.document import Document
from src.models.research_evidence_table import EvidenceTableVersion
from src.models.research_experiment import (
    ResearchFigure,
    ResearchRunArtifact,
    ResearchRunManifest,
)
from src.models.research_protocol import (
    ProtocolDeviation,
    ResearchProtocolVersion,
    ResearchQuestionVersion,
)
from src.models.research_step import ResearchStep
from src.schemas.research_engine import (
    FigureCreate,
    FigureLineageResponse,
    FigureListResponse,
    FigureResponse,
    RunArtifactResponse,
    RunManifestV2Response,
)
from src.services.artifacts.storage import get_artifact_storage
from src.services.research import release_rules
from src.services.research_decisions import (
    DecisionIdempotencyConflict,
    append_decision,
    decision_request_fingerprint,
    lock_aggregate_stream,
)
from src.services.research_engine import evidence_rules
from src.services.research_engine import manifest_rules as rules
from src.services.research_engine.appraisal_service import _stale_nodes
from src.services.research_engine.contracts import canonical_json_bytes
from src.services.research_engine.identity_service import _replayed_event
from src.services.research_engine.project_access import (
    ProjectContext,
    project_documents_query,
)
from src.services.research_engine.screening_service import _is_unique_violation

AGGREGATE_TYPE = "research_experiment"
SUBJECT_TYPE = "experiment"

MANIFEST_NOT_FOUND = "Run manifest not found"
ARTIFACT_NOT_FOUND = "Run artifact not found"
ARTIFACT_CORRUPT = "Run artifact bytes do not match their hash"
OUTPUT_NOT_FOUND = "Run output not found"
OUTPUT_NOT_FIGURE = "Only a figure or table output of a recorded run can be registered"
FIGURE_NOT_FOUND = "Figure not found"
FIGURE_STALE = "Figure is stale; reload"
FIGURE_NOT_CURRENT = "Figure is not current"

Edge = tuple[release_rules.Node, release_rules.Node]
InputLoader = Callable[[Mapping[str, Any]], Any]


class ManifestSecretDetected(RuntimeError):
    """A manifest or environment capture carried a credential."""


def _cid(context: ProjectContext) -> UUID:
    return cast(UUID, context.collection.id)


def _conflict(detail: str) -> HTTPException:
    return HTTPException(status_code=409, detail=detail)


async def _all(db: AsyncSession, statement: Any) -> list[Any]:
    return list((await db.execute(statement)).scalars().all())


def _iso(value: Any) -> str | None:
    return None if value is None else value.isoformat()


# --- Inputs (step executor) -------------------------------------------------------


async def _document_bytes(document: Any) -> bytes:
    """The stored upload, whichever backend holds it; ``LookupError`` if gone."""
    path = document.storage_path
    if document.storage_backend == "supabase" and path:
        from src.core.supabase_client import StorageHelper, parse_storage_key

        bucket, key = parse_storage_key(path)
        return cast(
            bytes, await run_in_threadpool(StorageHelper().download_file, bucket, key)
        )
    if document.storage_backend == "s3" and path:
        from src.core.s3_client import S3StorageHelper

        return cast(
            bytes, await run_in_threadpool(S3StorageHelper().download_file, path)
        )
    local = document.file_path
    if not local or not os.path.exists(local):
        raise LookupError("input_not_found")

    def read() -> bytes:
        with open(local, "rb") as handle:
            return handle.read()

    return await run_in_threadpool(read)


def input_loader(db: AsyncSession, collection_id: UUID) -> InputLoader:
    """``StepExecutor(analyze_inputs=...)``: one declared input as
    ``(bytes, recorded_sha256, media_type)``, scoped to this Collection's
    organization-visible documents and its own evidence tables."""

    async def load(item: Mapping[str, Any]) -> tuple[bytes, Any, str]:
        ref = UUID(str(item["id"]))
        if item["kind"] == "document":
            document = (
                (
                    await db.execute(
                        project_documents_query(collection_id).where(Document.id == ref)
                    )
                )
                .scalars()
                .first()
            )
            if document is None:
                raise LookupError("input_not_found")
            data = await _document_bytes(document)
            return data, document.checksum_sha256, str(document.mime_type)
        table: Any = (
            await db.execute(
                select(EvidenceTableVersion).where(
                    EvidenceTableVersion.id == ref,
                    EvidenceTableVersion.collection_id == collection_id,
                )
            )
        ).scalar_one_or_none()
        if table is None:
            raise LookupError("input_not_found")
        data = evidence_rules.table_bytes(
            table.protocol_version_id,
            table.form_version_id,
            table.outcome_key,
            table.timepoint,
            table.field_ids,
            table.rows,
            table.excluded,
        )
        return data, table.content_hash, "application/json"

    return load


# --- Manifest recording (run terminal transaction) ---------------------------------


def _analyze_step(steps: Sequence[Mapping[str, Any]]) -> tuple[int, Any] | None:
    # ponytail: one manifest binds the plan's first analyze step; a plan
    # with several needs a manifest per step (a schema/3 change).
    for index, step in enumerate(steps):
        if (step or {}).get("type") == rules.STEP_TYPE:
            params = step.get("params") or step.get("parameters") or {}
            return index, rules.parse_analyze_step(params)
    return None


def _artifact_rows(
    run: Any,
    collection_id: UUID,
    organization_id: UUID,
    out: Mapping[str, Any],
) -> tuple[list[ResearchRunArtifact], dict[str, Any]]:
    """One row per retained file plus the manifest pieces that cite them."""

    def row(
        role: str, name: str, media_type: str, digest: str, size: int, **extra: Any
    ) -> ResearchRunArtifact:
        return ResearchRunArtifact(
            id=uuid4(),
            run_id=run.id,
            collection_id=collection_id,
            organization_id=organization_id,
            role=role,
            name=name,
            media_type=media_type,
            sha256=digest,
            byte_size=size,
            storage_key=rules.artifact_key(str(organization_id), str(run.id), digest),
            **extra,
        )

    code_in = out.get("code") or {}
    env_in = dict(out.get("environment") or {})
    code = row(
        "code",
        rules.CODE_NAME,
        "text/x-python",
        code_in["sha256"],
        code_in["byte_size"],
    )
    lock = row(
        "environment",
        rules.LOCK_NAME,
        "text/plain",
        env_in["lock_sha256"],
        env_in.pop("lock_byte_size"),
    )
    inputs = [
        row(
            "input",
            item["name"],
            item["media_type"],
            item["sha256"],
            item["byte_size"],
            source_ref={"kind": item["kind"], "id": item["ref_id"]},
        )
        for item in out.get("inputs") or []
    ]
    outputs = [
        row(
            "output",
            item["name"],
            item["media_type"],
            item["sha256"],
            item["byte_size"],
        )
        for item in out.get("outputs") or []
    ]
    pieces = {
        "code": {
            "sha256": code.sha256,
            "artifact_id": str(code.id),
            "repository": code_in.get("repository"),
            "commit": code_in.get("commit"),
        },
        "environment": {**env_in, "lock_artifact_id": str(lock.id)},
        "inputs": [
            {**item, "artifact_id": str(r.id)}
            for item, r in zip(out.get("inputs") or [], inputs)
        ],
        "outputs": [
            {**item, "artifact_id": str(r.id)}
            for item, r in zip(out.get("outputs") or [], outputs)
        ],
    }
    return [code, lock, *inputs, *outputs], pieces


async def _forget_blobs(rows: Sequence[ResearchRunArtifact]) -> None:
    """Compensating delete for keys this run wrote; nothing committed cites them."""
    storage = get_artifact_storage()
    for key in {cast(str, r.storage_key) for r in rows}:
        await storage.delete(key)


async def record_manifest(
    db: AsyncSession,
    run: Any,
    *,
    steps: Sequence[Mapping[str, Any]],
    collection_id: UUID,
    organization_id: UUID,
    actor_id: UUID,
) -> ResearchRunManifest | None:
    """Write the run's one ``nous.run-manifest/2`` (and its artifact rows and
    ``run.manifest_recorded``) in the caller's transaction; never commits.
    ``None`` for a run without an ``analyze`` step. Raises
    ``ManifestSecretDetected`` (after deleting this run's blobs) when the
    manifest or the environment lock carries a credential."""
    found = _analyze_step(steps)
    if found is None:
        return None
    existing = (
        await db.execute(
            select(ResearchRunManifest).where(ResearchRunManifest.run_id == run.id)
        )
    ).scalar_one_or_none()
    if existing is not None:
        return cast(ResearchRunManifest, existing)
    step_index, spec = found
    step = (
        await db.execute(
            select(ResearchStep)
            .where(
                ResearchStep.run_id == run.id,
                ResearchStep.step_index == step_index,
                ResearchStep.is_deleted.is_(False),
            )
            .order_by(ResearchStep.created_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    out: Any = None if step is None else dict(step.output or {}).get("analyze")
    artifacts: list[ResearchRunArtifact] = []
    pieces: dict[str, Any] = {"code": {}, "environment": None}
    if isinstance(out, Mapping) and out.get("status") == "completed":
        artifacts, pieces = _artifact_rows(run, collection_id, organization_id, out)
    else:
        out = {}
    protocol = (
        None
        if run.protocol_version_id is None
        else await db.get(ResearchProtocolVersion, run.protocol_version_id)
    )
    question = (
        None
        if protocol is None or protocol.question_version_id is None
        else await db.get(ResearchQuestionVersion, protocol.question_version_id)
    )
    deviations = await _all(
        db, select(ProtocolDeviation.id).where(ProtocolDeviation.run_id == run.id)
    )
    manifest = rules.build(
        run={
            "run_id": str(run.id),
            "effective_plan_hash": run.effective_plan_hash,
            "blueprint_id": str(run.blueprint_id),
            "blueprint_version": run.blueprint_version,
            "step_index": step_index,
            "started_at": out.get("started_at"),
            "completed_at": out.get("completed_at"),
        },
        protocol=(
            None
            if protocol is None
            else {"id": str(protocol.id), "content_hash": protocol.content_hash}
        ),
        question=(
            None
            if question is None
            else {"id": str(question.id), "hypothesis": question.hypothesis}
        ),
        spec=spec,
        code=pieces["code"],
        environment=pieces["environment"],
        inputs=pieces.get("inputs", []),
        outputs=pieces.get("outputs", []),
        metrics=out.get("metrics"),
        status=str(run.status),
        deviation_ids=[str(value) for value in deviations],
    )
    lock = next((a for a in artifacts if a.role == "environment"), None)
    lock_text = ""
    if lock is not None:
        data = await get_artifact_storage().get(cast(str, lock.storage_key))
        lock_text = (data or b"").decode("utf-8", errors="replace")
    try:
        rules.assert_no_secrets(
            [manifest, lock_text], secret_values=[os.getenv("E2B_API_KEY") or ""]
        )
    except ValueError as error:
        await _forget_blobs(artifacts)
        raise ManifestSecretDetected(str(error)) from None
    state, missing = rules.completeness(manifest)
    await lock_aggregate_stream(
        db,
        collection_id=collection_id,
        aggregate_type=AGGREGATE_TYPE,
        aggregate_id=collection_id,
    )
    for artifact in artifacts:
        db.add(artifact)
    await db.flush()
    row = ResearchRunManifest(
        id=uuid4(),
        run_id=run.id,
        collection_id=collection_id,
        schema_version=rules.SCHEMA_VERSION,
        manifest=manifest,
        manifest_hash=rules.manifest_hash(manifest),
        completeness=state,
        missing=missing,
    )
    db.add(row)
    await db.flush()
    payload = {
        "collection_id": str(collection_id),
        "run_id": str(run.id),
        "manifest_id": str(row.id),
        "manifest_hash": row.manifest_hash,
        "completeness": state,
        "missing": missing,
        "status": str(run.status),
        "output_artifact_ids": [str(a.id) for a in artifacts if a.role == "output"],
    }
    await append_decision(
        db,
        collection_id=collection_id,
        aggregate_type=AGGREGATE_TYPE,
        aggregate_id=collection_id,
        event_type="run.manifest_recorded",
        event_schema_version=1,
        actor_user_id=actor_id,
        actor_role="machine",
        subject_type=SUBJECT_TYPE,
        subject_id=cast(UUID, row.id),
        subject_version_id=None,
        subject_hash=decision_request_fingerprint(payload),
        reason=None,
        payload=payload,
        idempotency_key=f"manifest:{run.id}",
        request_fingerprint=decision_request_fingerprint(payload),
    )
    return row


# --- Reads -----------------------------------------------------------------------


async def _manifest_row(db: AsyncSession, run_id: Any) -> Any:
    return (
        await db.execute(
            select(ResearchRunManifest).where(ResearchRunManifest.run_id == run_id)
        )
    ).scalar_one_or_none()


async def manifest_v2(
    db: AsyncSession, run: Any, legacy: Mapping[str, Any]
) -> RunManifestV2Response:
    """VIEW: the stored manifest, or the legacy view (``legacy`` is the
    unchanged ``GET /runs/{id}/manifest`` body) with nothing synthesized."""
    row = await _manifest_row(db, run.id)
    if row is None:
        return RunManifestV2Response.model_validate(rules.legacy_view(legacy))
    return RunManifestV2Response(
        schema=rules.SCHEMA,
        manifest=row.manifest,
        manifest_hash=row.manifest_hash,
        completeness=row.completeness,
        missing=list(row.missing),
    )


async def manifest_bytes(db: AsyncSession, run: Any) -> tuple[bytes, str]:
    """The exact hashed bytes and ``manifest_hash``; 404 for a legacy run."""
    row = await _manifest_row(db, run.id)
    if row is None:
        raise HTTPException(status_code=404, detail=MANIFEST_NOT_FOUND)
    data = canonical_json_bytes(row.manifest)
    if rules.sha256_hex(data) != row.manifest_hash:
        raise HTTPException(status_code=500, detail=ARTIFACT_CORRUPT)
    return data, cast(str, row.manifest_hash)


async def read_artifact(
    db: AsyncSession, run: Any, artifact_id: UUID
) -> tuple[bytes, ResearchRunArtifact]:
    """One retained file of this run, re-hashed before it leaves; 404 for a
    foreign run's artifact."""
    artifact = (
        await db.execute(
            select(ResearchRunArtifact).where(
                ResearchRunArtifact.id == artifact_id,
                ResearchRunArtifact.run_id == run.id,
            )
        )
    ).scalar_one_or_none()
    if artifact is None:
        raise HTTPException(status_code=404, detail=ARTIFACT_NOT_FOUND)
    data = await get_artifact_storage().get(cast(str, artifact.storage_key))
    if data is None:
        raise HTTPException(status_code=404, detail=ARTIFACT_NOT_FOUND)
    if rules.sha256_hex(data) != artifact.sha256:
        raise HTTPException(status_code=500, detail=ARTIFACT_CORRUPT)
    return data, cast(ResearchRunArtifact, artifact)


async def _figures(db: AsyncSession, collection_id: UUID) -> list[Any]:
    return await _all(
        db,
        select(ResearchFigure)
        .where(ResearchFigure.collection_id == collection_id)
        .order_by(ResearchFigure.created_at, ResearchFigure.id),
    )


def _superseded(rows: Sequence[Any]) -> set[Any]:
    return {r.supersedes_figure_id for r in rows if r.supersedes_figure_id}


def _figure_response(row: Any, superseded: set[Any], stale: set[Any]) -> FigureResponse:
    response = FigureResponse.model_validate(row)
    return response.model_copy(
        update={
            "superseded": row.id in superseded,
            "stale": release_rules.node("figure", row.id) in stale,
        }
    )


async def graph_part(
    db: AsyncSession, collection_id: UUID
) -> tuple[list[Edge], set[release_rules.Node]]:
    """Edges ``run_input -> run``, ``evidence_table -> run`` and
    ``run -> figure``; ``changed`` holds superseded figures and document
    inputs whose current checksum differs or whose document is deleted.
    ``figure -> link`` comes from the link loop in
    ``draft_release_service._graph``, which calls this."""
    manifests = await _all(
        db,
        select(ResearchRunManifest).where(
            ResearchRunManifest.collection_id == collection_id
        ),
    )
    figures = await _figures(db, collection_id)
    if not manifests and not figures:
        return [], set()
    node = release_rules.node
    edges: list[Edge] = []
    pinned: dict[str, set[str]] = {}  # document id -> pinned sha256s
    for manifest in manifests:
        run = node("run", manifest.run_id)
        for item in (manifest.manifest or {}).get("inputs") or []:
            if item.get("kind") == "document":
                value = f"document:{item['ref_id']}:{item['sha256']}"
                edges.append((node("run_input", value), run))
                pinned.setdefault(str(item["ref_id"]), set()).add(str(item["sha256"]))
            else:
                edges.append((node("evidence_table", item["ref_id"]), run))
    for figure in figures:
        edges.append((node("run", figure.run_id), node("figure", figure.id)))
    changed = {node("figure", value) for value in _superseded(figures)}
    if pinned:
        current = {
            str(document_id): (checksum, deleted)
            for document_id, checksum, deleted in (
                await db.execute(
                    select(
                        Document.id, Document.checksum_sha256, Document.is_deleted
                    ).where(Document.id.in_([UUID(v) for v in pinned]))
                )
            ).all()
        }
        for document_id, digests in pinned.items():
            checksum, deleted = current.get(document_id, (None, True))
            for digest in digests:
                if deleted or checksum != digest:
                    changed.add(node("run_input", f"document:{document_id}:{digest}"))
    return edges, changed


async def current_figure(db: AsyncSession, collection_id: UUID, figure_id: UUID) -> Any:
    """An unsuperseded, non-stale figure of this Collection (the only kind a
    claim may cite); 404 for a foreign id, 409 otherwise."""
    rows = await _figures(db, collection_id)
    row = next((r for r in rows if r.id == figure_id), None)
    if row is None:
        raise HTTPException(status_code=404, detail=FIGURE_NOT_FOUND)
    stale = await _stale_nodes(db, collection_id)
    if row.id in _superseded(rows) or release_rules.node("figure", row.id) in stale:
        raise _conflict(FIGURE_NOT_CURRENT)
    return row


async def list_figures(db: AsyncSession, context: ProjectContext) -> FigureListResponse:
    """VIEW: every figure version with its superseded and stale flags."""
    collection_id = _cid(context)
    rows = await _figures(db, collection_id)
    stale = await _stale_nodes(db, collection_id) if rows else set()
    superseded = _superseded(rows)
    return FigureListResponse(
        figures=[_figure_response(row, superseded, stale) for row in rows]
    )


async def lineage(
    db: AsyncSession, context: ProjectContext, figure_id: UUID
) -> FigureLineageResponse:
    """VIEW: output -> run -> code/environment/data -> hypothesis/protocol,
    read from the one manifest the figure names."""
    collection_id = _cid(context)
    rows = await _figures(db, collection_id)
    row = next((r for r in rows if r.id == figure_id), None)
    if row is None:
        raise HTTPException(status_code=404, detail=FIGURE_NOT_FOUND)
    artifact = await db.get(ResearchRunArtifact, row.output_artifact_id)
    manifest_row: Any = await db.get(ResearchRunManifest, row.manifest_id)
    assert artifact is not None and manifest_row is not None  # FK RESTRICT
    manifest = cast(Mapping[str, Any], manifest_row.manifest)
    stale = await _stale_nodes(db, collection_id)
    return FigureLineageResponse(
        figure=_figure_response(row, _superseded(rows), stale),
        output=RunArtifactResponse.model_validate(artifact),
        run_id=row.run_id,
        run_status=str(manifest.get("status")),
        manifest_id=manifest_row.id,
        manifest_hash=manifest_row.manifest_hash,
        completeness=manifest_row.completeness,
        missing=list(manifest_row.missing),
        code=dict(manifest.get("code") or {}),
        environment=manifest.get("environment"),
        inputs=list(manifest.get("inputs") or []),
        parameters=dict(manifest.get("parameters") or {}),
        seed=manifest.get("seed"),
        question_version_id=manifest.get("question_version_id"),
        hypothesis_sha256=manifest.get("hypothesis_sha256"),
        protocol_version_id=manifest.get("protocol_version_id"),
        protocol_content_hash=manifest.get("protocol_content_hash"),
        effective_plan_hash=manifest.get("effective_plan_hash"),
    )


# --- Writes ----------------------------------------------------------------------


async def register_figure(
    db: AsyncSession,
    context: ProjectContext,
    actor_id: UUID,
    data: FigureCreate,
) -> tuple[FigureResponse, bool]:
    """EDIT (the route resolved it): a figure or table record on one exact
    output of a recorded run. Re-registering the key's tip output returns the
    tip and writes nothing; a new output is a successor that stales the
    releases citing its predecessor. Commits once."""
    collection_id = _cid(context)
    stream = await lock_aggregate_stream(
        db,
        collection_id=collection_id,
        aggregate_type=AGGREGATE_TYPE,
        aggregate_id=collection_id,
    )
    key = f"figure:{data.idempotency_key}"
    fingerprint = decision_request_fingerprint(
        {
            "operation": "register_figure",
            "actor_user_id": str(actor_id),
            "request": data.model_dump(mode="json", exclude={"idempotency_key"}),
        }
    )
    replay = await _replayed_event(db, stream, key, fingerprint)
    if replay is not None:
        figure_id = UUID(cast(dict[str, Any], replay.payload)["figure_id"])
        return (
            _figure_response(await db.get(ResearchFigure, figure_id), set(), set()),
            True,
        )
    artifact = (
        await db.execute(
            select(ResearchRunArtifact).where(
                ResearchRunArtifact.id == data.output_artifact_id,
                ResearchRunArtifact.collection_id == collection_id,
            )
        )
    ).scalar_one_or_none()
    if artifact is None:
        raise HTTPException(status_code=404, detail=OUTPUT_NOT_FOUND)
    manifest = await _manifest_row(db, artifact.run_id)
    outputs: list[Any] = (
        [] if manifest is None else list((manifest.manifest or {}).get("outputs") or [])
    )
    declared = next(
        (o for o in outputs if o.get("artifact_id") == str(artifact.id)), None
    )
    if (
        artifact.role != "output"
        or declared is None
        or declared.get("role") not in rules.FIGURE_ROLES
    ):
        raise HTTPException(status_code=422, detail=OUTPUT_NOT_FIGURE)
    assert manifest is not None
    rows = await _figures(db, collection_id)
    superseded = _superseded(rows)
    tip = next(
        (r for r in rows if r.figure_key == data.figure_key and r.id not in superseded),
        None,
    )
    if tip is not None and tip.output_artifact_id == artifact.id:
        return _figure_response(tip, set(), set()), True
    if data.supersedes_figure_id != (None if tip is None else tip.id):
        raise _conflict(FIGURE_STALE)
    row = ResearchFigure(
        id=uuid4(),
        collection_id=collection_id,
        figure_key=data.figure_key,
        kind=data.kind,
        caption=data.caption,
        output_artifact_id=artifact.id,
        run_id=artifact.run_id,
        manifest_id=manifest.id,
        supersedes_figure_id=None if tip is None else tip.id,
        created_by_id=actor_id,
        actor_role="editor",
    )
    db.add(row)
    try:
        await db.flush()
    except IntegrityError as error:
        await db.rollback()
        if _is_unique_violation(error):
            raise _conflict(FIGURE_STALE) from error
        raise
    payload = {
        "collection_id": str(collection_id),
        "figure_id": str(row.id),
        "figure_key": row.figure_key,
        "kind": row.kind,
        "output_artifact_id": str(artifact.id),
        "run_id": str(artifact.run_id),
        "manifest_id": str(manifest.id),
        "supersedes_figure_id": None if tip is None else str(tip.id),
    }
    try:
        result = await append_decision(
            db,
            collection_id=collection_id,
            aggregate_type=AGGREGATE_TYPE,
            aggregate_id=collection_id,
            event_type="figure.registered",
            event_schema_version=1,
            actor_user_id=actor_id,
            actor_role="editor",
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
        raise _conflict("Idempotency conflict") from exc
    if tip is not None:
        # Local import: draft_release_service imports this module lazily.
        from src.services.research import draft_release_service

        await draft_release_service.invalidate_dependents(
            db,
            collection_id=collection_id,
            changed={release_rules.node("figure", tip.id)},
            actor_id=actor_id,
            actor_role="editor",
            cause={
                "family": AGGREGATE_TYPE,
                "event_id": str(result.event.id),
                "kind": "figure.registered",
            },
        )
    await db.commit()
    await db.refresh(row)
    return _figure_response(row, set(), set()), False
