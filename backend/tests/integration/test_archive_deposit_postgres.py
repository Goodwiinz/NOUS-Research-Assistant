"""Real PostgreSQL proof for GOO-318 resumable archive deposits.

Runs on GOO-301's ``screening_factory`` schema, whose migration chain now
ends at ``b0e2a4c6d8f9``: the approvals and attempts tables, their CHECKs and
partial unique indexes, the outbox and the insert-only triggers come from
the migration. The remote side is a fake Zenodo served through
``httpx.MockTransport`` behind the real ``ZenodoAdapter`` (real request and
response shapes, a state machine that can time out after acting or crash
mid-upload). The fake cannot close the ticket: the live sandbox run is NOT
RUN without ``ZENODO_SANDBOX_TOKEN``.

Seeds: owner O (no decision role), adjudicator J (``A`` in the shared
seed), supervisor S, foreign user F; verified releases are inserted directly
(candidate + verified rows over one real package zip with a sealed
``references.json`` and ``statements.json``), so this proof is about the
deposit, not about GOO-315's promotion.

Mutation verification (docs/engineering/testing.md), full record in
``docs/testing/agent-orchestration-mutation-checks.md`` (GOO-318 section):
the draft lookup before create skipped (step 4: two drafts), a timeout
recorded ``failed`` (step 4: not ambiguous), the worker's approval recheck
skipped (step 6: publish after revocation), the self-approval guard dropped
(step 2), ``check_readback`` returning ``[]`` (step 8: verified), ``redact``
as identity (step 10: token retained), ``uq_deposit_live`` dropped (step 3:
two operations).

Command (from ``backend/``): ``RESEARCH_DECISION_DATABASE_URL=... pytest -q
tests/integration/test_archive_deposit_postgres.py``.
"""

import asyncio
import hashlib
import importlib.util
import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Awaitable, Callable, TypeAlias, TypeVar, cast
from uuid import UUID, uuid4

import httpx
import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from fastapi import HTTPException
from pydantic import SecretStr
from sqlalchemy import text
from sqlalchemy.engine import Connection, make_url
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from src.core.config import settings
from src.models.base import Base
from src.models.draft_release import DraftRelease
from src.models.generated_draft import GeneratedDraft
from src.models.manuscript_release import ManuscriptRelease
from src.models.research_project_role import (
    ResearchProjectRole,
    ResearchProjectRoleAssignment,
)
from src.models.user import User
from src.models.workspace import WorkspaceMember, WorkspaceRole
from src.services.artifacts.storage import LocalArtifactStorage
from src.services.research import deposit_service as svc
from src.services.research import manuscript_release_service as mr
from src.services.research import manuscript_rules, venue_rules
from src.services.research.archives.zenodo import ZenodoAdapter
from src.services.research_decisions import replay_decisions
from src.services.research_engine import audit_bundle
from src.services.research_engine.project_access import (
    ProjectContext,
    ResearchAction,
    resolve_project,
)
from src.shared.deposit_schemas import (
    DepositApprovalCreate,
    DepositApprovalRevoke,
    DepositCreate,
    DepositResponse,
)
from tests.integration.test_report_identity_postgres import _seed
from tests.integration.test_screening_queue_postgres import (  # noqa: F401
    _REBUILT_TABLES,
    VERSIONS,
    _upgrade,
    screening_factory,
)

pytestmark = [pytest.mark.integration, pytest.mark.requires_postgres]

Factory: TypeAlias = async_sessionmaker[AsyncSession]
T = TypeVar("T")
VIEW, RELEASE = ResearchAction.VIEW, ResearchAction.RELEASE
BASE = "https://sandbox.zenodo.org/api"
# Built by concatenation so no token-shaped literal is committed.
TOKEN = "zen" + "odo-" + uuid4().hex
ACCOUNT = "zenodo_sandbox:nous-fixture"
REFERENCES = [
    {
        "key": "doc1",
        "type": "journal_article",
        "title": "Imaging of cell membranes",
        "authors": ["Ada Lovelace"],
        "year": 2021,
        "venue": "Journal of Microscopy",
        "doi": "10.1000/jm.2021.001",
        "arxiv_id": None,
        "source": "citation",
    }
]
STATEMENTS = {
    "statement_set_id": str(uuid4()),
    "set_hash": "c" * 64,
    "schema": venue_rules.STATEMENTS_SCHEMA,
    "credit_vocabulary": venue_rules.CREDIT_VOCABULARY,
    "statements": {
        "authors": [
            {
                "author_key": "a",
                "order": 1,
                "display_name": "Imogen Vasquez-Thorne",
                "affiliations": ["Fenwick Institute of Hydrology"],
                "orcid": "0000-0002-1825-0097",
            }
        ],
        "licenses": {"text": "CC-BY-4.0", "data": None, "code": None},
    },
    "approvals": [{"author_key": "a", "method": "in_app_self"}],
}


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _md5(data: bytes) -> str:
    return hashlib.md5(data, usedforsecurity=False).hexdigest()


