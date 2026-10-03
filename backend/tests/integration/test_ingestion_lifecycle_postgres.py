"""GOO-360: actual HTTP, PostgreSQL, Redis and canonical Celery worker lifecycle.

Requires disposable INGESTION_TEST_DATABASE_URL, INGESTION_TEST_REDIS_URL and
the repository-pinned en_core_web_sm model. Each test owns a schema and queue.
Only Neo4j driver, Spaces and DO HTTP transports are replaced. Text parsing,
entity extraction, authentication, locking, commits and full-text search run.
Linux fork/solo workers provide deterministic process-loss and provider barriers;
this is not verification of a deployed prefork worker or provider server.
"""

from __future__ import annotations

import asyncio
import multiprocessing
import os
import queue
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any, AsyncIterator, Iterator, cast
from uuid import UUID, uuid4

import pytest
import redis
import spacy
from celery.app.amqp import AMQP
from celery.signals import task_postrun, worker_ready
from fastapi import FastAPI
from fastapi.testclient import TestClient
from jose import jwt
from sqlalchemy.orm import Session
from tests.unit.tasks.test_ingestion_stage_guard_postgres import _ingestion

from src.api.documents import documents, files
from src.api.search import search
from src.core import database
from src.core.config import settings
from src.models.document import Document, ProcessingStatus
from src.models.entity import Entity
from src.models.organization import Organization
from src.models.processing import JobStatus, ProcessingJob
from src.models.search_analytics import SearchAnalyticsEvent
from src.models.user import User, UserRole
from src.services.do_kb import client as kb_client
from src.services.do_kb import ingest
from src.services.knowledge_graph.knowledge_graph_service import KnowledgeGraphService
from src.services.processing.processing_service import ProcessingPipeline
from src.tasks import processing_tasks as pt
from src.tasks import reconcile_tasks as rt
from src.tasks.celery_app import celery_app

pytestmark = [
    pytest.mark.integration,
    pytest.mark.requires_postgres,
    pytest.mark.requires_redis,
]

TEXT = "Ada Lovelace studied analytical engines in London. Lifecycleproof astronomy."


class _Release:
    """A pipe remains usable if a blocked worker is killed; Event.wait does not."""

    def __init__(self, context: Any) -> None:
        self.reader, self.writer = context.Pipe(duplex=False)

    def wait(self, timeout: float) -> bool:
        if not self.reader.poll(timeout):
            return False
        self.reader.recv_bytes()
        return True

    def set(self) -> None:
        self.writer.send_bytes(b"continue")

    def close(self) -> None:
        self.reader.close()
        self.writer.close()


class _Providers:
    def __init__(self, manager: Any, context: Any) -> None:
        self.graph = manager.dict()
        self.sources = manager.dict()
        self.objects = manager.dict()
        self.controls = manager.dict(block="", cleanup_fail=False)
        self.entered = context.Event()
        self.release = _Release(context)

    def barrier(self, stage: str) -> None:
        if self.controls["block"] == stage:
            self.controls["block"] = ""
            self.entered.set()
            assert self.release.wait(45), "provider barrier was not released"


class _GraphTransport:
    def __init__(self, providers: _Providers) -> None:
        self.providers = providers

    def __enter__(self) -> _GraphTransport:
        return self

    def __exit__(self, *args: Any) -> None:
        return None

    def begin_transaction(self) -> _GraphTransport:
        return self

    def run(self, cypher: str, params: dict[str, Any]) -> SimpleNamespace:
        if "MERGE (e:Entity:" in cypher:
            self.providers.barrier("graph")
            key = (params["org_key"], params["entity_type"], params["canonical_key"])
            existing = self.providers.graph.get(key)
            if existing is None:
                self.providers.graph[key] = dict(params)
                existing = params
            record = {"resolved_id": existing["id"]}
            self.providers.barrier("graph_accepted")
        elif "DELETE " in cypher:
            assert params.get("organization_id"), "cleanup must carry tenant scope"
            assert params.get("source_document_id"), "cleanup must carry document scope"
            if self.providers.controls["cleanup_fail"]:
                raise OSError("synthetic provider outage")
            count = 0
            if "DELETE e" in cypher:
                for key, node in list(self.providers.graph.items()):
                    if (
                        node["org_key"] == params["organization_id"]
                        and node["source_document_id"] == params["source_document_id"]
                    ):
                        del self.providers.graph[key]
                        count += 1
            record = {"deleted_count": count}
        else:
            raise AssertionError(f"Unexpected graph transport operation: {cypher[:80]}")
        return SimpleNamespace(single=lambda: record)


