# Agent orchestration mutation checks

Task 1 mutation-verified the durable run transition guard and the producer's
queued-graph cancellation guard. Each proof disabled one guard, ran its
focused behavioral test and observed an assertion failure, restored the
source from a byte-for-byte pre-mutation copy, compared the restored file,
and reran the same test successfully. The temporary copies were kept under
`/tmp/task1-*.pre-mutation.py`; no mutant was committed.

Run commands from the repository worktree root. The examples use the task's
fresh Python environment and isolated Redis service.

## Durable status transition guard

- **Source and guard:** `backend/src/services/agent/agent_run_service.py`,
  `_transition_predicate`, lines 401-404. The default transition
  must require a pre-terminal status and a null `cancel_requested_at` at the
  SQL update point. The terminal branch also permits an idempotent write of
  the same terminal status.
- **Covering test:**
  `backend/tests/unit/agent/test_agent_run_service.py::test_terminal_update_loses_to_completed`.
- **Mutation:** replace the default `return and_(AgentRun.status.in_(...),
  AgentRun.cancel_requested_at.is_(None))` predicate with `return True`.
- **Command:**

  ```sh
  PYTHONPATH=backend REDIS_URL=redis://127.0.0.1:32775/15 LANGCHAIN_TRACING_V2=false LANGSMITH_TRACING=false ENVIRONMENT=testing .venv/bin/python -m pytest -c backend/pytest.ini -q --tb=short backend/tests/unit/agent/test_agent_run_service.py::test_terminal_update_loses_to_completed
  ```

- **Observed mutant failure:** exit 1; a stale terminal update returned
  `awaiting_confirmation` where the fresh-session assertion required
  `completed`.
- **Restore proof:** copied the saved pre-mutation source back and ran
  `cmp -s backend/src/services/agent/agent_run_service.py
  /tmp/task1-agent_run_service.pre-mutation.py` (exit 0).
- **Restored command result:** same command, exit 0; `1 passed`.

## Queued graph cancellation monitor

- **Source and guard:** `backend/src/services/agent/agent_execution_service.py`,
  `_invoke_graph_with_cancellation_monitor`, lines 2374-2375. The
  monitor must poll the persisted owner-scoped marker and cancel and join the
  graph task when a stop is committed.
- **Covering test:**
  `backend/tests/unit/services/test_agent_cancellation.py::test_resumed_cancel_interrupts_graph`.
- **Mutation:** change the monitor's `if await cancellation_requested():`
  immediately before `graph_task.cancel()` to
  `if False and await cancellation_requested():`.
- **Command:**

  ```sh
  PYTHONPATH=backend REDIS_URL=redis://127.0.0.1:32775/15 LANGCHAIN_TRACING_V2=false LANGSMITH_TRACING=false ENVIRONMENT=testing .venv/bin/python -m pytest -c backend/pytest.ini -q --tb=short backend/tests/unit/services/test_agent_cancellation.py::test_resumed_cancel_interrupts_graph
  ```

- **Observed mutant failure:** exit 1; the test timed out waiting for the
  blocked graph's cancellation event because the monitor did not interrupt
  and join it.
- **Restore proof:** copied the saved pre-mutation source back and ran
  `cmp -s backend/src/services/agent/agent_execution_service.py
  /tmp/task1-agent_execution_service.pre-mutation.py` (exit 0).
- **Restored command result:** same command, exit 0; `1 passed`.

## Fix round 1: terminal winner publication and slot preservation

All mutation commands below ran from the Task 1 worktree root with this
environment: `PYTHONPATH=backend REDIS_URL=redis://127.0.0.1:32775/15
ORCHESTRATION_TEST_REDIS_URL=redis://127.0.0.1:32775/15
ORCHESTRATION_TEST_DATABASE_URL=postgresql://orch_test:orch_local_test@127.0.0.1:32774/orch_test
LANGCHAIN_TRACING_V2=false LANGSMITH_TRACING=false ENVIRONMENT=testing`.
The test cases use disposable PostgreSQL schemas and the isolated Redis DB 15.
Each mutant was applied once, failed its listed assertion, then was restored
from the saved copy; the restored source SHA-256 is recorded for comparison.
Logs are retained in `/tmp/task1-mutation-*.log`.

### COMPLETED cannot overtake accepted STOPPING