class FakeZenodo:
    """The Zenodo deposition API as a state machine (real JSON shapes)."""

    def __init__(self) -> None:
        self.depositions: dict[int, dict[str, Any]] = {}
        self.stored: dict[int, dict[str, bytes]] = {}
        self.puts: list[str] = []
        self.publishes = 0
        self.authorization: set[str] = set()
        self.timeout_after_create = False
        self.crash_on_put: int | None = None
        self.corrupt: set[str] = set()
        self._next = 100

    def _view(self, dep_id: int, echo: str = "") -> dict[str, Any]:
        """``echo``: a misconfigured server echoing the caller's token in a
        link query string, which ``redact`` must strip before retention."""
        dep = self.depositions[dep_id]
        links = dict(dep["links"])
        if echo:
            links["self"] = f"{BASE}/deposit/depositions/{dep_id}?access_token={echo}"
        files = [
            {
                "id": name,
                "filename": name,
                "filesize": len(data),
                "checksum": _md5(data),
            }
            for name, data in self.stored[dep_id].items()
        ]
        return {**dep, "links": links, "files": files}

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.authorization.add(request.headers.get("Authorization", ""))
        echo = request.headers.get("Authorization", "").removeprefix("Bearer ")
        path = request.url.path.removeprefix("/api")
        parts = path.strip("/").split("/")
        if request.method == "POST" and path == "/deposit/depositions":
            self._next += 1
            dep_id = self._next
            notes = json.loads(request.content)["metadata"]["notes"]
            self.depositions[dep_id] = {
                "id": dep_id,
                "record_id": dep_id,
                "state": "unsubmitted",
                "submitted": False,
                "doi": "",
                "links": {
                    "bucket": f"{BASE}/files/bucket-{dep_id}",
                    "html": f"https://sandbox.zenodo.org/deposit/{dep_id}",
                },
                "metadata": {
                    **json.loads(request.content)["metadata"],
                    "notes": notes,
                    "prereserve_doi": {"doi": f"10.5072/zenodo.{dep_id}"},
                },
            }
            self.stored[dep_id] = {}
            if self.timeout_after_create:
                self.timeout_after_create = False
                raise httpx.ReadTimeout("acted, then timed out", request=request)
            return httpx.Response(201, json=self._view(dep_id, echo))
        if request.method == "GET" and path == "/deposit/depositions":
            newest = sorted(self.depositions, reverse=True)
            return httpx.Response(200, json=[self._view(d, echo) for d in newest])
        if parts[:2] == ["deposit", "depositions"] and len(parts) >= 3:
            dep_id = int(parts[2])
            if dep_id not in self.depositions:
                return httpx.Response(404, json={"message": "not found"})
            if request.method == "POST" and parts[3:] == ["actions", "publish"]:
                self.publishes += 1
                dep = self.depositions[dep_id]
                dep.update(
                    state="done",
                    submitted=True,
                    doi=dep["metadata"]["prereserve_doi"]["doi"],
                )
                return httpx.Response(202, json=self._view(dep_id, echo))
            return httpx.Response(200, json=self._view(dep_id, echo))
        if request.method == "PUT" and parts[0] == "files":
            if self.crash_on_put is not None:
                self.crash_on_put -= 1
                if self.crash_on_put == 0:
                    self.crash_on_put = None
                    raise RuntimeError("worker crashed mid-upload")
            dep_id = int(parts[1].removeprefix("bucket-"))
            name = parts[2]
            self.stored[dep_id][name] = request.content
            self.puts.append(name)
            return httpx.Response(
                201,
                json={
                    "key": name,
                    "size": len(request.content),
                    "checksum": f"md5:{_md5(request.content)}",
                },
            )
        if request.method == "GET" and parts[0] == "records":
            dep_id = int(parts[1])
            dep = self.depositions[dep_id]
            files = [
                {
                    "key": name,
                    "size": len(data),
                    "checksum": "md5:"
                    + (("0" * 32) if name in self.corrupt else _md5(data)),
                }
                for name, data in self.stored[dep_id].items()
            ]
            return httpx.Response(
                200, json={"id": dep_id, "doi": dep["doi"], "files": files}
            )
        return httpx.Response(400, json={"message": f"unexpected {path}"})


