"""Live proof for GOO-319 (scheduled search updates) and GOO-320 (review versions).

Side effects, read before running: creates rows in the scratch database named by
``DATABASE_URL`` (an asyncpg URL, migrated to Alembic head), spawns a private
``redis-server`` on 127.0.0.1:6391 plus ``celery worker`` and ``celery beat``
processes it owns, makes a handful of live HTTPS requests to Crossref, PubMed and
OpenAlex, and writes evidence under ``--out``. It signals only the processes it
spawned. ``--allow-hard-kill`` is required for the crash scenario (SIGKILL of the
spawned worker's process group); without it that scenario is recorded as NOT RUN.

Scenarios (docs/plans/2026-10-04-academic-r8-acceptance-closure.md, Tasks 5-6):
``outage`` (Crossref unreachable), ``main`` (two beats ticking, all five delta
classes, real Crossref notices), ``crash`` (worker killed mid-run, restarted),
``revoke`` (archived workspace), plus the GOO-320 review chain on the main delta.
Seeding reuses the PostgreSQL proofs' helpers from ``tests/integration``.

Run from ``backend/`` with that directory on ``PYTHONPATH``::

    DATABASE_URL=postgresql+asyncpg://... python scripts/validation/r8_live_proof.py \
        --out <evidence dir> --scratch <temp dir> [--self-check | --allow-hard-kill]
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import contextlib
import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence, TypeAlias, cast
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from src.core.encryption import initialize_encryption
from src.models.research_import import ResearchImportRecord
from src.models.research_project_role import (
    ResearchProjectRole,
    ResearchProjectRoleAssignment,
)
from src.models.research_step import ResearchStep
from src.schemas.research_engine import (
    ReviewVersionCreate,
    ScreeningAssignmentCreate,
    ScreeningObservationCreate,
    ScreeningQueueCreate,
    SearchScheduleCreate,
)
from src.services.research_engine import (
    corpus_service,
    review_update_service,
    screening_service,
)
from src.services.research_engine import search_update_rules as rules
from src.services.research_engine import search_update_service as svc
from src.services.research_engine.connectors.registry import build_connectors
from src.services.research_engine.discovery import search_sources
from src.services.research_engine.project_access import ResearchAction, resolve_project
from tests.integration.research_engine_postgres_support import (
    seed_approved_protocol_binding,
)
from tests.integration.test_report_identity_postgres import _seed

BACKEND = Path(__file__).resolve().parents[2]
REDIS_PORT = 6391
QUERY = "membrane imaging"
PROVIDERS = ["crossref", "pubmed", "openalex"]
TZ = "Europe/London"
RETRACTED_DOI = "10.1016/s0140-6736(97)11096-0"
RETRACTED_TITLE = (
    "Ileal-lymphoid-nodular hyperplasia, non-specific colitis, and pervasive "
    "developmental disorder in children"
)
RETRACTION_NOTICE_DOI = "10.1016/s0140-6736(10)60175-4"
ABSENT_DOI = "10.5555/r8-live-absent"
ABSENT_TITLE = "Absent fixture work (made-up DOI under the 10.5555 test prefix)"
# Minutes after the start each scenario's schedule fires. Four minutes apart so a
# slow run cannot overlap the next scenario's worker configuration.
OFFSETS = {"outage": 3, "main": 7, "crash": 11, "revoke": 13}
# Outage: Crossref is sent to a closed local port; PubMed and OpenAlex bypass it.
OUTAGE_ENV = {
    "HTTPS_PROXY": "http://127.0.0.1:9",
    "HTTP_PROXY": "http://127.0.0.1:9",
    "NO_PROXY": "eutils.ncbi.nlm.nih.gov,api.openalex.org,127.0.0.1,localhost",
}
# The real tick entry only, so no unrelated beat task runs against the scratch DB.
BEAT_LAUNCHER = (
    "import sys\n"
    "from src.tasks.celery_app import celery_app\n"
    "tick = celery_app.conf.beat_schedule['search-updates-tick']\n"
    "celery_app.conf.beat_schedule = {'search-updates-tick': tick}\n"
    "celery_app.start(['beat', '--loglevel=INFO', '-s', sys.argv[1]])\n"
)
SNAPSHOT_SQL = text("""
SELECT e.id::text AS execution_id, e.schedule_id::text AS schedule_id,
       e.collection_id::text AS collection_id, e.scheduled_local, e.missed_fires,
       COALESCE((SELECT json_agg(json_build_object(
                   'id', a.id, 'outcome', a.outcome, 'reason', a.reason,
                   'worker', a.worker, 'detail', a.detail, 'at', a.created_at)
                 ORDER BY a.created_at, a.id)
                 FROM research_search_execution_attempts a
                 WHERE a.execution_id = e.id), '[]'::json)::text AS attempts,
       r.delta_hash, (r.delta -> 'counts')::text AS counts