- **Source and guard:** `backend/src/services/agent/agent_run_service.py`,
  `_transition_predicate`, lines 394-400. For a terminal target, the guard
  allows an idempotent same-terminal write or an uncancelled pre-STOP status;
  it must reject both `STOPPING` and a row with `cancel_requested_at`.
- **Owning test:**
  `backend/tests/integration/test_agent_run_concurrency.py::test_post_monitor_stop_wins_through_real_runner_publication[initial]`
  and `[resume]`. These run each producer through the graph/publication path
  and assert durable `CANCELLED`, retained cancellation marker, released slot,
  one acknowledgement, and matching L1/Redis state without a result.
- **Mutation:** replace the line 397 return expression with
  `return (AgentRun.status == status.value) | AgentRun.status.in_(_ACTIVE_RUN_STATUSES)`.
- **Command:**

  ```sh
  PYTHONPATH=backend REDIS_URL=redis://127.0.0.1:32775/15 ORCHESTRATION_TEST_REDIS_URL=redis://127.0.0.1:32775/15 ORCHESTRATION_TEST_DATABASE_URL=postgresql://orch_test:orch_local_test@127.0.0.1:32774/orch_test LANGCHAIN_TRACING_V2=false LANGSMITH_TRACING=false ENVIRONMENT=testing .venv/bin/pytest -q 'backend/tests/integration/test_agent_run_concurrency.py::test_post_monitor_stop_wins_through_real_runner_publication[initial]' 'backend/tests/integration/test_agent_run_concurrency.py::test_post_monitor_stop_wins_through_real_runner_publication[resume]'
  ```

- **Observed mutant failure:** exit 1; `2 failed`. Both initial and resumed
  runners left durable `completed` where the owning assertion required
  `cancelled`. Full output: `/tmp/task1-mutation-terminal-red.log`.
- **Restore proof:** copied `/tmp/task1-agent_run_service.fix-pre-mutation.py`
  back and compared with `cmp -s`; both hashes were
  `cc7b27632be38ff913b2ed5df11b428dfe4c44caad8ec0195129bcb2df24b143`.
- **Restored result:** same command, exit 0; `2 passed, 1 warning`.
  Output: `/tmp/task1-mutation-terminal-green.log`.

### Active-thread collision retry keeps the valid correlation

- **Source and guard:** `backend/src/services/agent/agent_run_service.py`,
  active-thread conflict retry at lines 317-329. When a failed contender
  observes that the previous slot owner completed after rollback, it retries
  once using the same valid thread ID. A thread-less fallback is reserved for
  a separately and positively identified missing-thread FK.
- **Owning test:**
  `backend/tests/integration/test_agent_run_concurrency.py::test_postgres_slot_collision_never_drops_valid_thread_after_winner_finishes`.
  With real PostgreSQL writers, it verifies B preserves the correlation and
  C still collides with B as the sole active checkpoint writer.
- **Mutation:** change the retry insert's `thread_id=thread_uuid` at line 325
  to `thread_id=None`.
- **Command:**

  ```sh
  PYTHONPATH=backend REDIS_URL=redis://127.0.0.1:32775/15 ORCHESTRATION_TEST_REDIS_URL=redis://127.0.0.1:32775/15 ORCHESTRATION_TEST_DATABASE_URL=postgresql://orch_test:orch_local_test@127.0.0.1:32774/orch_test LANGCHAIN_TRACING_V2=false LANGSMITH_TRACING=false ENVIRONMENT=testing .venv/bin/pytest -q backend/tests/integration/test_agent_run_concurrency.py::test_postgres_slot_collision_never_drops_valid_thread_after_winner_finishes
  ```

- **Observed mutant failure:** exit 1; `1 failed`. Row B became a RUNNING
  row with `thread_id=None`; the test failed its correlation assertion.
  Output: `/tmp/task1-mutation-slot-red.log`.
- **Restore proof:** copied `/tmp/task1-agent_run_service.slot-pre-mutation.py`
  back and compared with `cmp -s`; both hashes were
  `cc7b27632be38ff913b2ed5df11b428dfe4c44caad8ec0195129bcb2df24b143`.
- **Restored result:** same command, exit 0; `1 passed, 1 warning`.
  Output: `/tmp/task1-mutation-slot-green.log`.

### Delayed Redis writer cannot replace a durable terminal