async def _as(
    w: Any,
    user: str,
    action: ResearchAction,
    call: Callable[[AsyncSession, ProjectContext], Awaitable[T]],
) -> T:
    """One route-shaped request: resolve, then the service (which commits)."""
    async with w.factory() as db:
        context = await resolve_project(db, w.p1, w.ids[user], action)
        return await call(db, context)


async def _refused(awaitable: Awaitable[Any], status: int, detail: str = "") -> None:
    with pytest.raises(HTTPException) as error:
        await awaitable
    assert error.value.status_code == status, error.value.detail
    assert detail in str(error.value.detail)


async def _scalar(factory: Factory, sql: str, **params: Any) -> Any:
    async with factory() as db:
        return (await db.execute(text(sql), params)).scalar_one()


async def _seed_release(w: Any, tag: str) -> Any:
    """One verified release (candidate + verified rows) over a real package."""
    snapshot = {
        "schema": manuscript_rules.SNAPSHOT_SCHEMA,
        "draft": {"title": f"Deposit fixture {tag}"},
        "references": REFERENCES,
    }
    csl = json.loads(mr.reference_file(snapshot, "csl-json")[0])
    manuscript = f"# Deposit fixture {tag}\n".encode()
    parts = [
        audit_bundle.Part("manuscript.md", None, manuscript, _sha(manuscript)),
        audit_bundle._sealed_part(
            "references.json", mr.REFERENCES_CSL_SCHEMA, csl, False
        ),
        audit_bundle._sealed_part(
            "statements.json", venue_rules.STATEMENTS_PART_SCHEMA, STATEMENTS, False
        ),
    ]
    package, _ = audit_bundle.write_zip(
        parts,
        project_id=str(w.p1),
        generated_at="2026-10-01T00:00:00+00:00",
        deployment_sha=None,
        protocol_version_id=None,
        stream_heads={},
        schema=manuscript_rules.PACKAGE_SCHEMA,
    )
    checks = {
        k: {"state": "pass", "items": []} for k in manuscript_rules.VERIFIED_OBLIGATIONS
    }
    async with w.factory() as db:
        draft = GeneratedDraft(
            project_id=w.p1,
            version=1,
            title=f"Draft {tag}",
            content=tag,
            is_current=True,
        )
        db.add(draft)
        await db.flush()
        draft_release = DraftRelease(
            collection_id=w.p1,
            draft_id=draft.id,
            draft_version=1,
            content_hash=_sha(tag.encode()),
            claim_version_ids=[],
            assessment_ids=[],
            interpretation_claim_version_ids=[],
            policy_version=1,
            promoted_by_id=w.ids["A"],
            actor_role="adjudicator",
        )
        db.add(draft_release)
        await db.flush()
        rows: list[ManuscriptRelease] = []
        for stage in ("candidate", "verified"):
            candidate = None if stage == "candidate" else rows[0]
            release_id = uuid4()
            key = mr.storage_key(w.ids["org"], release_id)
            await w.storage.put(key, package, "application/zip")
            row = ManuscriptRelease(
                id=release_id,
                collection_id=w.p1,
                draft_id=draft.id,
                draft_version=1,
                content_hash=_sha(tag.encode()),
                stage=stage,
                candidate_release_id=None if candidate is None else candidate.id,
                draft_release_id=None if candidate is None else draft_release.id,
                snapshot=snapshot,
                snapshot_hash=manuscript_rules.snapshot_hash(snapshot),
                checks=checks,
                checks_hash="d" * 64,
                package_files=mr.package_files(package),
                package_sha256=_sha(package),
                package_storage_key=key,
                created_by_id=w.ids["O" if stage == "candidate" else "A"],
                actor_role="editor" if stage == "candidate" else "adjudicator",
            )
            db.add(row)
            await db.flush()
            rows.append(row)
        await db.commit()
    return SimpleNamespace(
        id=rows[1].id,
        sha=_sha(package),
        package=package,
        csl=mr.reference_file(snapshot, "csl-json")[0].encode(),
    )