FROM research_search_executions e
LEFT JOIN research_search_execution_results r ON r.execution_id = e.id
ORDER BY e.scheduled_for, e.created_at
""")
Factory: TypeAlias = async_sessionmaker[AsyncSession]
Snapshot: TypeAlias = dict[str, dict[str, Any]]


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def idem(name: str) -> str:
    """A plain-label idempotency key (these requests are replayed on purpose)."""
    return f"r8-live-{name}"


def minute_at(start: datetime, minutes: int) -> int:
    return (start + timedelta(minutes=minutes)).astimezone(ZoneInfo(TZ)).minute


# --- evidence ----------------------------------------------------------------


class Recorder:
    def __init__(self, out: Path) -> None:
        self.out = out
        self.timeline: list[dict[str, Any]] = []
        self.checks: list[dict[str, Any]] = []
        self.seen: dict[str, str] = {}

    def event(self, kind: str, **detail: Any) -> None:
        entry = {"at": utcnow().isoformat(timespec="seconds"), "event": kind, **detail}
        self.timeline.append(entry)
        print(json.dumps(entry, default=str), flush=True)

    def check(self, name: str, ok: bool, detail: Any = None) -> bool:
        self.checks.append({"check": name, "ok": bool(ok), "detail": detail})
        print(f"{'PASS' if ok else 'FAIL'} {name}", flush=True)
        return bool(ok)

    def save(self, name: str, data: Any) -> None:
        body = json.dumps(data, indent=2, sort_keys=True, default=str)
        (self.out / name).write_text(body + "\n")

    def flush(self) -> None:
        self.save("timeline.json", self.timeline)
        self.save("checks.json", self.checks)


# --- processes ---------------------------------------------------------------


@dataclass
class Child:
    name: str
    proc: subprocess.Popen[bytes]
    log: Path


class Fleet:
    """Child processes this run spawned; the only processes it ever signals."""

    def __init__(self, scratch: Path) -> None:
        self.scratch = scratch
        self.logs = scratch / "logs"
        self.children: list[Child] = []

    def _env(self, outage: bool) -> dict[str, str]:
        env = {
            k: os.environ[k]
            for k in ("PATH", "HOME", "LANG", "TMPDIR")
            if k in os.environ
        }
        env.update(
            DATABASE_URL=os.environ["DATABASE_URL"],
            REDIS_URL=f"redis://127.0.0.1:{REDIS_PORT}/0",
            SEARCH_UPDATES_ENABLED="true",
            PYTHONPATH=str(BACKEND),
            PYTHONUNBUFFERED="1",
        )
        if outage:
            env.update(OUTAGE_ENV)
        return env

    def _spawn(self, name: str, argv: Sequence[str], env: Mapping[str, str]) -> Child:
        self.logs.mkdir(parents=True, exist_ok=True)
        path = self.logs / f"{name}.log"
        with path.open("ab") as handle:
            proc = subprocess.Popen(
                list(argv),
                env=dict(env),
                cwd=str(BACKEND),
                stdout=handle,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
        child = Child(name, proc, path)
        self.children.append(child)
        return child

    def start_redis(self) -> Child:
        with contextlib.suppress(OSError):
            socket.create_connection(("127.0.0.1", REDIS_PORT), timeout=1).close()
            raise RuntimeError(f"port {REDIS_PORT} is already in use")
        binary = shutil.which("redis-server")
        if binary is None:
            raise RuntimeError("redis-server is not on PATH")
        data = self.scratch / "redis"
        data.mkdir(parents=True, exist_ok=True)
        child = self._spawn(
            "redis",
            [binary, "--port", str(REDIS_PORT), "--save", "", "--appendonly", "no"]
            + ["--dir", str(data)],
            {"PATH": os.environ["PATH"]},
        )
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            with contextlib.suppress(OSError):
                socket.create_connection(("127.0.0.1", REDIS_PORT), timeout=1).close()
                return child
            time.sleep(0.3)
        raise TimeoutError("redis did not start")

    def start_worker(self, name: str, *, outage: bool) -> Child:
        argv = [sys.executable, "-m", "celery", "-A", "src.tasks.celery_app", "worker"]
        argv += ["-Q", "celery", "--concurrency=1", "--loglevel=INFO"]
        argv += ["-n", f"{name}@%h"]
        return self._spawn(name, argv, self._env(outage))

    def start_beat(self, name: str) -> Child:
        schedule = self.scratch / f"{name}-schedule"
        return self._spawn(
            name,
            [sys.executable, "-c", BEAT_LAUNCHER, str(schedule)],
            self._env(False),
        )

    def alive(self, child: Child) -> bool:
        return child.proc.poll() is None

    def _signal(self, child: Child, sig: int) -> None:
        with contextlib.suppress(ProcessLookupError):
            os.killpg(child.proc.pid, sig)

    def stop(self, child: Child, timeout: float = 60.0) -> None:
        """SIGTERM the group; SIGKILL only if it ignores that for ``timeout``."""
        self._signal(child, signal.SIGTERM)
        try:
            child.proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            self._signal(child, signal.SIGKILL)
            child.proc.wait(timeout=15)

    def hard_kill(self, child: Child) -> None:
        """SIGKILL the whole group (parent and prefork children), like an
        OOM-killed pod."""
        self._signal(child, signal.SIGKILL)
        child.proc.wait(timeout=15)

    def stop_all(self) -> None:
        for child in reversed(self.children):
            if self.alive(child):
                self.stop(child)

    def excerpts(self, limit: int = 40) -> str:
        """The lines that show tick handling, restarts and shutdowns, per
        process (full logs stay in the scratch directory)."""
        keys = (
            "ready.",
            "beat: Starting",
            "Sending due task search-updates-tick",
            "search_update_tasks.tick",
            "scheduled search execution failed",
            "Warm shutdown",
        )
        parts: list[str] = []
        for child in self.children:
            lines = child.log.read_text(errors="replace").splitlines()
            picked = [ln for ln in lines if any(k in ln for k in keys)]
            parts.append(f"=== {child.name} ({len(picked)} matching lines)")
            parts.extend(picked[:limit])
        return "\n".join(parts) + "\n"

    def summary(self) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for child in self.children:
            lines = child.log.read_text(errors="replace").splitlines()
            tick = [ln for ln in lines if "search_update_tasks.tick" in ln]
            out[child.name] = {
                "log_lines": len(lines),
                "tick_received": sum("received" in ln for ln in tick),
                "tick_succeeded": sum("succeeded" in ln for ln in tick),
                "beat_ticks_sent": sum(
                    "Sending due task search-updates-tick" in ln for ln in lines
                ),
                "execution_failures_logged": sum(
                    "scheduled search execution failed" in ln for ln in lines
                ),
                "exit_code": child.proc.returncode,
            }
        return out


# --- world -------------------------------------------------------------------


@dataclass
class Fixtures:
    """Two live provider documents (a Crossref or OpenAlex result with a DOI,
    year and venue) that become the ``unchanged`` and ``changed`` baseline
    works."""

    unchanged: Any
    changed: Any


async def _no_rag(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
    return {}


async def pick_fixtures() -> Fixtures:
    names = ["crossref", "openalex"]
    connectors = build_connectors(_no_rag, names)
    docs, _coverage = await search_sources(
        connectors, names, QUERY, 10, execution_namespace=f"r8-fixtures:{uuid4()}"
    )
    parsed = svc._records(uuid4(), docs)
    usable = [
        doc
        for doc, record in zip(docs, parsed)
        if (record.parsed.get("identifiers") or {}).get("doi")
        and record.parsed.get("year")
        and record.parsed.get("venue")
    ]
    if len(usable) < 2:
        raise RuntimeError("fewer than two usable live fixtures")
    return Fixtures(usable[0], usable[1])


def pinned_strategy(protocol_version_id: str) -> dict[str, Any]:
    """The proofs' GOO-298 strategy with the live providers."""
    strategy: dict[str, Any] = {
        "schema_version": "nous.academic.search-strategy.v1",
        "project_id": None,
        "protocol_version_id": protocol_version_id,
        "effective_plan_hash": "e" * 64,
        "blueprint_id": None,
        "blueprint_version": 1,
        "step_id": "search",
        "intended": {
            "selected_providers": list(PROVIDERS),
            "parameters": {"query_template": "$query"},
        },
        "route_limits": {
            "max_providers": 6,
            "max_results_per_provider": 50,
            "requested_results_per_provider": 10,
            "request_timeout_seconds": 90.0,
        },
    }
    strategy["strategy_version"] = rules.strategy_hash(strategy)
    return strategy