class _KBTransport:
    def __init__(self, providers: _Providers) -> None:
        self.providers = providers

    async def list_data_sources(self, *, kb_uuid: str) -> list[dict[str, Any]]:
        return [
            source
            for source in self.providers.sources.values()
            if source["kb_uuid"] == kb_uuid
        ]

    async def add_spaces_data_source(
        self, *, kb_uuid: str, bucket: str, key: str
    ) -> SimpleNamespace:
        self.providers.barrier("do_kb")
        source_id = str(uuid4())
        self.providers.sources[source_id] = {
            "uuid": source_id,
            "kb_uuid": kb_uuid,
            "spaces_data_source": {"item_path": key},
        }
        self.providers.barrier("do_kb_accepted")
        return SimpleNamespace(uuid=source_id)

    async def start_indexing(self, *, kb_uuid: str) -> None:
        return None

    async def delete_data_source(self, *, kb_uuid: str, ds_uuid: str) -> None:
        source = self.providers.sources.get(ds_uuid)
        if source is not None:
            assert source["kb_uuid"] == kb_uuid
            del self.providers.sources[ds_uuid]


def _worker_main(env: SimpleNamespace, ready: Any, done: Any, log_path: Path) -> None:
    with log_path.open("w") as log:
        sys.stdout = sys.stderr = log
        env.Session.kw["bind"].dispose(close=False)

        def on_ready(**kwargs: Any) -> None:
            ready.set()

        def on_done(task_id: str, retval: Any, state: str, **kwargs: Any) -> None:
            done.put({"id": task_id, "result": retval, "state": state})

        worker_ready.connect(on_ready, weak=False)
        task_postrun.connect(on_done, weak=False)
        celery_app.Worker(
            pool="solo",
            concurrency=1,
            queues=[env.queue],
            without_mingle=True,
            without_gossip=True,
            without_heartbeat=True,
            loglevel="WARNING",
            hostname=f"ingestion-test-{uuid4().hex}@local",
        ).start()


class _Lifecycle:
    def __init__(self, env: SimpleNamespace, context: Any, tmp_path: Path) -> None:
        self.env, self.context, self.tmp_path = env, context, tmp_path
        self.ready, self.done = context.Event(), context.Queue()
        self.workers: list[Any] = []

    def start(self) -> Any:
        self.ready.clear()
        worker = self.context.Process(
            target=_worker_main,
            args=(self.env, self.ready, self.done, self.tmp_path / "worker.log"),
        )
        worker.start()
        self.workers.append(worker)
        assert self.ready.wait(45), self.diagnostics()
        return worker

    def diagnostics(self) -> str:
        log = self.tmp_path / "worker.log"
        return log.read_text()[-8000:] if log.exists() else "worker did not start"

    def completion(self) -> dict[str, Any]:
        try:
            result = self.done.get(timeout=45)
        except queue.Empty:
            pytest.fail(self.diagnostics())
        assert result["state"] == "SUCCESS", result
        return cast(dict[str, Any], result["result"])

    def close(self) -> None:
        for worker in self.workers:
            if worker.is_alive():
                worker.terminate()
                worker.join(10)
            if worker.is_alive():
                worker.kill()
                worker.join(10)
        self.done.close()
        self.done.join_thread()