async def _seed_supervisor(w: Any) -> None:
    w.ids["S"] = uuid4()
    async with w.factory() as db:
        db.add(
            User(
                id=w.ids["S"],
                email=f"{w.ids['S']}@test.invalid",
                password_hash="unused",
                first_name="Test",
                last_name="S",
                organization_id=w.ids["org"],
            )
        )
        await db.flush()
        db.add(
            WorkspaceMember(
                workspace_id=w.ids["workspace"],
                user_id=w.ids["S"],
                role=WorkspaceRole.VIEWER,
            )
        )
        db.add(
            ResearchProjectRoleAssignment(
                collection_id=w.p1,
                user_id=w.ids["S"],
                role=ResearchProjectRole.SUPERVISOR,
                assigned_by_id=w.ids["O"],
            )
        )
        await db.commit()


def _approve(w: Any, user: str, release: Any, key: str) -> Awaitable[Any]:
    data = DepositApprovalCreate(
        release_id=release.id,
        package_sha256=release.sha,
        rationale=f"Approved for the Zenodo sandbox ({key})",
        idempotency_key=key,
    )
    return _as(
        w, user, RELEASE, lambda db, ctx: svc.approve(db, ctx, w.ids[user], data)
    )


def _request(w: Any, user: str, release: Any, key: str) -> Awaitable[Any]:
    data = DepositCreate(
        release_id=release.id, package_sha256=release.sha, idempotency_key=key
    )
    return _as(
        w,
        user,
        RELEASE,
        lambda db, ctx: svc.request_deposit(db, ctx, w.ids[user], data),
    )


async def _listing(w: Any, user: str = "S") -> Any:
    return await _as(w, user, VIEW, lambda db, ctx: svc.list_deposits(db, ctx))


async def _deposit(w: Any, operation_id: UUID) -> DepositResponse:
    listing = await _listing(w)
    return cast(
        DepositResponse,
        next(d for d in listing.deposits if d.operation_id == operation_id),
    )


async def _drain(w: Any, now: datetime | None = None) -> int:
    async with w.factory() as db:
        return await svc.drain(db, w.adapter, now=now)


async def _outbox(w: Any, operation_id: UUID) -> str:
    return cast(
        str,
        await _scalar(
            w.factory,
            "SELECT status FROM archive_deposit_outbox WHERE operation_id = :o",
            o=operation_id,
        ),
    )


async def _downgrade_on_fresh_schema() -> None:
    """Step 14 on an empty schema: only the three tables drop; the GOO-309
    trigger function and the manuscript releases stay."""
    configured = os.environ["RESEARCH_DECISION_DATABASE_URL"]
    url = make_url(configured).set(drivername="postgresql+asyncpg")
    schema = f"test_deposit_down_{uuid4().hex}"
    admin = create_async_engine(url)
    async with admin.begin() as connection:
        await connection.exec_driver_sql(f'CREATE SCHEMA "{schema}"')
    engine = create_async_engine(
        url, connect_args={"server_settings": {"search_path": schema}}
    )

    def downgrade(connection: Connection) -> None:
        spec = importlib.util.spec_from_file_location(
            "deposit_migration", VERSIONS / "b0e2a4c6d8f9_create_archive_deposits.py"
        )
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        setattr(module, "op", Operations(MigrationContext.configure(connection)))
        module.downgrade()

    try:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
            for table in _REBUILT_TABLES:
                await connection.exec_driver_sql(f'DROP TABLE "{table}"')
            await connection.run_sync(_upgrade)
            await connection.run_sync(downgrade)
            tables = {
                row[0]
                for row in (
                    await connection.exec_driver_sql(
                        "SELECT tablename FROM pg_tables WHERE schemaname = "
                        f"'{schema}'"
                    )
                ).all()
            }
            assert not {t for t in tables if t.startswith("archive_deposit")}
            assert "manuscript_releases" in tables
            function = await connection.exec_driver_sql(
                "SELECT count(*) FROM pg_proc WHERE proname = "
                "'prevent_research_insert_only_mutation'"
            )
            assert function.scalar_one() >= 1
    finally:
        await engine.dispose()
        async with admin.begin() as connection:
            await connection.exec_driver_sql(f'DROP SCHEMA "{schema}" CASCADE')
        await admin.dispose()