def baseline_records(collection: UUID, fx: Fixtures) -> list[ResearchImportRecord]:
    """P1 exactly as the live providers return it (``unchanged``), P2 with the
    year raised by one (``changed``), the retracted DOI (a real Crossref
    notice, ``corrected_retracted``) and a made-up DOI (never returned,
    ``unknown``)."""
    p1, p2 = svc._records(collection, [fx.unchanged, fx.changed])
    shifted = {**p2.parsed, "year": str(int(p2.parsed["year"]) + 1)}
    p2.parsed = shifted
    p2.raw = json.dumps({"fixture": "year shifted by one", "parsed": shifted})
    records = [p1, p2]
    for title, doi, year, venue in (
        (RETRACTED_TITLE, RETRACTED_DOI, "1998", "The Lancet"),
        (ABSENT_TITLE, ABSENT_DOI, "2020", "J"),
    ):
        parsed = {
            "title": title,
            "authors": [],
            "year": year,
            "venue": venue,
            "identifiers": {"doi": doi},
            "providers": ["fixture"],
            "url": None,
        }
        records.append(
            ResearchImportRecord(
                id=uuid4(),
                collection_id=collection,
                record_index=len(records),
                status="accepted",
                raw=json.dumps(parsed, sort_keys=True),
                parsed=parsed,
            )
        )
    return records


@dataclass
class World:
    label: str
    ids: dict[str, UUID]
    strategy: dict[str, Any]
    schedule_id: UUID | None = None
    fire_minute: int | None = None
    notes: dict[str, Any] = field(default_factory=dict)

    @property
    def collection(self) -> UUID:
        return self.ids["collection"]


async def seed_world(factory: Factory, label: str, fx: Fixtures) -> World:
    ids = await _seed(factory)  # org, users O R A V F, workspace, collection, run
    collection = ids["collection"]
    async with factory() as db:
        db.add(
            ResearchProjectRoleAssignment(
                collection_id=collection,
                user_id=ids["O"],
                role=ResearchProjectRole.SUPERVISOR,
                assigned_by_id=ids["O"],
            )
        )
        blueprint_id = (
            await db.execute(
                text("SELECT blueprint_id FROM research_runs WHERE id = :r"),
                {"r": ids["run"]},
            )
        ).scalar_one()
        binding = await seed_approved_protocol_binding(
            db,
            blueprint_id=blueprint_id,
            collection_id=collection,
            author_id=ids["O"],
            steps=[{"id": "search", "type": "search"}],
            parameters={},
        )
        strategy = pinned_strategy(str(binding.protocol_version_id))
        journal = {
            "schema_version": 1,
            "strategies": {strategy["strategy_version"]: strategy},
            "executions": {},
        }
        await db.execute(
            text("""UPDATE research_runs SET status = 'completed',
                    protocol_version_id = :v,
                    reproducibility_manifest = CAST(:m AS jsonb)
                    WHERE id = :r"""),
            {
                "v": binding.protocol_version_id,
                "m": json.dumps({"_search_receipts_v1": journal}),
                "r": ids["run"],
            },
        )
        db.add(
            ResearchStep(
                run_id=ids["run"],
                step_index=0,
                step_type="search",
                output={
                    "query": QUERY,
                    "coverage": {"strategy_version": strategy["strategy_version"]},
                },
            )
        )
        await db.commit()
    async with factory() as db:
        await resolve_project(db, collection, ids["O"], ResearchAction.EDIT)
        await corpus_service.insert_receipt(
            db,
            collection_id=collection,
            actor_user_id=ids["O"],
            kind="file_import",
            dedup_key=f"file:baseline:{label}",
            lineage_key="b" * 64,
            declared={"database": "r8-live-fixture"},
            observed={},
            records=baseline_records(collection, fx),
        )
        await db.commit()
    return World(label=label, ids=ids, strategy=strategy)