- **Source and guard:** `backend/src/services/agent/job_store.py`,
  `_REDIS_GUARDED_WRITE_SCRIPT`, lines 372-375. Lua compares the old and
  candidate statuses in one atomic operation, preserving an existing terminal
  state against delayed writers.
- **Owning test:**
  `backend/tests/integration/test_agent_run_concurrency.py::test_delayed_real_redis_running_writer_cannot_replace_cancelled_winner`.
  The test pauses a stale writer, commits STOPPING then CANCELLED through the
  real database, and releases the writer against real Redis.
- **Mutation:** replace the line 373 condition with `if false then`, bypassing
  the old-terminal refusal for a stale RUNNING candidate.
- **Command:**

  ```sh
  PYTHONPATH=backend REDIS_URL=redis://127.0.0.1:32775/15 ORCHESTRATION_TEST_REDIS_URL=redis://127.0.0.1:32775/15 ORCHESTRATION_TEST_DATABASE_URL=postgresql://orch_test:orch_local_test@127.0.0.1:32774/orch_test LANGCHAIN_TRACING_V2=false LANGSMITH_TRACING=false ENVIRONMENT=testing .venv/bin/pytest -q backend/tests/integration/test_agent_run_concurrency.py::test_delayed_real_redis_running_writer_cannot_replace_cancelled_winner
  ```

- **Observed mutant failure:** exit 1; `1 failed`. Redis ended as `running`
  instead of `cancelled`. Output: `/tmp/task1-mutation-redis-red.log`.
- **Restore proof:** copied `/tmp/task1-job_store.redis-pre-mutation.py` back
  and compared with `cmp -s`; both hashes were
  `82906c81b80c0821a1c4fe0fabb0e8c30a8e54829fb66fd597222d371f876b21`.
- **Restored result:** same command, exit 0; `1 passed, 1 warning`.
  Output: `/tmp/task1-mutation-redis-green.log`.

### Sweeper acknowledges an eligible old Stop as cancellation

- **Source and guard:** `backend/src/tasks/agent_run_tasks.py`, cancellation
  target selection at lines 475-480. An already eligible old STOPPING marker
  must request `CANCELLED`; a newly arrived Stop remains active and keeps its
  grace/lease.
- **Owning test:**
  `backend/tests/integration/test_agent_run_concurrency.py::test_real_sweeper_acknowledges_old_stop_and_corrects_cached_terminal`.
  It uses real PostgreSQL and Redis and verifies durable cancellation, marker
  retention, active-slot release, L1/Redis correction, and result removal
  before checking counters.
- **Mutation:** change the eligible cancellation assignment at line 479 from
  `JobStatus.CANCELLED` to `JobStatus.FAILED`.
- **Command:**

  ```sh
  PYTHONPATH=backend REDIS_URL=redis://127.0.0.1:32775/15 ORCHESTRATION_TEST_REDIS_URL=redis://127.0.0.1:32775/15 ORCHESTRATION_TEST_DATABASE_URL=postgresql://orch_test:orch_local_test@127.0.0.1:32774/orch_test LANGCHAIN_TRACING_V2=false LANGSMITH_TRACING=false ENVIRONMENT=testing .venv/bin/pytest -q backend/tests/integration/test_agent_run_concurrency.py::test_real_sweeper_acknowledges_old_stop_and_corrects_cached_terminal
  ```

- **Observed mutant failure:** exit 1; `1 failed`. Durable status remained
  `stopping` instead of becoming `cancelled`; the test failed before its
  counter assertions. Output: `/tmp/task1-mutation-sweeper-requested-red-v2.log`.
- **Restore proof:** copied `/tmp/task1-agent_run_tasks.requested-pre-mutation-v2.py`
  back and compared with `cmp -s`; both hashes were
  `b0f413eed53f16202e1b82ceb794bd9b7c21ee82059456b16a1fa64ad9fa5fcb`.
- **Restored result:** same command, exit 0; `1 passed, 1 warning`.
  Output: `/tmp/task1-mutation-sweeper-requested-green.log`.

### Sweeper consumes the committed winner before release and accounting

- **Source and guard:** `backend/src/tasks/agent_run_tasks.py`, effective
  decision consumption at line 511. Only the committed winning status may
  decide whether a freshly arriving Stop retains its lease, and what terminal
  cache projection and counters to publish.