async def test_deposit_resumes_reconciles_and_never_duplicates(
    screening_factory: Factory,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    factory = screening_factory
    storage = LocalArtifactStorage(tmp_path / "artifacts")
    monkeypatch.setattr(mr, "get_artifact_storage", lambda: storage)
    monkeypatch.setattr(settings, "ZENODO_SANDBOX_TOKEN", SecretStr(TOKEN))
    monkeypatch.setattr(settings, "ZENODO_ACCOUNT_LABEL", "nous-fixture")
    fake = FakeZenodo()
    ids = await _seed(factory)
    w = SimpleNamespace(
        factory=factory,
        ids=ids,
        p1=ids["collection"],
        storage=storage,
        adapter=ZenodoAdapter(
            BASE, SecretStr(TOKEN), transport=httpx.MockTransport(fake)
        ),
    )

    # 1. Seed: verified V1; supervisor S, adjudicator J (= A), owner O, foreign F.
    await _seed_supervisor(w)
    v1 = await _seed_release(w, "v1")

    # 2. Authorization: no approval; self-approval; J approves; O and F refused.
    await _refused(_request(w, "S", v1, "early"), 409, svc.NO_APPROVAL)
    own, created = await _approve(w, "S", v1, "approve-s")
    assert created is False and own.in_force and own.account_ref == ACCOUNT
    await _refused(_request(w, "S", v1, "self"), 422, svc.SELF_APPROVAL)
    approval, _ = await _approve(w, "A", v1, "approve-j-1")
    assert approval.in_force and approval.package_sha256 == v1.sha
    await _refused(_request(w, "O", v1, "owner"), 403)
    await _refused(_request(w, "F", v1, "foreign"), 404)
    await _refused(_approve(w, "O", v1, "owner-approves"), 403)

    # 3. Concurrency: two requests with different keys -> one operation.
    (first, r1), (second, r2) = await asyncio.gather(
        _request(w, "S", v1, "k1"), _request(w, "S", v1, "k2")
    )
    assert first.operation_id == second.operation_id
    assert sorted([r1, r2]) == [False, True]
    op = first.operation_id
    assert first.status == "prepared" and first.approval_in_force
    assert {f.name for f in first.files} == {"package.zip", "references.csl.json"}
    assert {f.name: f.sha256 for f in first.files}["package.zip"] == v1.sha
    assert await _scalar(factory, "SELECT count(*) FROM archive_deposit_attempts") == 1
    assert await _outbox(w, op) == "pending"
    replay, replayed = await _request(w, "S", v1, "k1")
    assert replayed and replay.operation_id == op
    changed = DepositCreate(
        release_id=v1.id, package_sha256="f" * 64, idempotency_key="k1"
    )
    await _refused(
        _as(
            w,
            "S",
            RELEASE,
            lambda db, ctx: svc.request_deposit(db, ctx, w.ids["S"], changed),
        ),
        409,
        "Idempotency conflict",
    )
    # The requester can no longer approve this release either.
    await _refused(_approve(w, "S", v1, "approve-s-late"), 422, svc.SELF_APPROVAL)

    # 4. Timeout after remote success: unknown, then reconcile by marker.
    fake.timeout_after_create = True
    assert await _drain(w) == 1
    state = await _deposit(w, op)
    assert state.status == "ambiguous" and state.attempts[-1].outcome == "unknown"
    assert state.attempts[-1].phase == "draft_created" and state.doi is None
    assert len(fake.depositions) == 1
    assert await _drain(w) == 1
    assert len(fake.depositions) == 1, "a retry created a second deposition"
    state = await _deposit(w, op)
    assert state.status == "draft_created"
    assert state.attempts[-1].reason == svc.RECONCILED
    (dep_id,) = fake.depositions
    assert state.remote_deposition_id == str(dep_id)

    # 5. Crash after file 1 uploaded, before the local commit.
    fake.crash_on_put = 2
    assert await _drain(w) == 0
    assert await _outbox(w, op) == "processing"
    assert (await _deposit(w, op)).status == "draft_created"
    assert fake.puts == ["package.zip"]
    assert await _drain(w) == 0  # the claim is not stale yet
    later = datetime.now(timezone.utc) + svc.STALE_CLAIM + timedelta(seconds=5)
    assert await _drain(w, now=later) == 1
    assert fake.puts == ["package.zip", "references.csl.json"], "a file re-uploaded"
    state = await _deposit(w, op)
    assert state.status == "files_uploaded" and state.doi is None
    assert fake.publishes == 0

    # 6. A revoked approval blocks publish before any remote call.
    await _as(
        w,
        "A",
        RELEASE,
        lambda db, ctx: svc.revoke(
            db,
            ctx,
            w.ids["A"],
            approval.id,
            DepositApprovalRevoke(rationale="Hold the DOI", idempotency_key="rev-1"),
        ),
    )
    calls_before = len(fake.puts), fake.publishes
    assert await _drain(w) == 1
    state = await _deposit(w, op)
    assert state.last_reason == svc.APPROVAL_INVALID and not state.approval_in_force
    assert state.status == "files_uploaded"
    assert (len(fake.puts), fake.publishes) == calls_before
    assert await _outbox(w, op) == "done"
    assert await _drain(w) == 0
    await _approve(w, "A", v1, "approve-j-2")
    await _as(w, "S", RELEASE, lambda db, ctx: svc.requeue(db, ctx, op))
    assert await _outbox(w, op) == "pending"

    # 7. Publish + verify in one pass; the receipt matches the release.
    assert await _drain(w) == 1
    state = await _deposit(w, op)
    assert state.status == "verified" and fake.publishes == 1
    assert [a.phase for a in state.attempts[-2:]] == ["published", "verified"]
    assert state.doi == f"10.5072/zenodo.{dep_id}"
    assert state.doi_url == f"https://doi.org/10.5072/zenodo.{dep_id}"
    assert state.remote_record_id == str(dep_id)
    assert await _outbox(w, op) == "done"
    remote = {name: _md5(data) for name, data in fake.stored[dep_id].items()}
    assert remote == {f.name: f.md5 for f in state.files}
    assert remote == {
        "package.zip": _md5(v1.package),
        "references.csl.json": _md5(v1.csl),
    }
    sent = json.loads(
        await _scalar(
            factory,
            "SELECT request::text FROM archive_deposit_attempts WHERE id = :o",
            o=op,
        )
    )
    assert sent["metadata"]["creators"][0]["orcid"] == "0000-0002-1825-0097"
    assert sent["metadata"]["license"] == "cc-by-4.0"
    assert fake.depositions[dep_id]["metadata"]["license"] == "cc-by-4.0"
    releases = await _as(w, "S", VIEW, lambda db, ctx: mr.list_releases(db, ctx))
    by_id = {r.id: r for r in releases.releases}
    assert by_id[v1.id].external_submission == "authorized"
    assert all(
        r.external_submission == "not_authorized"
        for r in releases.releases
        if r.id != v1.id
    )

    # 8. Read-back mismatch on V2: failed, never verified, no DOI.
    v2 = await _seed_release(w, "v2")
    await _approve(w, "A", v2, "approve-v2")
    op2 = (await _request(w, "S", v2, "req-v2"))[0].operation_id
    fake.corrupt = {"package.zip"}
    for _ in range(3):
        await _drain(w)
    state = await _deposit(w, op2)
    assert state.status == "failed" and state.doi is None
    assert state.attempts[-1].mismatch == ["checksum:package.zip"]
    assert state.last_reason == svc.READBACK_MISMATCH
    assert await _outbox(w, op2) == "done"
    fake.corrupt = set()

    # 9. A new release has no approval; V1's never carries over.
    v3 = await _seed_release(w, "v3")
    await _refused(_request(w, "S", v3, "req-v3"), 409, svc.NO_APPROVAL)

    # 10. The token is retained nowhere (the fake echoed it in link query
    # strings); it was sent as a header only.
    assert fake.authorization == {f"Bearer {TOKEN}"}
    for sql in (
        "SELECT coalesce(string_agg(t::text, ''), '') FROM archive_deposit_attempts t",
        "SELECT coalesce(string_agg(t::text, ''), '') FROM archive_deposit_approvals t",
        "SELECT coalesce(string_agg(payload::text, ''), '') FROM research_decision_events",
    ):
        assert TOKEN not in await _scalar(factory, sql)
    exported = await _as(w, "S", VIEW, lambda db, ctx: svc.export_part(db, ctx))
    assert TOKEN not in json.dumps(exported) and exported["deposits"]

    # 11. Insert-only evidence; the outbox is disposable.
    for statement in (
        "UPDATE archive_deposit_attempts SET reason = 'x'",
        "DELETE FROM archive_deposit_attempts",
        "UPDATE archive_deposit_approvals SET rationale = 'x'",
        "DELETE FROM archive_deposit_approvals",
    ):
        async with factory() as db:
            with pytest.raises(DBAPIError) as blocked:
                await db.execute(text(statement))
            assert getattr(blocked.value.orig, "sqlstate", None) == "55000"
    attempts = await _scalar(factory, "SELECT count(*) FROM archive_deposit_attempts")
    async with factory() as db:
        await db.execute(text("DELETE FROM archive_deposit_outbox"))
        await db.commit()
    rebuilt = await _as(w, "S", RELEASE, lambda db, ctx: svc.requeue(db, ctx, op))
    assert rebuilt.status == "verified" and await _outbox(w, op) == "pending"
    assert await _drain(w) == 0
    assert await _outbox(w, op) == "done"
    assert await _scalar(factory, "SELECT count(*) FROM archive_deposit_attempts") == (
        attempts
    )

    # 12. Archived: writes 409, reads 200, the worker records project_unavailable.
    await _approve(w, "A", v3, "approve-v3")
    op3 = (await _request(w, "S", v3, "req-v3-ok"))[0].operation_id
    deposits_before = len(fake.depositions)
    async with factory() as db:
        await db.execute(
            text("UPDATE workspaces SET is_archived = true WHERE id = :w"),
            {"w": ids["workspace"]},
        )
        await db.commit()
    await _refused(_request(w, "S", v3, "archived"), 409)
    assert len((await _listing(w)).deposits) == 3
    assert await _drain(w) == 1
    state = await _deposit(w, op3)
    assert state.last_reason == svc.PROJECT_UNAVAILABLE
    assert len(fake.depositions) == deposits_before

    # 13. The research_deposit stream replays.
    async with factory() as db:
        events = await replay_decisions(
            db,
            collection_id=w.p1,
            aggregate_type=svc.AGGREGATE_TYPE,
            aggregate_id=w.p1,
        )
    kinds = [str(e.event_type) for e in events]
    assert kinds.count("deposit.requested") == 3
    assert "deposit.revoked" in kinds and "deposit.phase_recorded" in kinds

    async with factory() as db:
        await db.execute(
            text("UPDATE workspaces SET is_deleted = true WHERE id = :w"),
            {"w": ids["workspace"]},
        )
        await db.commit()
    await _refused(_listing(w), 404)

    # 14. Downgrade refuses retained rows; on an empty schema only the three
    # tables drop and the GOO-309 function stays.
    async with factory() as db:
        connection = await db.connection()

        def refuse(sync: Connection) -> None:
            spec = importlib.util.spec_from_file_location(
                "deposit_migration_refuse",
                VERSIONS / "b0e2a4c6d8f9_create_archive_deposits.py",
            )
            assert spec is not None and spec.loader is not None
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            setattr(module, "op", Operations(MigrationContext.configure(sync)))
            with pytest.raises(RuntimeError, match="refusing to drop"):
                module.downgrade()

        await connection.run_sync(refuse)
        await db.rollback()
    if os.getenv("RESEARCH_DECISION_DATABASE_URL"):
        await _downgrade_on_fresh_schema()
