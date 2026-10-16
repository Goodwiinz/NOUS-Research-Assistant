"""Synthetic agent-traffic generator for the NOUS dev cluster.

Drives the compiled LangGraph agent in-process (no HTTP) with a real
Postgres session and a dedicated synthetic user so every run produces
LangSmith traces in ``rag-agent-dev`` — exactly the same project and
signal shape as real user traffic.

Scheduling
----------
A Kubernetes CronJob (``templates/synthetic-traffic-cronjob.yaml``) runs
this every 20 minutes on the dev cluster via::

    python -m scripts.synthetic_traffic [--rotate|--all|--scenario <key>|--cleanup]

To disable: set ``syntheticTraffic.enabled: false`` in ``values-dev.yaml``
and let ArgoCD sync.  No code changes needed.

Cleanup
-------
Every run (unless ``--cleanup`` is passed alone) ends with a lightweight
cleanup pass that soft-deletes the synthetic org's Documents and the
Collections owned by the synthetic user whose names start with
``synthtraffic-``, both older than 6 hours. Purging Documents is what keeps
the content-hash dedup index from permanently blocking the ingest scenario.

Refusal
-------
The script exits immediately unless ``ENVIRONMENT`` is one of
``ALLOWED_ENVIRONMENTS``. It writes rows and spends LLM budget, so it must
not be startable in a production pod via ``kubectl exec``.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

import structlog

# ---------------------------------------------------------------------------
# Logging — prefer structlog when available, fall back to stdlib
# ---------------------------------------------------------------------------
try:
    log = structlog.get_logger(__name__)
except Exception:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s"
    )
    log = logging.getLogger(__name__)  # type: ignore[assignment]

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

SYNTH_EMAIL = "synthetic-traffic@nous.dev"
SYNTH_FIRST_NAME = "Synthetic"
SYNTH_LAST_NAME = "Traffic"
SYNTH_PREFIX = "synthtraffic-"
SYNTH_ORG_NAME = "Synthetic Traffic Org"

# This script writes real rows and spends real LLM budget as a synthetic user.
# Nothing but this allowlist stops `kubectl exec` in a prod pod from starting a
# sweep, so it fails closed: an unset ENVIRONMENT refuses too (audit R5-L21).
ALLOWED_ENVIRONMENTS = frozenset(
    {"dev", "development", "local", "test", "testing", "ci"}
)

# Rotate window: one scenario per 20-minute window keeps cost bounded while
# ensuring full coverage across 8 × 20 min = 2.7 h.
ROTATE_WINDOW_S = 1200  # 20 minutes in seconds

MAX_HITL_RESUMES = 3
SCENARIO_TIMEOUT_S = 360  # cap per scenario to match jobs._run_agent_graph


def assert_safe_environment(environment: Optional[str] = None) -> str:
    """Refuse to run anywhere but a dev-like environment (audit R5-L21).

    ``ENVIRONMENT`` used to be read only to tag traces, so the generator was
    perfectly runnable against production. Raises ``SystemExit`` — this is a
    refusal, not an error to be caught and retried.
    """
    import os

    raw = environment if environment is not None else os.environ.get("ENVIRONMENT", "")
    env = (raw or "").strip().lower()
    if env not in ALLOWED_ENVIRONMENTS:
        raise SystemExit(
            f"synthetic_traffic: refusing to run with ENVIRONMENT="
            f"{env or '<unset>'!r}. This generator writes rows and spends LLM "
            f"budget; allowed environments: {sorted(ALLOWED_ENVIRONMENTS)}"
        )
    return env


# ---------------------------------------------------------------------------
# Scenario catalogue  (mirrors the perf harness + adds create/ingest)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Scenario:
    key: str
    prompt: str
    budget_s: float
    expect_interrupt: bool = False
    page_context: Dict[str, Any] = field(default_factory=dict)


def _ts() -> str:
    """Short timestamp suffix for project names."""
    return datetime.now(tz=timezone.utc).strftime("%Y%m%dT%H%M%S")


# Rotating arXiv ids for the ingest scenario. A single hardcoded id made the
# scenario unsatisfiable after its first success: content-hash dedup
# (uq_documents_org_checksum_live) correctly rejects the second copy, so every
# run from 2026-07-18 onward reported TOOL-FAILED for a pipeline that was
# working. Rotating keeps the destructive path genuinely exercised; the ids are
# well-known, stable papers that will not 404.
INGEST_PAPER_IDS = (
    "1706.03762",  # Attention Is All You Need
    "1810.04805",  # BERT
    "2005.11401",  # RAG
    "1907.11692",  # RoBERTa
    "2005.14165",  # GPT-3
    "1512.03385",  # ResNet
)


def _rotating_paper_id() -> str:
    """Pick an id from the pool by clock, like _choose_scenario_rotate does.

    Deterministic within a window and stateless, so two pods in the same window
    agree — and consecutive runs differ, which is the point.
    """
    import time

    window = int(time.time() // ROTATE_WINDOW_S)
    return INGEST_PAPER_IDS[window % len(INGEST_PAPER_IDS)]


SCENARIOS: List[Scenario] = [
    Scenario(
        key="greeting",
        prompt="hi",
        budget_s=3.0,
        page_context={"type": "general"},
    ),
    Scenario(
        key="general_qa",
        prompt="What is retrieval-augmented generation, in two sentences?",
        budget_s=15.0,
        page_context={"type": "general"},
    ),
    Scenario(
        key="research_arxiv",
        prompt="Search arXiv for recent papers on retrieval-augmented generation.",
        budget_s=45.0,
        page_context={"type": "general"},
    ),
    Scenario(
        key="kg_query",
        prompt=(
            "What entities and relationships are in the knowledge graph for transformers?"
        ),
        budget_s=45.0,
        page_context={"type": "general"},
    ),
    Scenario(
        key="writing_draft",
        # "Draft a short note" is ambiguous — the model can satisfy it with
        # prose and never touch a tool, which is exactly what it did while
        # reporting flag=ok. Ask for something only create_draft /
        # create_project_note can deliver, so the destructive path is really
        # exercised.
        prompt=(
            "Write a short note summarizing the key ideas behind RAG and "
            "save it to my library."
        ),
        budget_s=45.0,
        # create_draft and create_project_note are both in
        # WRITING_DESTRUCTIVE_TOOLS, so a real save always raises the HITL
        # interrupt. Without this the scenario had no expectation at all and a
        # zero-tool run was indistinguishable from success.
        expect_interrupt=True,
        page_context={"type": "general"},
    ),
    Scenario(
        key="create_project",
        # Prompt is a template; _build_prompt fills the timestamp.
        prompt=f"Create a project called {SYNTH_PREFIX}{{ts}}",
        budget_s=30.0,
        expect_interrupt=True,
        page_context={"type": "general"},
    ),
    Scenario(
        key="ingest",
        prompt=(
            "Ingest the arXiv paper {paper} into a project named "
            f"{SYNTH_PREFIX}{{ts}}"
        ),
        budget_s=60.0,
        expect_interrupt=True,
        page_context={"type": "general"},
    ),
]

SCENARIOS_BY_KEY: Dict[str, Scenario] = {s.key: s for s in SCENARIOS}


def _expand_prompt(scenario: Scenario) -> str:
    """Fill ``{ts}`` (unique project name) and ``{paper}`` (rotating arXiv id)."""
    return scenario.prompt.replace("{ts}", _ts()).replace(
        "{paper}", _rotating_paper_id()
    )


# ---------------------------------------------------------------------------
# Synthetic user + workspace bootstrap
# ---------------------------------------------------------------------------


async def _get_or_create_synth_org(db: Any) -> Any:
    """Return the org dedicated to synthetic traffic, creating it if absent.

    Was ``select(Organization).limit(1)`` with no ``order_by`` and no filter:
    that grabs an arbitrary REAL org, burning its quota and letting synthetic
    documents surface in that tenant's org-scoped search (audit R5-M28).
    ``Organization.name`` is unique, so the name IS the key.
    """
    from sqlalchemy import select
    from sqlalchemy.exc import IntegrityError

    from src.models.organization import Organization, StorageTier

    result = await db.execute(
        select(Organization).where(Organization.name == SYNTH_ORG_NAME)
    )
    org: Optional[Any] = result.scalar_one_or_none()
    if org is not None:
        return org

    org = Organization(
        name=SYNTH_ORG_NAME,
        storage_tier=StorageTier.FREE,
        storage_limit_bytes=Organization.get_default_storage_limit(StorageTier.FREE),
        is_active=True,
    )
    db.add(org)
    try:
        await db.flush()
    except IntegrityError:
        # Lost the create race against a concurrent pod; the unique name means
        # the winner's row is the one we want.
        await db.rollback()
        result = await db.execute(
            select(Organization).where(Organization.name == SYNTH_ORG_NAME)
        )
        org = result.scalar_one()
    log.info("synthetic_traffic.bootstrap", action="org_ready", org_id=str(org.id))
    return org


async def _ensure_workspace(db: Any, user: Any, org_id: Any) -> Any:
    """Guarantee the synthetic user owns a workspace, healing if it does not.

    The user INSERT and the workspace INSERT are separate commits, and the
    insert-race branch returned a user with no workspace at all. A crash
    between the two used to leave a permanently broken account (audit R5-L25).
    Running this on *every* path — found user included — makes that state heal
    on the next fire instead of persisting forever.

    Ownership alone is not enough. R5-M28 re-pins the *user* to the synthetic
    org, but a workspace created before that still carries the real org it was
    born in, and every org-scoped access path
    (``project_access.accessible_research_workspace_ids``, which compares
    ``coalesce(workspace.organization_id, owner.organization_id)`` against the
    actor's org) rejects it — so ``create_project`` failed on every synthetic
    run with "No workspace found". Only a workspace whose effective org is the
    synthetic org counts; a stale foreign-org one is reported, never touched
    (it sits inside a real tenant — moving or deleting it is a human call).
    """
    from sqlalchemy import select

    from src.models.workspace import Workspace

    result = await db.execute(
        select(Workspace).where(
            Workspace.owner_id == user.id,
            Workspace.is_deleted == False,  # noqa: E712
        )
    )
    owned = list(result.scalars().all())
    # NULL organization_id resolves to the owner's org, which is the synthetic
    # org by the time we get here (the caller re-pins the user first).
    usable = [
        ws for ws in owned if ws.organization_id is None or ws.organization_id == org_id
    ]
    if usable:
        return usable[0]
    for stale in owned:
        log.warning(
            "synthetic_traffic.bootstrap",
            action="stale_foreign_org_workspace",
            warning="synthetic user owns a workspace in a non-synthetic org; "
            "leaving it untouched and creating one in the synthetic org",
            workspace_id=str(stale.id),
            workspace_org_id=str(stale.organization_id),
            org_id=str(org_id),
        )

    workspace = Workspace(
        name=f"{SYNTH_PREFIX}workspace",
        description="Auto-created for synthetic traffic generation",
        owner_id=user.id,
        organization_id=org_id,
        is_archived=False,
        is_public=False,
    )
    db.add(workspace)
    await db.commit()
    log.info(
        "synthetic_traffic.bootstrap",
        action="workspace_healed",
        user_id=str(user.id),
    )
    return workspace


async def _get_or_create_synth_user(db: Any) -> Any:
    """Return the synthetic user, creating it (+ a Workspace) if absent.

    Matches the seeding shape in ``database.init_database``:
    - UserRole.USER (non-admin; the agent needs no elevated rights)
    - organization_id = the dedicated SYNTH_ORG_NAME org, never a real one
    - is_active = True
    - password: random (synthetic user never authenticates via HTTP)
    """
    from sqlalchemy import select, text, update

    from src.models.user import User

    # The org is dedicated to synthetic traffic and is resolved on every path,
    # so a user created before R5-M28 gets re-pinned off whatever real org it
    # originally latched onto.
    org = await _get_or_create_synth_org(db)

    # 1. Look up user
    result = await db.execute(select(User).where(User.email == SYNTH_EMAIL))
    user: Optional[Any] = result.scalar_one_or_none()

    if user is None:
        log.info("synthetic_traffic.bootstrap", action="create_user", email=SYNTH_EMAIL)

        # Insert the user via raw SQL, NOT the ORM. User.first_name/last_name
        # use an encrypted column type whose bind-param encrypts on flush, but
        # ENCRYPTION_MASTER_KEY is unset in dev (real users are created by the
        # Supabase handle_new_user trigger at SQL level, never the ORM), so an
        # ORM insert raises "Encryption not initialized". Plaintext names are
        # fine here: the agent never reads them, and the read path returns the
        # raw value when decryption fails.
        new_id = uuid.uuid4()
        # Build a valid bcrypt hash without flushing (set_password mutates the
        # transient object; password_hash is a plain, non-encrypted column).
        _tmp = User(email=SYNTH_EMAIL)
        _tmp.set_password(f"synth-{uuid.uuid4().hex}")
        pw_hash = _tmp.password_hash
        await db.execute(
            text(
                "INSERT INTO users (id, email, password_hash, first_name, "
                "last_name, role, is_active, organization_id, login_count, "
                "created_at, updated_at, is_deleted) VALUES (:id, :email, :pw, "
                ":fn, :ln, CAST(:role AS userrole), true, :org, 0, now(), "
                "now(), false) ON CONFLICT (email) DO NOTHING"
            ),
            {
                "id": new_id,
                "email": SYNTH_EMAIL,
                "pw": pw_hash,
                "fn": SYNTH_FIRST_NAME,
                "ln": SYNTH_LAST_NAME,
                # The userrole PG enum uses the Python member NAMES (uppercase),
                # not the lowercase values — SQLAlchemy Enum's default mapping.
                "role": "USER",
                "org": org.id,
            },
        )
        await db.commit()

        result = await db.execute(select(User).where(User.email == SYNTH_EMAIL))
        user = result.scalar_one_or_none()
        if user is None:
            # Lost the insert race: the only acceptable retry is the SYNTHETIC
            # user itself. Never fall back to a real user — the entire run
            # (memory writes included) would execute in their namespace
            # (audit R2-M2). Dying loudly is fine: the CronJob retries in 20m.
            await asyncio.sleep(1.0)
            result = await db.execute(select(User).where(User.email == SYNTH_EMAIL))
            user = result.scalar_one_or_none()
            if user is None:
                raise RuntimeError(
                    "synthetic_traffic: synthetic user missing after insert; "
                    "refusing to run as a real user"
                )
            log.warning(
                "synthetic_traffic.bootstrap",
                action="reread_after_race",
                user_id=str(user.id),
            )
        else:
            log.info(
                "synthetic_traffic.bootstrap",
                action="user_created",
                user_id=str(user.id),
            )
    else:
        log.debug(
            "synthetic_traffic.bootstrap", action="user_found", user_id=str(user.id)
        )
        if user.organization_id != org.id:
            # Core UPDATE, not attribute assignment: the ORM would try to
            # re-encrypt first_name/last_name on flush (see the INSERT above).
            await db.execute(
                update(User).where(User.id == user.id).values(organization_id=org.id)
            )
            await db.commit()
            result = await db.execute(select(User).where(User.email == SYNTH_EMAIL))
            user = result.scalar_one()
            log.warning(
                "synthetic_traffic.bootstrap",
                action="repinned_to_synth_org",
                user_id=str(user.id),
                org_id=str(org.id),
            )

    # Always — this is what makes a half-bootstrapped account heal (R5-L25).
    await _ensure_workspace(db, user, org.id)

    return user


# ---------------------------------------------------------------------------
# Graph builder (reuses the checkpointer + store, falls back to MemorySaver)
# ---------------------------------------------------------------------------


async def _build_graph() -> Any:
    """Compile the production agent graph with the durable checkpointer.

    Falls back to ``MemorySaver`` when Postgres is unreachable so the
    script can still produce LangSmith traces in degraded environments.
    """
    from src.services.agent.graph import compile_agent_graph

    checkpointer = None
    store = None
    try:
        from src.services.agent.checkpointer import get_checkpointer
        from src.services.agent.memory import get_memory_store

        checkpointer = await get_checkpointer()
        store = await get_memory_store()
    except Exception:
        log.warning(
            "synthetic_traffic.build_graph",
            warning="checkpointer unavailable, falling back to MemorySaver",
        )
        from langgraph.checkpoint.memory import MemorySaver

        checkpointer = MemorySaver()
        store = None

    return compile_agent_graph(checkpointer=checkpointer, store=store)


# ---------------------------------------------------------------------------
# Per-scenario driver
# ---------------------------------------------------------------------------


@dataclass
class TurnResult:
    scenario: str
    wall_clock_s: float
    interrupted: bool
    resumes: int
    intent: str
    tool_executions: int
    assistant_preview: str
    error: Optional[str] = None
    # Derived once by run_scenario via classify_turn; the sweep summary reads
    # it rather than re-deriving from the key, which would couple the summary
    # to the catalogue and KeyError after all the agent work is done.
    flag: str = ""
    # Names of tools whose execution reported status != completed. A count of
    # executions says a tool ran, not that it worked.
    failed_tools: tuple[str, ...] = ()


# Flag for a scenario that declared ``expect_interrupt`` but never produced
# one. Distinct from ERROR (nothing raised) and from ok (nothing happened).
MISSING_INTERRUPT_FLAG = "MISSING-INTERRUPT"

# Flag for a run still interrupted after MAX_HITL_RESUMES confirmations —
# the interrupt fired, the resume loop just never cleared it.
UNRESOLVED_INTERRUPT_FLAG = "INTERRUPT-UNRESOLVED"

# Flag for a run where a tool ran and failed after a confirmed interrupt. The
# resume makes the run *look* finished — but the action it gated did not
# happen.
TOOL_FAILED_FLAG = "TOOL-FAILED"

# The tool layer writes exactly three statuses. Two of them are successes:
# "deduped" means an identical call already succeeded this turn and its
# cached result was reused (``tool_dedupe.build_deduped_execution_entry``;
# the cache only admits ``completed`` candidates). Mirrors the success test
# in ``reflection.py`` — treating "deduped" as failure would flag a healthy
# run whenever the agent repeated a call, which is common.
SUCCESS_TOOL_STATUSES = ("completed", "deduped")

# A tool can report status="completed" and still have done nothing: the
# ingest tool returns ``{"status": "ingestion_failed", "ingested_count": 0}``
# with NO top-level "error" key, and ``_nodes_tools`` only downgrades an
# execution to "failed" when ``"error" in result``. So a zero-document ingest
# — the exact incident this flag exists for — arrives here looking successful.
# Mirrors ``reflection._INGEST_FAILURE_STATUSES``.
FAILED_RESULT_STATUSES = ("ingestion_failed", "ingestion_partial")


def _later_success(executions: list, start: int, tool_name: str) -> bool:
    """True when the same tool succeeds after index ``start``."""
    for te in executions[start + 1 :]:
        if not isinstance(te, dict) or te.get("tool_name") != tool_name:
            continue
        if te.get("status") in SUCCESS_TOOL_STATUSES:
            return True
    return False


def failed_tool_names(executions: Any) -> tuple[str, ...]:
    """Names of tool executions that genuinely failed and were not recovered.

    Three exclusions, each guarding against a false alarm:

    * **Transient failures** — same terms the agent's own reflection guard
      uses (``result.error_type == "transient"``). An arXiv 429 or an upstream
      timeout says nothing about whether the agent did its job.
    * **Circuit-broken duplicates** — ``tool_dedupe`` appends a
      ``capped_from`` entry after repeated identical failures. The failure it
      caps is already in this list, so counting the cap double-reports it, and
      resurrects a transient failure that was deliberately skipped.
    * **Failed-then-retried** — if the same tool succeeds later in the turn,
      the agent recovered. Flagging that would punish exactly the behaviour we
      want from it.

    Conversely a ``completed`` execution whose *result* reports an ingest
    failure counts: status alone does not mean the work happened.
    """
    items = list(executions or [])
    names: list[str] = []
    for idx, te in enumerate(items):
        if not isinstance(te, dict):
            continue
        result = te.get("result")
        result_status = result.get("status") if isinstance(result, dict) else None
        if (
            te.get("status") in SUCCESS_TOOL_STATUSES
            and result_status not in FAILED_RESULT_STATUSES
        ):
            continue
        if te.get("capped_from") is not None:
            continue
        if _later_success(items, idx, te.get("tool_name")):
            continue
        if isinstance(result, dict) and result.get("error_type") == "transient":
            continue
        names.append(str(te.get("tool_name") or "?"))
    return tuple(names)


def classify_turn(scenario: Scenario, result: TurnResult) -> str:
    """Summarize one turn as the flag recorded in ``scenario_done``.

    ``expect_interrupt`` scenarios drive destructive tools (project creation,
    arXiv ingest), so the HITL interrupt is the observable proof the tool was
    actually reached. Without this check a run where the agent answered in
    prose and called nothing is indistinguishable from a successful ingest —
    both logged ``flag=ok, errored=0``, which is how a live no-op ingest went
    unnoticed. The interrupt *detection* was fixed once already (see the
    __interrupt__ note in run_scenario); the expectation was never asserted,
    so a regression of that same class stays silent.

    Still-interrupted-after-resuming is called out separately: the old
    expression fell through to ``ok`` there too, which reads as success for a
    turn whose HITL loop ran out of confirmations without finishing.

    ``TOOL-FAILED`` closes the successor to that hole. Once the agent started
    calling its tools, an ingest run reported ``confirmed(1)`` with three
    executions while the paper was never imported: the first
    ``ingest_arxiv_papers`` call failed on a bad argument and the agent
    wandered off into ``list_projects`` instead of retrying. Interrupt fired,
    resume confirmed, zero documents created — and the summary said
    ``errored=0, expectations_unmet=0``. Ranked above ``confirmed`` because a
    failed destructive tool is the more actionable fact about that turn.
    """
    if result.error:
        return f"ERROR {result.error}"
    if result.interrupted and result.resumes == 0:
        return "INTERRUPT"
    if result.interrupted:  # resumes > 0: MAX_HITL_RESUMES exhausted
        return f"{UNRESOLVED_INTERRUPT_FLAG}({result.resumes})"
    # Gated on a *confirmed* interrupt rather than on expect_interrupt: a
    # resume is direct evidence a destructive tool actually ran, it excludes
    # research_arxiv's transient 429s (nothing interrupts there), it covers
    # writing_draft — whose create_draft/create_project_note are destructive
    # despite the scenario not declaring expect_interrupt — and it leaves the
    # resumes == 0 row to MISSING-INTERRUPT, which is the honest diagnosis
    # when no interrupt ever fired.
    if result.resumes > 0 and result.failed_tools:
        return f"{TOOL_FAILED_FLAG}({','.join(result.failed_tools)})"
    if result.resumes > 0:
        return f"confirmed({result.resumes})"
    if scenario.expect_interrupt:
        return MISSING_INTERRUPT_FLAG
    return "ok"


async def run_scenario(
    scenario: Scenario,
    graph: Any,
    db: Any,  # kept for call-signature stability; config carries ids only (B8)
    user: Any,
) -> TurnResult:
    """Drive one agent scenario as a real user (ids-only graph config).

    HITL auto-confirm: on a pending interrupt (detected via the returned
    ``__interrupt__`` state, or ``GraphInterrupt`` as a defensive fallback)
    we resume with ``Command(resume={"confirmed": True})`` up to
    ``MAX_HITL_RESUMES`` times so destructive tools (create_project / ingest)
    actually execute rather than hanging on the interrupt.

    The scenario result is logged regardless of error; exceptions are
    swallowed so a single failure never aborts the whole sweep.
    """
    from langchain_core.messages import HumanMessage
    from langgraph.errors import GraphInterrupt
    from langgraph.types import Command

    from src.services.agent._builders import RECURSION_LIMIT

    deploy_env = __import__("os").environ.get("ENVIRONMENT", "dev")
    commit = (
        __import__("os").environ.get("GIT_COMMIT")
        or __import__("os").environ.get("GITHUB_SHA")
        or __import__("os").environ.get("COMMIT_SHA")
        or "unknown"
    )

    prompt = _expand_prompt(scenario)
    thread_id = f"synthetic-{scenario.key}-{int(time.time() * 1000)}"

    initial_state = {
        "messages": [HumanMessage(content=prompt)],
        "page_context": dict(scenario.page_context),
        "retrieved_contexts": [],
        "tool_executions": [],
        "thread_id": thread_id,
        "tool_loop_count": 0,
        "error_count": 0,
        "last_error": "",
        "pending_confirmation": {},
        "user_confirmed": False,
        "intent": "",
        "user_memories": [],
        "project_memories": [],
        "plan": [],
        "reflection_count": 0,
        "compaction_count": 0,
        "intent_confidence": 0.0,
        "last_error_info": {},
        "user_id": str(user.id),
        "current_project_id": "",
        "model": "",
        "_reflection_result": None,
        "_force_synthesis_fired": False,
    }

    # Discriminate synthetic traffic in LangSmith via run_name + metadata, both
    # of which reliably land on the uploaded root run. (A post-hoc
    # Client.update_run to tag the finalized root run is rejected by LangSmith —
    # "Duplicate run update requests for the same run are not supported" — so we
    # don't attempt it; filter dashboards on metadata.synthetic or the
    # `synthetic:` run-name prefix instead.) Config "tags" are kept as a
    # best-effort hint; they tag child runs even when they don't reach the root.
    config = {
        "recursion_limit": RECURSION_LIMIT,
        "run_name": f"synthetic:{scenario.key}",
        "tags": [
            "synthetic-traffic",
            f"scenario:{scenario.key}",
            f"deploy:{deploy_env}",
        ],
        "metadata": {
            "synthetic": True,
            "scenario": scenario.key,
            "deploy_env": deploy_env,
            "commit": commit,
            "user_id": str(user.id),
            "org_id": str(getattr(user, "organization_id", "") or ""),
            "thread_id": thread_id,
        },
        # Ids only (audit B8): graph nodes/tools open their own
        # tool_session() and re-load the user org-scoped.
        "configurable": {
            "thread_id": thread_id,
            "user_id": str(user.id),
            "organization_id": str(getattr(user, "organization_id", "") or ""),
            "page_context": dict(scenario.page_context),
        },
    }

    interrupted = False
    resumes = 0
    error: Optional[str] = None
    final_state: Dict[str, Any] = {}

    t0 = time.perf_counter()
    try:
        async with asyncio.timeout(SCENARIO_TIMEOUT_S):
            final_state = await graph.ainvoke(initial_state, config=config)
    except GraphInterrupt:
        # Defensive fallback only. With a checkpointer attached (this graph
        # always has one — real Postgres or the MemorySaver fallback, never
        # None), LangGraph's interrupt() returns `__interrupt__` in the state
        # rather than raising — proven in
        # tests/unit/agent/test_interrupt_ainvoke_semantics.py. Before this
        # fix, this was the ONLY detection path, so create_project/ingest
        # (the two expect_interrupt=True scenarios) silently never triggered
        # the resume loop below across every sampled run.
        interrupted = True
    except Exception as exc:  # noqa: BLE001
        error = f"{type(exc).__name__}: {exc}"

    # Primary interrupt detection: check the state ainvoke() actually
    # returned. Mirrors the real, working mechanism streaming.py uses
    # (aget_state + snapshot.tasks[*].interrupts) for the SSE path.
    if not error and (interrupted or final_state.get("__interrupt__")):
        interrupted = True
        # Auto-confirm HITL interrupts so destructive tools actually execute
        while interrupted and resumes < MAX_HITL_RESUMES:
            resumes += 1
            log.info(
                "synthetic_traffic.hitl",
                scenario=scenario.key,
                resume=resumes,
            )
            try:
                async with asyncio.timeout(SCENARIO_TIMEOUT_S):
                    final_state = await graph.ainvoke(
                        Command(resume={"confirmed": True}), config=config
                    )
                interrupted = bool(final_state.get("__interrupt__"))
            except GraphInterrupt:
                interrupted = True
                continue
            except Exception as exc:
                error = f"{type(exc).__name__}: {exc}"
                break

    wall = time.perf_counter() - t0

    intent = ""
    assistant_preview = ""
    tool_executions_count = 0
    failed_tools: tuple[str, ...] = ()
    if final_state:
        intent = final_state.get("intent", "") or ""
        executions = final_state.get("tool_executions", []) or []
        tool_executions_count = len(executions)
        failed_tools = failed_tool_names(executions)
        for msg in reversed(final_state.get("messages", []) or []):
            if getattr(msg, "type", None) == "ai" and getattr(msg, "content", None):
                assistant_preview = str(msg.content)[:160]
                break

    result = TurnResult(
        scenario=scenario.key,
        wall_clock_s=wall,
        interrupted=interrupted,
        resumes=resumes,
        intent=intent,
        tool_executions=tool_executions_count,
        assistant_preview=assistant_preview,
        error=error,
        failed_tools=failed_tools,
    )

    flag = classify_turn(scenario, result)
    result.flag = flag
    if flag.startswith(TOOL_FAILED_FLAG):
        log.warning(
            "synthetic_traffic.expectation_unmet",
            scenario=scenario.key,
            expected="destructive tool completes",
            tool_executions=tool_executions_count,
            failed_tools=list(failed_tools),
            resumes=resumes,
            # Deliberately does not claim the failed tool WAS the destructive
            # one, or that it failed after the interrupt — execution entries
            # carry no timestamps, so neither is knowable here.
            reason="a tool failed unrecovered during a run with a confirmed interrupt",
        )
    if flag == MISSING_INTERRUPT_FLAG:
        log.warning(
            "synthetic_traffic.expectation_unmet",
            scenario=scenario.key,
            expected="interrupt",
            tool_executions=tool_executions_count,
            reason="destructive tool never reached — agent answered without it",
        )
    over = " OVER-BUDGET" if wall >= scenario.budget_s else ""
    log.info(
        "synthetic_traffic.scenario_done",
        scenario=scenario.key,
        wall_s=round(wall, 2),
        budget_s=scenario.budget_s,
        over_budget=bool(over),
        intent=intent or "-",
        tool_executions=tool_executions_count,
        flag=flag,
        resumes=resumes,
    )

    return result


# ---------------------------------------------------------------------------
# Cleanup pass
# ---------------------------------------------------------------------------


async def _cleanup_documents(db: Any, user: Any, cutoff: datetime) -> int:
    """Delete stale synthetic documents through the normal file lifecycle.

    ``cleanup`` only soft-deleted Collections, so ingested Documents — and the
    content hashes behind ``uq_documents_org_checksum_live`` — accreted
    forever, until every id in ``INGEST_PAPER_IDS`` was permanently
    dedup-blocked and the ingest scenario reported TOOL-FAILED for a pipeline
    that worked. That index is partial (``WHERE is_deleted = false``), so a
    soft delete frees the hash while preserving the "never hard DELETE,
    documents own storage objects" rule the migration set.
    """
    from sqlalchemy import select

    from src.models.document import Document
    from src.models.organization import Organization
    from src.services.documents.file_service import FileService

    org_id = getattr(user, "organization_id", None)
    if org_id is None:
        return 0

    # Belt and braces: only ever purge inside the dedicated synthetic org. A
    # synthetic user left pinned to a REAL org by a pre-R5-M28 bootstrap must
    # not lose that tenant's documents.
    org_name = (
        await db.execute(select(Organization.name).where(Organization.id == org_id))
    ).scalar_one_or_none()
    if org_name != SYNTH_ORG_NAME:
        log.warning(
            "synthetic_traffic.cleanup",
            warning="skipping document purge: user is not in the synthetic org",
            org_name=org_name,
        )
        return 0

    document_ids = (
        (
            await db.execute(
                select(Document.id)
                .where(
                    Document.organization_id == org_id,
                    Document.created_at < cutoff,
                    Document.is_deleted == False,  # noqa: E712
                )
                .order_by(Document.created_at)
            )
        )
        .scalars()
        .all()
    )

    deleted = 0
    file_service = FileService(db)
    for document_id in document_ids:
        document = await db.get(Document, document_id)
        if document is None or document.is_deleted:
            continue
        try:
            # This shared path removes the source object, canonical text,
            # figure chunks, DO-KB data source, and graph satellites.
            await file_service.delete_file(document, user)
            deleted += 1
        except Exception as exc:
            log.warning(
                "synthetic_traffic.cleanup",
                warning="document cleanup failed",
                document_id=str(document_id),
                error=str(exc),
            )
    return deleted


async def cleanup(db: Any, user: Any, older_than_hours: int = 6) -> None:
    """Soft-delete synthetic Collections (projects) older than ``older_than_hours``.

    Targets only Collections:
    - owned via a Workspace owned by the synthetic user
    - whose name starts with ``SYNTH_PREFIX``
    - created more than ``older_than_hours`` hours ago
    - not already soft-deleted

    Uses ``BaseModel.soft_delete()`` to match the codebase convention.
    Never raises — errors are logged and ignored so the cleanup failure
    cannot block a future run.
    """
    try:
        from sqlalchemy import delete, select

        from src.models.collection import Collection
        from src.models.project_memory import ProjectMemory
        from src.models.workspace import Workspace

        cutoff = datetime.now(tz=timezone.utc) - timedelta(hours=older_than_hours)

        docs_deleted = await _cleanup_documents(db, user, cutoff)
        if docs_deleted:
            log.info("synthetic_traffic.cleanup", documents_deleted=docs_deleted)

        # Fetch all Workspaces owned by the synthetic user
        ws_result = await db.execute(
            select(Workspace).where(
                Workspace.owner_id == user.id,
                Workspace.is_deleted == False,  # noqa: E712
            )
        )
        workspaces = ws_result.scalars().all()
        if not workspaces:
            log.debug("synthetic_traffic.cleanup", msg="no workspaces found")
            return

        ws_ids = [ws.id for ws in workspaces]

        # Fetch matching stale Collections
        coll_result = await db.execute(
            select(Collection).where(
                Collection.workspace_id.in_(ws_ids),
                Collection.name.like(f"{SYNTH_PREFIX}%"),
                Collection.created_at < cutoff,
                Collection.is_deleted == False,  # noqa: E712
            )
        )
        collections = coll_result.scalars().all()

        if not collections:
            log.debug("synthetic_traffic.cleanup", msg="nothing to clean up")
            return

        deleted = 0
        collection_ids = [collection.id for collection in collections]
        if collection_ids:
            await db.execute(
                delete(ProjectMemory).where(
                    ProjectMemory.project_id.in_(collection_ids)
                )
            )
        for coll in collections:
            try:
                coll.soft_delete()
                deleted += 1
            except Exception as exc:
                log.warning(
                    "synthetic_traffic.cleanup",
                    warning="soft_delete failed",
                    collection_id=str(coll.id),
                    error=str(exc),
                )

        if deleted:
            await db.commit()

        log.info(
            "synthetic_traffic.cleanup",
            deleted=deleted,
            cutoff_hours=older_than_hours,
        )
    except Exception as exc:
        log.warning(
            "synthetic_traffic.cleanup",
            warning="cleanup pass failed",
            error=str(exc),
        )


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def _choose_scenario_rotate() -> Scenario:
    """Pick one scenario deterministically by rotating through the list.

    Uses ``int(time.time() / ROTATE_WINDOW_S) % len(SCENARIOS)`` so the
    same scenario is chosen across any restart within the same 20-minute
    window, and coverage cycles fully in ``len(SCENARIOS) × 20 min``.
    """
    idx = int(time.time() / ROTATE_WINDOW_S) % len(SCENARIOS)
    return SCENARIOS[idx]


async def _main(args: argparse.Namespace) -> int:
    from src.core.database import AsyncSessionLocal
    from src.services.agent.observability import configure_langsmith

    configure_langsmith()

    # Field-level encryption must be initialized before any User write — the
    # User model encrypts first_name/last_name on insert. The app does this in
    # its startup lifespan; this standalone CronJob script does not boot the
    # app, so initialize it here or the synthetic-user INSERT raises
    # "Encryption not initialized" (ENCRYPTION_MASTER_KEY is in app-secrets).
    try:
        from src.core.encryption import initialize_encryption

        initialize_encryption()
    except Exception as exc:  # pragma: no cover - best effort
        log.warning("synthetic_traffic: encryption init failed: %s", exc)

    run_cleanup_only = getattr(args, "cleanup", False)

    async with AsyncSessionLocal() as db:
        user = await _get_or_create_synth_user(db)

        if run_cleanup_only:
            await cleanup(db, user)
            return 0

        # Build graph once; reuse across scenarios
        graph = await _build_graph()

        scenarios_to_run: List[Scenario] = []

        if getattr(args, "all", False):
            scenarios_to_run = list(SCENARIOS)
        elif getattr(args, "scenario", None):
            key = args.scenario
            if key not in SCENARIOS_BY_KEY:
                log.error(
                    "synthetic_traffic.main",
                    error=f"Unknown scenario key: {key!r}. "
                    f"Valid keys: {list(SCENARIOS_BY_KEY)}",
                )
                return 1
            scenarios_to_run = [SCENARIOS_BY_KEY[key]]
        else:
            # Default: --rotate
            scenarios_to_run = [_choose_scenario_rotate()]

        results: List[TurnResult] = []
        for scenario in scenarios_to_run:
            try:
                result = await run_scenario(scenario, graph, db, user)
                results.append(result)
            except Exception as exc:  # noqa: BLE001
                log.error(
                    "synthetic_traffic.scenario_unhandled",
                    scenario=scenario.key,
                    error=str(exc),
                )

        total_wall = sum(r.wall_clock_s for r in results)
        errored = [r for r in results if r.error]
        # Counted and logged, but deliberately not folded into the exit code:
        # whether the model reaches a destructive tool is nondeterministic, so
        # failing the CronJob on it would trade a silent miss for a noisy one.
        # The flag + this counter are the signal to alert on.
        # Both shapes of "the destructive action did not happen": never
        # reached (MISSING-INTERRUPT) and reached-but-failed (TOOL-FAILED).
        # Counting only the first is what let a confirmed(1) run that ingested
        # nothing report expectations_unmet=0.
        unmet = [
            r
            for r in results
            if r.flag == MISSING_INTERRUPT_FLAG or r.flag.startswith(TOOL_FAILED_FLAG)
        ]
        log.info(
            "synthetic_traffic.sweep_done",
            scenarios=len(results),
            total_wall_s=round(total_wall, 2),
            errored=len(errored),
            expectations_unmet=len(unmet),
        )

        # Always run cleanup at the end (unless this was a --cleanup-only run)
        await cleanup(db, user)

    return 1 if errored else 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Synthetic agent-traffic generator for the NOUS dev cluster."
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--rotate",
        action="store_true",
        default=False,
        help=(
            "Run ONE scenario chosen by rotating through the list based on "
            f"the current time (default; cycles every {ROTATE_WINDOW_S}s)."
        ),
    )
    mode.add_argument(
        "--all",
        action="store_true",
        default=False,
        help="Run every scenario once sequentially.",
    )
    mode.add_argument(
        "--scenario",
        metavar="KEY",
        help=f"Run one named scenario. Valid keys: {[s.key for s in SCENARIOS]}",
    )
    mode.add_argument(
        "--cleanup",
        action="store_true",
        default=False,
        help="Run ONLY the cleanup pass (delete stale synthetic projects).",
    )

    args = parser.parse_args()

    # Before argparse-adjacent work turns into DB writes and LLM spend.
    assert_safe_environment()

    # If no flag is given, behave as --rotate
    if not (args.all or args.scenario or args.cleanup):
        args.rotate = True

    return asyncio.run(_main(args))


if __name__ == "__main__":
    raise SystemExit(main())