@pytest.fixture
def lifecycle(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> Iterator[SimpleNamespace]:
    broker = os.getenv("INGESTION_TEST_REDIS_URL")
    dsn = os.getenv("INGESTION_TEST_DATABASE_URL")
    if not broker or not dsn:
        pytest.skip("disposable INGESTION_TEST_DATABASE_URL and REDIS_URL required")
    if "fork" not in multiprocessing.get_all_start_methods():
        pytest.skip("lifecycle process-loss fixture requires Linux fork")
    if not spacy.util.is_package("en_core_web_sm"):
        pytest.skip("repository-pinned en_core_web_sm model required")
    transport = cast(redis.Redis, redis.Redis.from_url(broker))
    assert transport.ping()
    monkeypatch.setenv("ORCHESTRATION_TEST_DATABASE_URL", dsn)
    context = multiprocessing.get_context("fork")
    with context.Manager() as manager, _ingestion() as env:
        providers = _Providers(manager, context)
        env.providers = providers
        env.queue = "ingestion_test_" + uuid4().hex
        for name, value in {
            "UPLOAD_DIR": str(tmp_path / "uploads"),
            "STORAGE_BACKEND": "local",
            "SUPABASE_STORAGE_ENABLED": False,
            "DO_KB_ENABLED": True,
            "SWEEPERS_ENABLED": True,
            "RECONCILER_ENABLED": True,
            "RECONCILER_APPLY": True,
            "SUPABASE_JWT_SECRET": uuid4().hex + uuid4().hex,
            "SUPABASE_JWT_ISSUER": "synthetic-lifecycle-test",
        }.items():
            monkeypatch.setattr(settings, name, value)
        monkeypatch.setattr(database, "SessionLocal", env.Session)
        monkeypatch.setattr(database, "AsyncSessionLocal", env.AsyncSession)
        monkeypatch.setattr(pt, "SessionLocal", env.Session)
        monkeypatch.setattr(rt, "SessionLocal", env.Session)
        monkeypatch.setattr(
            KnowledgeGraphService,
            "get_session",
            lambda self, database="neo4j": _GraphTransport(providers),
        )
        api = _KBTransport(providers)
        monkeypatch.setattr(ingest, "get_do_kb_client", lambda: api)
        monkeypatch.setattr(kb_client, "get_do_kb_client", lambda: api)

        class SpacesTransport:
            bucket = "synthetic-lifecycle-bucket"

            def upload_file(self, key: str, content: bytes, **kwargs: Any) -> None:
                providers.objects[key] = content

            def delete_file(self, key: str) -> None:
                providers.objects.pop(key, None)

            def list_objects(self, prefix: str) -> list[str]:
                return [key for key in providers.objects if key.startswith(prefix)]

        from src.core import s3_client

        monkeypatch.setattr(s3_client, "S3StorageHelper", SpacesTransport)
        for key, value in {
            "broker_url": broker,
            "result_backend": broker,
            "broker_pool_limit": 0,
            "task_default_queue": env.queue,
            "task_routes": {"process_document_ingestion": {"queue": env.queue}},
            "broker_transport_options": {
                "global_keyprefix": env.queue + ":",
                "socket_timeout": 5,
                "socket_connect_timeout": 5,
            },
            "result_backend_transport_options": {"global_keyprefix": env.queue + ":"},
        }.items():
            monkeypatch.setitem(celery_app.conf, key, value)
        # Celery caches routes, publishers and backends. Rebuild configuration
        # objects so one fixture cannot publish into a previous fixture's queue.
        monkeypatch.setattr(celery_app, "_pool", None)
        monkeypatch.setattr(celery_app, "amqp", AMQP(celery_app))
        monkeypatch.setattr(celery_app, "_backend_cache", None)
        monkeypatch.setattr(celery_app._local, "backend", None, raising=False)
        SearchAnalyticsEvent.__table__.create(env.Session.kw["bind"])
        env.user_id, env.other_user_id, env.other_org_id = uuid4(), uuid4(), uuid4()
        with env.Session() as db:
            db.get(Organization, env.org_id).do_kb_uuid = str(uuid4())
            db.add(
                Organization(
                    id=env.other_org_id,
                    name="Other synthetic organization",
                    storage_limit_bytes=10_000,
                    do_kb_uuid=str(uuid4()),
                )
            )
            db.flush()
            for user_id, org_id in [
                (env.user_id, env.org_id),
                (env.other_user_id, env.other_org_id),
            ]:
                db.add(
                    User(
                        id=user_id,
                        email=f"{user_id}@example.invalid",
                        password_hash="synthetic-unused-password",
                        first_name="Synthetic",
                        last_name="Lifecycle",
                        role=UserRole.USER,
                        organization_id=org_id,
                    )
                )
            db.commit()

        async def async_db() -> AsyncIterator[Any]:
            async with env.AsyncSession() as db:
                yield db

        def sync_db() -> Iterator[Session]:
            with env.Session() as db:
                yield db

        app = FastAPI()
        for router in (files.router, documents.router, search.router):
            app.include_router(router)
        app.dependency_overrides[database.get_db] = async_db
        app.dependency_overrides[database.get_db_sync] = sync_db
        env.client = TestClient(app)
        env.worker = _Lifecycle(env, context, tmp_path)
        try:
            yield env
        finally:
            providers.release.set()
            env.worker.close()
            providers.release.close()
            env.client.close()
            # Exact namespaced keys only; never flush a caller-provided broker.
            for key in transport.scan_iter(match=env.queue + ":*"):
                transport.delete(key)
            transport.close()


def _headers(env: SimpleNamespace, *, other: bool = False) -> dict[str, str]:
    user_id = env.other_user_id if other else env.user_id
    org_id = env.other_org_id if other else env.org_id
    token = jwt.encode(
        {
            "sub": str(user_id),
            "email": f"{user_id}@example.invalid",
            "aud": "authenticated",
            "iss": settings.SUPABASE_JWT_ISSUER,
            "exp": datetime.now(timezone.utc) + timedelta(minutes=10),
            "app_metadata": {"organization_id": str(org_id), "role": "USER"},
        },
        settings.SUPABASE_JWT_SECRET,
        algorithm="HS256",
    )
    return {"Authorization": f"Bearer {token}"}


def _upload(env: SimpleNamespace, *, other: bool = False) -> UUID:
    response = env.client.post(
        "/files/upload",
        headers=_headers(env, other=other),
        files={"file": ("lifecycle.txt", TEXT.encode(), "text/plain")},
        data={"title": "Lifecycle synthetic text"},
    )
    assert response.status_code == 200, response.text
    return UUID(response.json()["document_id"])


def _job(env: SimpleNamespace, document_id: UUID) -> UUID:
    with env.Session() as db:
        return cast(
            UUID,
            (
                db.query(ProcessingJob.id)
                .filter(ProcessingJob.document_id == document_id)
                .order_by(ProcessingJob.created_at.desc())
                .first()[0]
            ),
        )


def _assert_completed(env: SimpleNamespace, document_id: UUID) -> None:
    with env.Session() as db:
        document = db.get(Document, document_id)
        assert document.processing_status == ProcessingStatus.COMPLETED
        assert document.content_text == TEXT
        assert document.neo4j_index_status == "completed"
        assert document.do_kb_sync_status == "completed"
        jobs = db.query(ProcessingJob).filter_by(document_id=document_id).all()
        assert sum(job.status == JobStatus.COMPLETED for job in jobs) == 1
        entities = db.query(Entity).filter_by(document_id=document_id, is_deleted=False)
        names = [(entity.name, entity.entity_type) for entity in entities]
        assert names
        assert len(names) == len(set(names))


def test_ingestion_lifecycle_real_worker(lifecycle: SimpleNamespace) -> None:
    env = lifecycle
    env.worker.start()
    document_id = _upload(env)
    assert env.worker.completion()["status"] == "completed"
    _assert_completed(env, document_id)
    status = env.client.get(f"/documents/{document_id}/status", headers=_headers(env))
    assert status.status_code == 200, status.text
    assert status.json()["processing_status"] == "indexed"
    for other in (False, True):
        response = env.client.post(
            "/search/",
            headers=_headers(env, other=other),
            json={"query": "Lifecycleproof", "search_type": "fulltext"},
        )
        assert response.status_code == 200, response.text
        ids = [result["document_id"] for result in response.json()["results"]]
        assert ids == ([] if other else [str(document_id)])
    assert (
        env.client.get(
            f"/documents/{document_id}/status", headers=_headers(env, other=True)
        ).status_code
        == 404
    )
    assert env.client.get(f"/documents/{document_id}/status").status_code in (401, 403)
    # Same bytes belong independently to the other org; hash dedup stays scoped.
    other_document_id = _upload(env, other=True)
    assert other_document_id != document_id
    assert env.worker.completion()["status"] == "completed"
    _assert_completed(env, other_document_id)


def test_duplicate_real_delivery_is_terminal_noop(lifecycle: SimpleNamespace) -> None:
    env = lifecycle
    env.worker.start()
    document_id = _upload(env)
    env.worker.completion()
    before = dict(env.providers.graph), dict(env.providers.sources)
    pt.process_document_ingestion.apply_async(
        args=[str(_job(env, document_id))], queue=env.queue, task_id=str(uuid4())
    )
    assert env.worker.completion()["skipped"] == "terminal"
    _assert_completed(env, document_id)
    assert (dict(env.providers.graph), dict(env.providers.sources)) == before


def test_cancel_before_real_delivery(lifecycle: SimpleNamespace) -> None:
    env = lifecycle
    document_id = _upload(env)
    response = env.client.delete(f"/files/cancel/{document_id}", headers=_headers(env))
    assert response.status_code == 200, response.text
    env.worker.start()
    assert env.worker.completion()["skipped"] == "deleted"
    _assert_deleted(env, document_id)


def _assert_deleted(env: SimpleNamespace, document_id: UUID) -> None:
    with env.Session() as db:
        document = db.get(Document, document_id)
        assert document.is_deleted
        assert document.processing_status != ProcessingStatus.COMPLETED
        assert document.do_kb_data_source_uuid is None
        jobs = db.query(ProcessingJob).filter_by(document_id=document_id).all()
        assert jobs and all(
            job.is_deleted and job.status == JobStatus.CANCELLED for job in jobs
        )
        assert (
            db.query(Entity)
            .filter_by(document_id=document_id, is_deleted=False)
            .count()
            == 0
        )
        assert db.get(Organization, env.org_id).storage_used_bytes == 0
    assert not env.providers.graph
    assert not env.providers.sources
    assert (
        env.client.get(
            f"/documents/{document_id}/status", headers=_headers(env)
        ).status_code
        == 404
    )


@pytest.mark.parametrize("stage", ["graph", "do_kb"])
def test_cancel_during_provider_write_compensates(
    lifecycle: SimpleNamespace, stage: str
) -> None:
    env = lifecycle
    env.providers.controls["block"] = stage
    env.worker.start()
    document_id = _upload(env)
    assert env.providers.entered.wait(45), env.worker.diagnostics()
    response = env.client.delete(f"/files/cancel/{document_id}", headers=_headers(env))
    assert response.status_code == 200, response.text
    env.providers.release.set()
    assert env.worker.completion()["skipped"] == "deleted"
    _assert_deleted(env, document_id)
    assert (
        env.client.delete(
            f"/files/cancel/{document_id}", headers=_headers(env)
        ).status_code
        == 404
    )


@pytest.mark.parametrize("error_after_acceptance", [False, True])
def test_producer_resumes_after_real_worker_completion(
    lifecycle: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    error_after_acceptance: bool,
) -> None:
    env = lifecycle
    source = tmp_path / "producer.txt"
    source.write_text(TEXT)
    with env.Session() as db:
        document = db.get(Document, env.doc_id)
        document.file_path = str(source)
        document.content_text = None
        job = db.get(ProcessingJob, env.job_id)
        job.status = JobStatus.PENDING
        db.commit()
    env.worker.start()
    send_task = celery_app.send_task

    def publish(*args: Any, **kwargs: Any) -> Any:
        kwargs["queue"] = env.queue
        result = send_task(*args, **kwargs)
        assert env.worker.completion()["status"] == "completed"
        if error_after_acceptance:
            raise OSError("synthetic acknowledgement loss after accepted delivery")
        return result

    monkeypatch.setattr(celery_app, "send_task", publish)

    async def dispatch() -> None:
        async with env.AsyncSession() as db:
            await ProcessingPipeline(db).queue_processing_job(str(env.job_id))

    asyncio.run(dispatch())
    _assert_completed(env, env.doc_id)


def test_provider_cleanup_outage_remains_reconcilable(
    lifecycle: SimpleNamespace,
) -> None:
    env = lifecycle
    env.worker.start()
    document_id = _upload(env)
    env.worker.completion()
    env.providers.controls["cleanup_fail"] = True
    response = env.client.delete(f"/documents/{document_id}", headers=_headers(env))
    assert response.status_code == 200, response.text
    with env.Session() as db:
        document = db.get(Document, document_id)
        assert document.is_deleted
        assert document.document_metadata["graph_cleanup_requested"] is True
        assert document.neo4j_index_status == "failed"
    assert env.providers.graph
    env.providers.controls["cleanup_fail"] = False
    result = rt.reconcile_satellite_indexes()
    assert result["kg_cleanup_succeeded"] == 1
    assert not env.providers.graph
    assert not env.providers.sources
    with env.Session() as db:
        assert db.get(Document, document_id).neo4j_index_status == "completed"


def test_worker_process_loss_recovers_atomically_and_requires_explicit_retry(
    lifecycle: SimpleNamespace,
) -> None:
    env = lifecycle
    env.providers.controls["block"] = "do_kb"
    worker = env.worker.start()
    document_id = _upload(env)
    assert env.providers.entered.wait(45), env.worker.diagnostics()
    old_job_id = _job(env, document_id)
    worker.kill()
    worker.join(10)
    assert not worker.is_alive()
    assert worker.exitcode is not None and worker.exitcode < 0
    with env.Session() as db:
        job = db.get(ProcessingJob, old_job_id)
        assert job.status == JobStatus.RUNNING
        assert (
            db.get(Document, document_id).processing_status
            == ProcessingStatus.PROCESSING
        )
        job.updated_at = datetime.now(timezone.utc) - timedelta(hours=2)
        db.commit()
    assert pt.sweep_stuck_processing_jobs() == {"swept": 1}
    with env.Session() as db:
        assert db.get(ProcessingJob, old_job_id).status == JobStatus.FAILED
        assert (
            db.get(Document, document_id).processing_status == ProcessingStatus.FAILED
        )
    env.providers.controls["block"] = ""
    env.providers.release.set()
    env.worker.start()
    pt.process_document_ingestion.apply_async(
        args=[str(old_job_id)], queue=env.queue, task_id=str(uuid4())
    )
    assert env.worker.completion()["skipped"] == "terminal"
    response = env.client.post(f"/files/{document_id}/reprocess", headers=_headers(env))
    assert response.status_code == 200, response.text
    assert UUID(response.json()["job_id"]) != old_job_id
    assert env.worker.completion()["status"] == "completed"
    _assert_completed(env, document_id)
    with env.Session() as db:
        assert db.get(ProcessingJob, old_job_id).status == JobStatus.FAILED


@pytest.mark.parametrize("stage", ["graph", "do_kb"])
def test_cleanup_survives_worker_loss_after_late_provider_acceptance(
    lifecycle: SimpleNamespace, stage: str
) -> None:
    """Delete cleans the empty provider, then the old writer lands and dies."""
    env = lifecycle
    env.providers.controls["block"] = stage
    worker = env.worker.start()
    document_id = _upload(env)
    assert env.providers.entered.wait(45), env.worker.diagnostics()
    response = env.client.delete(f"/documents/{document_id}", headers=_headers(env))
    assert response.status_code == 200, response.text
    with env.Session() as db:
        document = db.get(Document, document_id)
        assert document.document_metadata["pending_satellite_writes"][stage]
        if stage == "graph":
            assert document.neo4j_index_status == "pending"
    env.providers.entered.clear()
    env.providers.controls["block"] = stage + "_accepted"
    env.providers.release.set()
    assert env.providers.entered.wait(45), env.worker.diagnostics()
    worker.kill()
    worker.join(10)
    assert not worker.is_alive()
    artifacts = env.providers.graph if stage == "graph" else env.providers.sources
    assert artifacts, "provider must have accepted the late write before process loss"
    result = rt.reconcile_satellite_indexes()
    assert result["scanned"] == 1
    assert not artifacts, "durable intent must locate accepted but unrecorded writes"
    if stage == "do_kb":
        assert not env.providers.objects


@pytest.mark.parametrize("route", ["files", "documents"])
def test_reprocess_rejects_an_overlapping_worker(
    lifecycle: SimpleNamespace, route: str
) -> None:
    env = lifecycle
    env.providers.controls["block"] = "graph"
    env.worker.start()
    document_id = _upload(env)
    assert env.providers.entered.wait(45), env.worker.diagnostics()
    job_id = _job(env, document_id)
    foreign = env.client.post(
        f"/{route}/{document_id}/reprocess", headers=_headers(env, other=True)
    )
    assert foreign.status_code == 404, foreign.text
    anonymous = env.client.post(f"/{route}/{document_id}/reprocess")
    assert anonymous.status_code in (401, 403)
    response = env.client.post(
        f"/{route}/{document_id}/reprocess?force_reprocess=true", headers=_headers(env)
    )
    assert response.status_code == 409, response.text
    with env.Session() as db:
        assert db.query(ProcessingJob).filter_by(document_id=document_id).count() == 1
        assert db.get(ProcessingJob, job_id).status == JobStatus.RUNNING
        assert (
            db.get(Document, document_id).processing_status
            == ProcessingStatus.PROCESSING
        )
    env.providers.release.set()
    assert env.worker.completion()["status"] == "completed"
    _assert_completed(env, document_id)