- **Owning test:**
  `backend/tests/integration/test_agent_run_concurrency.py::test_real_sweeper_consumes_stop_or_completion_winning_after_refresh[stopping]`
  and `[completed]`. Both force an actual PostgreSQL race after cache refresh;
  the Stop case checks durable STOPPING and lease retention before accounting,
  then checks later cancellation, while the completion case checks terminal
  completion, lease release, and terminal cache mirroring.
- **Mutation:** replace `effective_status = decision.effective_status` at line
  511 with `effective_status = requested_status`.
- **Command:**

  ```sh
  PYTHONPATH=backend REDIS_URL=redis://127.0.0.1:32775/15 ORCHESTRATION_TEST_REDIS_URL=redis://127.0.0.1:32775/15 ORCHESTRATION_TEST_DATABASE_URL=postgresql://orch_test:orch_local_test@127.0.0.1:32774/orch_test LANGCHAIN_TRACING_V2=false LANGSMITH_TRACING=false ENVIRONMENT=testing .venv/bin/pytest -q 'backend/tests/integration/test_agent_run_concurrency.py::test_real_sweeper_consumes_stop_or_completion_winning_after_refresh[stopping]'
  ```

- **Observed mutant failure:** exit 1; `1 failed`. The real row remained
  STOPPING but the incorrect local FAILED target released the sweeper lease;
  the owning assertion detected `lease_owner=None` before counters. Output:
  `/tmp/task1-mutation-sweeper-winner-red.log`.
- **Restore proof:** copied `/tmp/task1-agent_run_tasks.winner-pre-mutation.py`
  back and compared with `cmp -s`; both hashes were
  `b0f413eed53f16202e1b82ceb794bd9b7c21ee82059456b16a1fa64ad9fa5fcb`.
- **Restored result:** both `[stopping]` and `[completed]`, exit 0;
  `2 passed, 1 warning`. Output: `/tmp/task1-mutation-sweeper-winner-green.log`.

### Synchronous L1 mirrors preserve terminal and STOPPING winners

- **Source and guard:** `backend/src/services/agent/agent_execution_service.py`,
  synchronous `_set_job` L1 admission at line 175. The shared cache transition
  guard must reject a newer nonterminal candidate after a terminal or STOPPING
  winner while retaining ordinary nonterminal freshness ordering.
- **Owning tests:**
  `backend/tests/unit/services/test_job_store_terminal_durability.py::test_sync_set_job_preserves_terminal_and_stopping_winners`
  covers COMPLETED and STOPPING winners. The same focused selection covers
  nonterminal freshness and sync terminal-write rejection controls.
- **Behavioral RED:** with the old freshness-only L1 predicate, the two winner
  cases failed because RUNNING replaced COMPLETED and STOPPING; both control
  cases passed. Output:
  `.superpowers/sdd/2026-09-25-agent-orchestration-repairs/task1-sync-l1-guard-red-v2.log`.
- **Mutation:** replace the shared transition check at line 175 with
  `if existing is None or _job_store._is_newer_or_equal(data, existing):`.
- **Command:**

  ```sh
  PYTHONPATH=backend LANGCHAIN_TRACING_V2=false LANGSMITH_TRACING=false ENVIRONMENT=testing REDIS_URL=redis://127.0.0.1:32775/15 .venv/bin/python -m pytest -c backend/pytest.ini -q -o log_cli=false --tb=short backend/tests/unit/services/test_job_store_terminal_durability.py -k sync_set_job
  ```

- **Observed mutant failure:** exit 1; both the COMPLETED and STOPPING winner
  assertions failed because L1 ended at RUNNING. Both controls passed. Output:
  `.superpowers/sdd/2026-09-25-agent-orchestration-repairs/task1-sync-l1-guard-mutation-red.log`.
- **Restore proof:** restored from `/tmp/task1-sync-l1-guard-pre-mutation.py`
  and compared with `cmp -s`; the source and snapshot both hashed to
  `57cc7579cb27cd54d160a9b94f7490916f0caed256591dee57c29a4209be6d14`.
- **Restored result:** same command, exit 0; `4 passed, 5 deselected`. Output:
  `.superpowers/sdd/2026-09-25-agent-orchestration-repairs/task1-sync-l1-guard-restored-green.log`.