async def create_schedule(factory: Factory, world: World, minute: int) -> None:
    async with factory() as db:
        context = await resolve_project(
            db, world.collection, world.ids["O"], ResearchAction.SUPERVISE
        )
        schedule, _replayed = await svc.create_schedule(
            db,
            context,
            world.ids["O"],
            SearchScheduleCreate(
                source_run_id=world.ids["run"],
                step_id="search",
                strategy_version=world.strategy["strategy_version"],
                cron=f"{minute} * * * *",
                timezone=TZ,
                idempotency_key=idem(world.label),
            ),
        )
    world.schedule_id = schedule.schedule_id
    world.fire_minute = minute


async def sql_one(factory: Factory, sql: str, **params: Any) -> Any:
    async with factory() as db:
        return (await db.execute(text(sql), params)).mappings().first()


# --- observation -------------------------------------------------------------


async def snapshot(factory: Factory) -> Snapshot:
    async with factory() as db:
        rows = (await db.execute(SNAPSHOT_SQL)).mappings().all()
    return {
        row["execution_id"]: {
            "schedule_id": row["schedule_id"],
            "collection_id": row["collection_id"],
            "scheduled_local": row["scheduled_local"],
            "missed_fires": row["missed_fires"],
            "attempts": json.loads(row["attempts"]),
            "delta_hash": row["delta_hash"],
            "counts": json.loads(row["counts"]) if row["counts"] else None,
        }
        for row in rows
    }


def executions_of(snap: Snapshot, world: World) -> list[dict[str, Any]]:
    return [
        {"execution_id": eid, **row}
        for eid, row in snap.items()
        if row["collection_id"] == str(world.collection)
    ]


def outcomes(execution: Mapping[str, Any]) -> list[str]:
    return [a["outcome"] for a in execution["attempts"]]


def terminal(execution: Mapping[str, Any]) -> bool:
    return any(o in svc.TERMINAL for o in outcomes(execution))


def finished(world: World) -> Callable[[Snapshot], bool]:
    def predicate(snap: Snapshot) -> bool:
        runs = executions_of(snap, world)
        return bool(runs) and terminal(runs[0])

    return predicate


async def wait_for(
    factory: Factory,
    rec: Recorder,
    label: str,
    predicate: Callable[[Snapshot], bool],
    *,
    timeout: float,
    worlds: Mapping[str, World],
    interval: float = 2.0,
) -> Snapshot:
    names = {str(w.collection): w.label for w in worlds.values()}
    deadline = time.monotonic() + timeout
    while True:
        snap = await snapshot(factory)
        for eid, row in snap.items():
            signature = json.dumps([outcomes(row), row["delta_hash"] is not None])
            if rec.seen.get(eid) != signature:
                rec.seen[eid] = signature
                rec.event(
                    "execution",
                    scenario=names.get(row["collection_id"]),
                    execution_id=eid,
                    attempts=[
                        {k: a[k] for k in ("outcome", "reason", "worker", "at")}
                        for a in row["attempts"]
                    ],
                    counts=row["counts"],
                )
        if predicate(snap):
            return snap
        if time.monotonic() > deadline:
            raise TimeoutError(f"timed out waiting for {label}")
        await asyncio.sleep(interval)


async def result_of(factory: Factory, execution_id: str) -> dict[str, Any] | None:
    row = await sql_one(
        factory,
        """SELECT delta::text AS delta, coverage::text AS coverage,
                  citation_chasing::text AS chasing,
                  corpus_snapshot ->> 'digest' AS digest, delta_hash,
                  import_receipt_id::text AS receipt,
                  baseline_execution_id::text AS baseline
           FROM research_search_execution_results WHERE execution_id = :e""",
        e=execution_id,
    )
    if row is None:
        return None
    return {
        "delta": json.loads(row["delta"]),
        "coverage": json.loads(row["coverage"]),
        "citation_chasing": json.loads(row["chasing"]),
        "corpus_snapshot_digest": row["digest"],
        "delta_hash": row["delta_hash"],
        "import_receipt_id": row["receipt"],
        "baseline_execution_id": row["baseline"],
    }


async def receipt_count(factory: Factory, execution_id: str) -> int:
    row = await sql_one(
        factory,
        "SELECT count(*) AS n FROM research_import_receipts WHERE dedup_key = :k",
        k=f"scheduled:{execution_id}",
    )
    return int(row["n"]) if row else 0


async def baseline_reports(factory: Factory, world: World) -> dict[str, UUID]:
    """``doi -> report id`` of the world's baseline snapshot."""
    row = await sql_one(
        factory,
        "SELECT baseline_snapshot::text AS s FROM research_search_schedules"
        " WHERE id = :s",
        s=world.schedule_id,
    )
    reports = json.loads(row["s"])["reports"] if row else {}
    return {
        doi: UUID(rid)
        for rid, entry in reports.items()
        for doi in (entry.get("identifiers") or {}).get("doi") or []
    }


def item_for(delta: Mapping[str, Any], report_id: UUID | None) -> dict[str, Any] | None:
    wanted = None if report_id is None else str(report_id)
    return next((i for i in delta["items"] if i["report_id"] == wanted), None)


def coverage_view(coverage: Mapping[str, Any]) -> dict[str, Any]:
    keys = ("status", "returned", "limit", "completion", "error_type")
    return {
        name: {k: receipt.get(k) for k in keys}
        for name, receipt in coverage["providers"].items()
    }


# --- scenarios ---------------------------------------------------------------


async def scenario_outage(
    factory: Factory, rec: Recorder, world: World, snap: Snapshot
) -> None:
    runs = executions_of(snap, world)
    rec.check("outage.one_execution", len(runs) == 1, len(runs))
    run = runs[0]
    rec.check("outage.succeeded", outcomes(run)[-1:] == ["succeeded"], outcomes(run))
    result = await result_of(factory, run["execution_id"])
    assert result is not None
    view = coverage_view(result["coverage"])
    rec.check("outage.crossref_not_ok", view["crossref"]["status"] != "ok", view)
    counts = result["delta"]["counts"]
    rec.check(
        "outage.no_fabricated_retraction", counts["corrected_retracted"] == 0, counts
    )
    item = item_for(
        result["delta"], (await baseline_reports(factory, world)).get(RETRACTED_DOI)
    )
    rec.check(
        "outage.retracted_doi_unknown_provider_failed",
        item is not None
        and item["class"] == "unknown"
        and item.get("reason") == "provider_failed"
        and item["publication"]["check"] == "failed",
        item,
    )
    rec.save(
        "scenario-outage.json",
        {
            "execution": run,
            "coverage_providers": view,
            "counts": counts,
            "retracted_doi_item": item,
        },
    )


async def scenario_main(
    factory: Factory, rec: Recorder, world: World, snap: Snapshot, two_beats: bool
) -> dict[str, Any]:
    runs = executions_of(snap, world)
    rec.check("main.one_execution", len(runs) == 1, len(runs))
    run = runs[0]
    rec.check("main.two_beats_ticking", two_beats, two_beats)
    rec.check("main.attempts", outcomes(run) == ["started", "succeeded"], outcomes(run))
    result = await result_of(factory, run["execution_id"])
    assert result is not None
    counts = result["delta"]["counts"]
    rec.check(
        "main.all_five_classes",
        all(counts[c] >= 1 for c in rules.DELTA_CLASSES),
        counts,
    )
    item = item_for(
        result["delta"], (await baseline_reports(factory, world)).get(RETRACTED_DOI)
    )
    notices = ((item or {}).get("evidence") or {}).get("notices", [])
    rec.check(
        "main.retraction_notice_real",
        item is not None
        and item["class"] == "corrected_retracted"
        and RETRACTION_NOTICE_DOI in {n["notice_doi"] for n in notices}
        and {"correction", "retraction"} <= {n["type"] for n in notices},
        item,
    )
    receipts = await receipt_count(factory, run["execution_id"])
    rec.check("main.single_import_receipt", receipts == 1, receipts)
    async with factory() as db:
        context = await resolve_project(
            db, world.collection, world.ids["O"], ResearchAction.VIEW
        )
        export_a = await svc.export_delta(db, context, UUID(run["execution_id"]))
        export_b = await svc.export_delta(db, context, UUID(run["execution_id"]))
    rec.check(
        "main.export_hash_stable",
        export_a["body_sha256"] == export_b["body_sha256"],
        export_a["body_sha256"],
    )
    root = await sql_one(
        factory,
        "SELECT baseline_digest FROM research_search_schedules WHERE id = :s",
        s=world.schedule_id,
    )
    baseline_digest = None if root is None else root["baseline_digest"]
    rec.check(
        "main.two_distinct_corpus_snapshots",
        baseline_digest not in (None, result["corpus_snapshot_digest"]),
        {"baseline": baseline_digest, "after": result["corpus_snapshot_digest"]},
    )
    rec.save("delta-main.json", export_a)
    rec.save(
        "snapshots-main.json",
        {
            "baseline_digest": baseline_digest,
            "snapshot_after_fire_digest": result["corpus_snapshot_digest"],
            "baseline_execution_id": result["baseline_execution_id"],
            "delta_hash": result["delta_hash"],
        },
    )
    rec.save(
        "scenario-main.json",
        {
            "execution": run,
            "coverage_providers": coverage_view(result["coverage"]),
            "counts": counts,
            "retracted_doi_item": item,
            "citation_chasing": result["citation_chasing"],
        },
    )
    return {"execution_id": run["execution_id"], "delta_hash": result["delta_hash"]}


async def scenario_revoke(
    factory: Factory, rec: Recorder, world: World, snap: Snapshot
) -> None:
    runs = executions_of(snap, world)
    rec.check("revoke.one_execution", len(runs) == 1, len(runs))
    run = runs[0]
    last = run["attempts"][-1]
    rec.check(
        "revoke.failed_project_archived",
        last["outcome"] == "failed" and last["reason"] == svc.PROJECT_ARCHIVED,
        last,
    )
    receipts = await receipt_count(factory, run["execution_id"])
    rec.check("revoke.no_import_receipt", receipts == 0, receipts)
    rec.save("scenario-revoke.json", {"execution": run, "import_receipts": receipts})


async def crash_watch(
    factory: Factory,
    rec: Recorder,
    fleet: Fleet,
    worlds: Mapping[str, World],
    allow_hard_kill: bool,
) -> dict[str, Any] | None:
    """Kill the live worker's process group the moment the crash execution has
    a started attempt (and the main execution is done), then start a new one."""
    if not allow_hard_kill:
        rec.event("crash_skipped", reason="--allow-hard-kill not given")
        rec.check("crash.not_run", True, "NOT RUN: --allow-hard-kill not given")
        return None
    crash, main = worlds["crash"], worlds["main"]

    def ready(snap: Snapshot) -> bool:
        runs, gate = executions_of(snap, crash), executions_of(snap, main)
        return (
            bool(runs)
            and outcomes(runs[0]) == ["started"]
            and bool(gate)
            and terminal(gate[0])
        )

    snap = await wait_for(
        factory,
        rec,
        "crash: a started attempt",
        ready,
        timeout=1800,
        worlds=worlds,
        interval=0.25,
    )
    victim = next(
        c
        for c in reversed(fleet.children)
        if c.name.startswith("worker") and fleet.alive(c)
    )
    fleet.hard_kill(victim)
    rec.event("worker_hard_killed", worker=victim.name, scenario="crash")
    first = executions_of(snap, crash)[0]["attempts"][0]
    replacement = fleet.start_worker("worker-3-after-crash", outage=False)
    rec.event("worker_started", worker=replacement.name)
    await asyncio.sleep(200)  # more than three ticks
    after = executions_of(await snapshot(factory), crash)
    rec.check(
        "crash.no_second_run_while_attempt_is_live",
        len(after) == 1 and outcomes(after[0]) == ["started"],
        [outcomes(r) for r in after],
    )
    return {"first_attempt": first}


async def crash_finish(
    factory: Factory,
    rec: Recorder,
    worlds: Mapping[str, World],
    first: Mapping[str, Any],
) -> None:
    crash = worlds["crash"]
    final = await wait_for(
        factory,
        rec,
        "crash: stale attempt retried to a terminal outcome",
        finished(crash),
        timeout=svc.STALE_EXECUTION.total_seconds() + 900,
        worlds=worlds,
        interval=5.0,
    )
    runs = executions_of(final, crash)
    rec.check("crash.one_execution", len(runs) == 1, len(runs))
    run = runs[0]
    rec.check(
        "crash.attempts_started_started_succeeded",
        outcomes(run) == ["started", "started", "succeeded"],
        outcomes(run),
    )
    detail = run["attempts"][1].get("detail") or {}
    rec.check(
        "crash.retry_of_first_attempt", detail.get("retry_of") == first["id"], detail
    )
    rec.check(
        "crash.attempts_from_two_workers",
        run["attempts"][0]["worker"] != run["attempts"][1]["worker"],
        [a["worker"] for a in run["attempts"]],
    )
    receipts = await receipt_count(factory, run["execution_id"])
    rec.check("crash.single_import_receipt", receipts == 1, receipts)
    rec.save("scenario-crash.json", {"execution": run, "import_receipts": receipts})


# --- GOO-320 chain -----------------------------------------------------------


async def screen_all(
    factory: Factory,
    world: World,
    queue_id: UUID,
    reports: Sequence[UUID],
    decision: str,
) -> None:
    """The seeded reviewer R submits one observation per report (single review
    mode). These are harness decisions, not scientific screening."""
    queue = await sql_one(
        factory, "SELECT criteria_hash FROM screening_queues WHERE id = :q", q=queue_id
    )
    assignment = await sql_one(
        factory,
        """SELECT id FROM screening_assignments
           WHERE queue_id = :q AND reviewer_id = :u AND revoked_at IS NULL""",
        q=queue_id,
        u=world.ids["R"],
    )
    assert queue is not None and assignment is not None
    for report in reports:
        async with factory() as db:
            context = await resolve_project(
                db, world.collection, world.ids["R"], ResearchAction.REVIEW
            )
            await screening_service.submit(
                db,
                context,
                queue_id,
                world.ids["R"],
                ScreeningObservationCreate(
                    report_id=report,
                    assignment_id=assignment["id"],
                    criteria_hash=queue["criteria_hash"],
                    decision=cast(Any, decision),
                    idempotency_key=idem(f"obs-{queue_id}-{report}"),
                ),
            )
            await db.commit()


async def parent_review(
    factory: Factory, rec: Recorder, world: World
) -> tuple[Any, dict[str, UUID]]:
    """Before the first fire: the seeded reviewer excludes the four baseline
    works at title/abstract and the supervisor freezes version 1."""
    ids = world.ids
    reports = await baseline_reports(factory, world)
    async with factory() as db:
        context = await resolve_project(
            db, world.collection, ids["O"], ResearchAction.SUPERVISE
        )
        queue = await screening_service.create_queue(
            db,
            context,
            ids["O"],
            ScreeningQueueCreate(
                protocol_version_id=UUID(world.strategy["protocol_version_id"]),
                stage=cast(Any, "title_abstract"),
                report_ids=list(reports.values()),
                idempotency_key=idem("parent-ta"),
            ),
        )
        await screening_service.assign(
            db,
            context,
            queue.id,
            ids["O"],
            ScreeningAssignmentCreate(
                reviewer_user_id=ids["R"], idempotency_key=idem("parent-assign")
            ),
        )
        await db.commit()
    await screen_all(factory, world, queue.id, list(reports.values()), "exclude")
    async with factory() as db:
        context = await resolve_project(
            db, world.collection, ids["O"], ResearchAction.SUPERVISE
        )
        root, _replayed = await review_update_service.create_root(
            db,
            context,
            ids["O"],
            ReviewVersionCreate(
                rationale="R8 live proof: parent review frozen before the update",
                idempotency_key=idem("root"),
            ),
        )
    rec.check(
        "chain.root_carries_four_decisions", len(root.carried) == 4, len(root.carried)
    )
    return root, reports


async def successor_review(
    factory: Factory,
    rec: Recorder,
    world: World,
    root: Any,
    reports: Mapping[str, UUID],
    execution_id: str,
    delta_hash: str,
) -> None:
    ids = world.ids
    absent = reports[ABSENT_DOI]
    request = ReviewVersionCreate(
        parent_review_version_id=root.id,
        execution_id=UUID(execution_id),
        delta_hash=delta_hash,
        reviewer_user_ids=[ids["R"]],
        carry_with_uncertainty=[absent],
        rationale="R8 live proof: accept the real scheduled delta",
        idempotency_key=idem("successor"),
    )

    async def create() -> tuple[Any, bool]:
        async with factory() as db:
            context = await resolve_project(
                db, world.collection, ids["O"], ResearchAction.SUPERVISE
            )
            return cast(
                tuple[Any, bool],
                await review_update_service.create_successor(
                    db, context, ids["O"], request
                ),
            )

    successor, first_replayed = await create()
    again, second_replayed = await create()
    rec.check(
        "chain.successor_idempotent",
        not first_replayed and second_replayed and again.id == successor.id,
        {"first_replayed": first_replayed, "second_replayed": second_replayed},
    )
    result = await result_of(factory, execution_id)
    assert result is not None
    classes = {i["report_id"]: i["class"] for i in result["delta"]["items"]}
    carried = {str(c.report_id): c for c in successor.carried}
    unchanged = {r for r, cls in classes.items() if cls == "unchanged"}
    rec.check(
        "chain.unchanged_decision_carried_by_reference",
        bool(unchanged) and unchanged <= set(carried),
        {"unchanged": sorted(unchanged), "carried": sorted(carried)},
    )
    rec.check(
        "chain.uncertain_unknown_carried_with_flag",
        str(absent) in carried and carried[str(absent)].uncertain,
        str(absent),
    )
    targeted = {
        r
        for r, cls in classes.items()
        if cls in ("new", "changed", "corrected_retracted")
    }
    required = {str(r) for r in successor.required_work.get("title_abstract", [])}
    rec.check(
        "chain.required_work_matches_delta_classes",
        required == targeted,
        {"required": len(required), "expected": len(targeted)},
    )
    work = next((w for w in successor.work if w.stage == "title_abstract"), None)
    rec.check(
        "chain.targeted_queue_assigned_to_reviewer",
        work is not None
        and work.queue_id is not None
        and ids["R"] in work.assigned_reviewer_ids,
        (
            None
            if work is None
            else {"status": work.status, "reviewers": len(work.assigned_reviewer_ids)}
        ),
    )
    async with factory() as db:
        context = await resolve_project(
            db, world.collection, ids["O"], ResearchAction.VIEW
        )
        before = await review_update_service.accounting(db, context, successor.id)
    rec.check(
        "chain.accounting_unresolved_while_work_open",
        before.boxes is None and before.error is not None,
        before.error,
    )
    if work is not None and work.queue_id is not None:
        await screen_all(
            factory,
            world,
            work.queue_id,
            [UUID(r) for r in sorted(required)],
            "exclude",
        )
    async with factory() as db:
        context = await resolve_project(
            db, world.collection, ids["O"], ResearchAction.VIEW
        )
        after = await review_update_service.accounting(db, context, successor.id)
        exports = {}
        for label, version in (("root", root.id), ("successor", successor.id)):
            first_body = await review_update_service.export_version(
                db, context, version
            )
            second_body = await review_update_service.export_version(
                db, context, version
            )
            exports[label] = (first_body, second_body)
    rec.check(
        "chain.accounting_reconciles_after_review",
        after.boxes is not None
        and after.error is None
        and after.flow_matches_frozen_hash,
        {"error": after.error, "boxes": after.boxes},
    )
    rec.check(
        "chain.exports_reload_identically",
        all(a["body_sha256"] == b["body_sha256"] for a, b in exports.values()),
        {k: v[0]["body_sha256"] for k, v in exports.items()},
    )
    for user, expected, label in (("R", 403, "reviewer"), ("F", 404, "foreign")):
        try:
            async with factory() as db:
                context = await resolve_project(
                    db, world.collection, ids[user], ResearchAction.SUPERVISE
                )
                await review_update_service.create_root(
                    db,
                    context,
                    ids[user],
                    ReviewVersionCreate(
                        rationale="denied", idempotency_key=idem(f"deny-{user}")
                    ),
                )
            status = 200
        except Exception as error:  # noqa: BLE001 - HTTPException carries the status
            status = int(getattr(error, "status_code", 0))
        rec.check(f"chain.{label}_cannot_create_version", status == expected, status)
    rec.save(
        "review-chain.json",
        {
            "root": root.model_dump(mode="json"),
            "successor_at_creation": successor.model_dump(mode="json"),
            "accounting_before_review": before.model_dump(mode="json"),
            "accounting_after_review": after.model_dump(mode="json"),
            "export_root_sha256": exports["root"][0]["body_sha256"],
            "export_successor_sha256": exports["successor"][0]["body_sha256"],
        },
    )
    rec.save("review-version-root-export.json", exports["root"][0])
    rec.save("review-version-successor-export.json", exports["successor"][0])


# --- driver ------------------------------------------------------------------


async def self_check(out: Path, scratch: Path) -> int:
    rec = Recorder(out)
    fleet = Fleet(scratch)
    try:
        fleet.start_redis()
        worker = fleet.start_worker("worker-selfcheck", outage=False)
        deadline = time.monotonic() + 90
        registered = False
        while time.monotonic() < deadline and fleet.alive(worker):
            body = worker.log.read_text(errors="replace")
            if "src.tasks.search_update_tasks.tick" in body and "ready." in body:
                registered = True
                break
            time.sleep(1)
        rec.check("self_check.worker_registers_tick", registered, str(worker.log))
    finally:
        fleet.stop_all()
    return 0 if all(c["ok"] for c in rec.checks) else 1


def init_field_encryption() -> None:
    """Seeded users carry encrypted PII columns, so this process needs a key.
    It is random per run and never stored: the workers resolve projects through
    ``User.organization_id`` only and never read those columns."""
    os.environ.setdefault(
        "ENCRYPTION_MASTER_KEY", base64.b64encode(os.urandom(32)).decode()
    )
    initialize_encryption()


async def run(args: argparse.Namespace) -> int:
    out, scratch = Path(args.out), Path(args.scratch)
    out.mkdir(parents=True, exist_ok=True)
    scratch.mkdir(parents=True, exist_ok=True)
    rec = Recorder(out)
    fleet = Fleet(scratch)
    engine = create_async_engine(os.environ["DATABASE_URL"])
    factory: Factory = async_sessionmaker(engine, expire_on_commit=False)
    started = utcnow()
    crash_task: asyncio.Task[dict[str, Any] | None] | None = None
    try:
        init_field_encryption()
        fx = await pick_fixtures()
        rec.event("fixtures_picked", query=QUERY)
        worlds = {label: await seed_world(factory, label, fx) for label in OFFSETS}
        for label, offset in OFFSETS.items():
            await create_schedule(factory, worlds[label], minute_at(started, offset))
        root, reports = await parent_review(factory, rec, worlds["main"])
        async with factory() as db:
            await db.execute(
                text("UPDATE workspaces SET is_archived = true WHERE id = :w"),
                {"w": worlds["revoke"].ids["workspace"]},
            )
            await db.commit()
        rec.event(
            "worlds_ready",
            schedules={
                w.label: {"schedule_id": str(w.schedule_id), "minute": w.fire_minute}
                for w in worlds.values()
            },
        )
        rec.save(
            "state.json",
            {
                "started_at": started.isoformat(),
                "worlds": {
                    w.label: {
                        "collection_id": str(w.collection),
                        "schedule_id": str(w.schedule_id),
                        "fire_minute": w.fire_minute,
                    }
                    for w in worlds.values()
                },
            },
        )
        fleet.start_redis()
        worker1 = fleet.start_worker("worker-1-outage", outage=True)
        beat1 = fleet.start_beat("beat-1")
        rec.event(
            "started", worker=worker1.name, beat=beat1.name, mode="crossref blocked"
        )

        snap = await wait_for(
            factory,
            rec,
            "outage",
            finished(worlds["outage"]),
            timeout=900,
            worlds=worlds,
        )
        await scenario_outage(factory, rec, worlds["outage"], snap)
        rec.check(
            "outage.finished_before_main_fire",
            not executions_of(await snapshot(factory), worlds["main"]),
        )

        fleet.stop(worker1)
        rec.event("worker_stopped", worker=worker1.name, signal="SIGTERM")
        worker2 = fleet.start_worker("worker-2-normal", outage=False)
        beat2 = fleet.start_beat("beat-2")
        rec.event(
            "started", worker=worker2.name, beat=beat2.name, mode="normal, two beats"
        )
        crash_task = asyncio.create_task(
            crash_watch(factory, rec, fleet, worlds, args.allow_hard_kill)
        )

        snap = await wait_for(
            factory, rec, "main", finished(worlds["main"]), timeout=900, worlds=worlds
        )
        main = await scenario_main(
            factory,
            rec,
            worlds["main"],
            snap,
            fleet.alive(beat1) and fleet.alive(beat2),
        )
        await successor_review(
            factory,
            rec,
            worlds["main"],
            root,
            reports,
            main["execution_id"],
            main["delta_hash"],
        )

        crashed = await crash_task
        if crashed is not None:
            await crash_finish(factory, rec, worlds, crashed["first_attempt"])
        snap = await wait_for(
            factory,
            rec,
            "revoke",
            finished(worlds["revoke"]),
            timeout=900,
            worlds=worlds,
        )
        await scenario_revoke(factory, rec, worlds["revoke"], snap)
    finally:
        if crash_task is not None and not crash_task.done():
            crash_task.cancel()
        fleet.stop_all()
        rec.event("all_children_stopped")
        rec.save("log-summary.json", fleet.summary())
        (out / "log-excerpts.txt").write_text(fleet.excerpts())
        rec.flush()
        await engine.dispose()
    failed = [c["check"] for c in rec.checks if not c["ok"]]
    print(
        f"checks: {len(rec.checks) - len(failed)} passed, {len(failed)} failed {failed}"
    )
    return 1 if failed else 0


def main() -> None:
    parser = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    parser.add_argument("--out", required=True, help="evidence directory")
    parser.add_argument("--scratch", required=True, help="temp dir for logs and Redis")
    parser.add_argument("--self-check", action="store_true")
    parser.add_argument("--allow-hard-kill", action="store_true")
    args = parser.parse_args()
    runner = (
        self_check(Path(args.out), Path(args.scratch)) if args.self_check else run(args)
    )
    sys.exit(asyncio.run(runner))


if __name__ == "__main__":
    main()
