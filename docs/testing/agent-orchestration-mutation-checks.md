# Agent orchestration mutation checks

Task 1 mutation-verified the durable run transition guard and the producer's
queued-graph cancellation guard. Each proof disabled one guard, ran its
focused behavioral test and observed an assertion failure, restored the
source from a byte-for-byte pre-mutation copy, compared the restored file,
and reran the same test successfully. The temporary copies were kept under
`/tmp/task1-*.pre-mutation.py`; no mutant was committed.

Run commands from the repository worktree root. The examples use the task's
fresh Python environment and isolated Redis service.

## Documentation redaction amendment — 2026-09-26

The command examples below now require externally supplied disposable-service
URLs. This documentation-only amendment removes the task's synthetic local
connection values to follow `docs/AGENTS.md`; it does not change the recorded
commands' test selections, outcomes, source hashes or restoration evidence.
Exact historical commands remain in the local ignored execution logs. Task 1's
scoped acceptance remains tied to `66e435a053a7f2e1c8b81c07c9899b1229b6df9d`;
this amendment makes no new runtime or release-verification claim.

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
  PYTHONPATH=backend REDIS_URL="${REDIS_URL:?}" LANGCHAIN_TRACING_V2=false LANGSMITH_TRACING=false ENVIRONMENT=testing .venv/bin/python -m pytest -c backend/pytest.ini -q --tb=short backend/tests/unit/agent/test_agent_run_service.py::test_terminal_update_loses_to_completed
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
  PYTHONPATH=backend REDIS_URL="${REDIS_URL:?}" LANGCHAIN_TRACING_V2=false LANGSMITH_TRACING=false ENVIRONMENT=testing .venv/bin/python -m pytest -c backend/pytest.ini -q --tb=short backend/tests/unit/services/test_agent_cancellation.py::test_resumed_cancel_interrupts_graph
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
environment: `PYTHONPATH=backend REDIS_URL="${REDIS_URL:?}"
ORCHESTRATION_TEST_REDIS_URL="${ORCHESTRATION_TEST_REDIS_URL:?}"
ORCHESTRATION_TEST_DATABASE_URL="${ORCHESTRATION_TEST_DATABASE_URL:?}"
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
  PYTHONPATH=backend REDIS_URL="${REDIS_URL:?}" ORCHESTRATION_TEST_REDIS_URL="${ORCHESTRATION_TEST_REDIS_URL:?}" ORCHESTRATION_TEST_DATABASE_URL="${ORCHESTRATION_TEST_DATABASE_URL:?}" LANGCHAIN_TRACING_V2=false LANGSMITH_TRACING=false ENVIRONMENT=testing .venv/bin/pytest -q 'backend/tests/integration/test_agent_run_concurrency.py::test_post_monitor_stop_wins_through_real_runner_publication[initial]' 'backend/tests/integration/test_agent_run_concurrency.py::test_post_monitor_stop_wins_through_real_runner_publication[resume]'
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
  PYTHONPATH=backend REDIS_URL="${REDIS_URL:?}" ORCHESTRATION_TEST_REDIS_URL="${ORCHESTRATION_TEST_REDIS_URL:?}" ORCHESTRATION_TEST_DATABASE_URL="${ORCHESTRATION_TEST_DATABASE_URL:?}" LANGCHAIN_TRACING_V2=false LANGSMITH_TRACING=false ENVIRONMENT=testing .venv/bin/pytest -q backend/tests/integration/test_agent_run_concurrency.py::test_postgres_slot_collision_never_drops_valid_thread_after_winner_finishes
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
  PYTHONPATH=backend REDIS_URL="${REDIS_URL:?}" ORCHESTRATION_TEST_REDIS_URL="${ORCHESTRATION_TEST_REDIS_URL:?}" ORCHESTRATION_TEST_DATABASE_URL="${ORCHESTRATION_TEST_DATABASE_URL:?}" LANGCHAIN_TRACING_V2=false LANGSMITH_TRACING=false ENVIRONMENT=testing .venv/bin/pytest -q backend/tests/integration/test_agent_run_concurrency.py::test_delayed_real_redis_running_writer_cannot_replace_cancelled_winner
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
  PYTHONPATH=backend REDIS_URL="${REDIS_URL:?}" ORCHESTRATION_TEST_REDIS_URL="${ORCHESTRATION_TEST_REDIS_URL:?}" ORCHESTRATION_TEST_DATABASE_URL="${ORCHESTRATION_TEST_DATABASE_URL:?}" LANGCHAIN_TRACING_V2=false LANGSMITH_TRACING=false ENVIRONMENT=testing .venv/bin/pytest -q backend/tests/integration/test_agent_run_concurrency.py::test_real_sweeper_acknowledges_old_stop_and_corrects_cached_terminal
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
  PYTHONPATH=backend REDIS_URL="${REDIS_URL:?}" ORCHESTRATION_TEST_REDIS_URL="${ORCHESTRATION_TEST_REDIS_URL:?}" ORCHESTRATION_TEST_DATABASE_URL="${ORCHESTRATION_TEST_DATABASE_URL:?}" LANGCHAIN_TRACING_V2=false LANGSMITH_TRACING=false ENVIRONMENT=testing .venv/bin/pytest -q 'backend/tests/integration/test_agent_run_concurrency.py::test_real_sweeper_consumes_stop_or_completion_winning_after_refresh[stopping]'
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
  PYTHONPATH=backend LANGCHAIN_TRACING_V2=false LANGSMITH_TRACING=false ENVIRONMENT=testing REDIS_URL="${REDIS_URL:?}" .venv/bin/python -m pytest -c backend/pytest.ini -q -o log_cli=false --tb=short backend/tests/unit/services/test_job_store_terminal_durability.py -k sync_set_job
  ```

- **Observed mutant failure:** exit 1; both the COMPLETED and STOPPING winner
  assertions failed because L1 ended at RUNNING. Both controls passed. Output:
  `.superpowers/sdd/2026-09-25-agent-orchestration-repairs/task1-sync-l1-guard-mutation-red.log`.
- **Restore proof:** restored from `/tmp/task1-sync-l1-guard-pre-mutation.py`
  and compared with `cmp -s`; the source and snapshot both hashed to
  `57cc7579cb27cd54d160a9b94f7490916f0caed256591dee57c29a4209be6d14`.
- **Restored result:** same command, exit 0; `4 passed, 5 deselected`. Output:
  `.superpowers/sdd/2026-09-25-agent-orchestration-repairs/task1-sync-l1-guard-restored-green.log`.

## Fix round 2 — Redis payload shape and winner preservation

Evidence dated 2026-09-26 against review base
`7ef93001c82f8062bf3934b3a85d9ed4f9112cde`; restored final source SHA-256 is
`236eca83ab196f5aa421f2b5671874741518236476b43cda2f3fa0b8959cd29a`.
The fix-round-1 delayed-writer mutation above tested the earlier Lua writer and
is historical evidence only. The final writer is the shared Python
WATCH/MULTI guard in `backend/src/services/agent/job_store.py:385-432`; all
status/freshness comparison, safe winner merging, JSON encoding, and SETEX now
occur within the watched transaction. Python JSON preserves nested empty
arrays and objects. Watch conflicts have five bounded attempts, and the async
pipeline context is closed on return or cancellation.

The focused commands below ran with this environment (the displayed values
are the disposable test services):
`PYTHONPATH=backend LANGCHAIN_TRACING_V2=false LANGSMITH_TRACING=false
ENVIRONMENT=testing REDIS_URL="${REDIS_URL:?}"
ORCHESTRATION_TEST_REDIS_URL="${ORCHESTRATION_TEST_REDIS_URL:?}"`; the delayed-writer
test also used
`ORCHESTRATION_TEST_DATABASE_URL="${ORCHESTRATION_TEST_DATABASE_URL:?}"`.

### Redis JSON shape survives cold reads and merges

- **Owning tests:**
  `backend/tests/integration/test_job_store_redis_publication.py::test_cold_l1_job_poll_preserves_nested_json_shapes`,
  `::test_repeated_completed_publication_preserves_winner_payload_and_enriches_scope`,
  and `::test_stale_completed_publication_keeps_concurrent_redis_winner`.
  The cold-L1 test calls the real polling endpoint and checks nested empty
  arrays/objects in the decoded Redis bytes and response model.
- **Behavioral RED command:**

  ```sh
  PYTHONPATH=backend LANGCHAIN_TRACING_V2=false LANGSMITH_TRACING=false ENVIRONMENT=testing REDIS_URL="${REDIS_URL:?}" ORCHESTRATION_TEST_REDIS_URL="${ORCHESTRATION_TEST_REDIS_URL:?}" .venv/bin/pytest -c backend/pytest.ini -q -o log_cli=false backend/tests/integration/test_job_store_redis_publication.py
  ```

- **Observed failures:** `3 failed`; Redis turned `tool_executions: []` and
  nested arrays into objects, and repeated/stale COMPLETED publications exposed
  the later answer. After moving the poll assertion ahead of raw-byte checks,
  the actual endpoint produced `JobStatusResponse` validation error
  `tool_executions: Input should be a valid list` (`input_value={}`). Its
  focused output is
  `.superpowers/sdd/2026-09-25-agent-orchestration-repairs/task1-round2-api-red.log`;
  the three-case RED is
  `.superpowers/sdd/2026-09-25-agent-orchestration-repairs/task1-round2-redis-red.log`.
- **Restored focused result:** the original three Redis cases passed after the
  WATCH/MULTI implementation (`3 passed`), recorded in
  `.superpowers/sdd/2026-09-25-agent-orchestration-repairs/task1-round2-redis-green-initial.log`.
  The final expanded cases and all
  owning tests are included in the final `173 passed` combined run recorded in
  `.superpowers/sdd/2026-09-25-agent-orchestration-repairs/task1-round2-affected-final.log`.

### Same-terminal COMPLETED publication retains its result

- **Source and guard:** `backend/src/services/agent/job_store.py:155-164`,
  `_preserve_terminal_payload`. A compatible existing COMPLETED result must
  survive a repeated COMPLETED candidate even when the candidate carries a
  different result. CANCELLED and FAILED candidates continue to remove both
  result and confirmation.
- **Mutation:** neutralize the existing-COMPLETED preservation condition by
  replacing `and _status_value(existing) == JobStatus.COMPLETED` with
  `and False`.
- **Command:** the shared focused environment above with
  ```sh
  PYTHONPATH=backend LANGCHAIN_TRACING_V2=false LANGSMITH_TRACING=false ENVIRONMENT=testing REDIS_URL="${REDIS_URL:?}" ORCHESTRATION_TEST_REDIS_URL="${ORCHESTRATION_TEST_REDIS_URL:?}" .venv/bin/pytest -c backend/pytest.ini -q -o log_cli=false backend/tests/integration/test_job_store_redis_publication.py::test_repeated_completed_publication_preserves_winner_payload_and_enriches_scope
  ```
- **Observed mutant failure:** exit 1; the real two-client Redis test saw
  `different later answer` instead of the existing `first completed answer`
  (`1 failed`). Output:
  `.superpowers/sdd/2026-09-25-agent-orchestration-repairs/task1-round2-preservation-condition-red.log`.
- **Restore proof:** copied `/tmp/task1-round2-job_store-r2.py` back;
  `sha256sum` of the saved and restored source matched exactly at
  `236eca83ab196f5aa421f2b5671874741518236476b43cda2f3fa0b8959cd29a`.
- **Restored result:** the same test passed (`1 passed`), recorded in
  `.superpowers/sdd/2026-09-25-agent-orchestration-repairs/task1-round2-preservation-condition-green.log`.

### Missing status cannot replace COMPLETED or STOPPING

- **Source and guard:** `backend/src/services/agent/job_store.py:105-113`,
  `_cache_transition_allowed`. Preserve Lua's prior refusal of a candidate
  whose status is unknown when the existing payload is terminal or STOPPING;
  ordinary valid nonterminal freshness remains unchanged.
- **Owning tests:**
  `backend/tests/unit/services/test_job_store_redis_guard.py::test_redis_write_with_unknown_status_cannot_replace_absorbing_winner`
  and
  `backend/tests/integration/test_job_store_redis_publication.py::test_real_redis_unknown_status_cannot_replace_absorbing_winner`,
  each parameterized over COMPLETED and STOPPING.
- **Mutation:** replace the unknown-status branch with its former freshness-only
  behavior, `return existing is None or _is_newer_or_equal(candidate, existing)`.
- **Command:**

  ```sh
  PYTHONPATH=backend LANGCHAIN_TRACING_V2=false LANGSMITH_TRACING=false ENVIRONMENT=testing REDIS_URL="${REDIS_URL:?}" ORCHESTRATION_TEST_REDIS_URL="${ORCHESTRATION_TEST_REDIS_URL:?}" .venv/bin/pytest -c backend/pytest.ini -q -o log_cli=false backend/tests/unit/services/test_job_store_redis_guard.py::test_redis_write_with_unknown_status_cannot_replace_absorbing_winner backend/tests/integration/test_job_store_redis_publication.py::test_real_redis_unknown_status_cannot_replace_absorbing_winner
  ```
- **Observed mutant failure:** exit 1; all four cases overwrote the terminal or
  STOPPING payload with the newer missing-status candidate (`4 failed`). Output:
  `.superpowers/sdd/2026-09-25-agent-orchestration-repairs/task1-round2-unknown-status-red.log`.
- **Restore proof:** restored `/tmp/task1-round2-job_store-unknown-guard.py`;
  the saved and restored source both hashed to
  `236eca83ab196f5aa421f2b5671874741518236476b43cda2f3fa0b8959cd29a`.
- **Restored result:** the same cases passed (`4 passed`), recorded in
  `.superpowers/sdd/2026-09-25-agent-orchestration-repairs/task1-round2-unknown-status-green.log`.

### Final WATCH guard protects a delayed real-Redis writer

- **Source and guard:** `backend/src/services/agent/job_store.py:393-401`,
  the `_cache_transition_allowed` decision inside the WATCH/MULTI transaction.
  This prevents a delayed RUNNING candidate from replacing an accepted,
  PostgreSQL-authoritative CANCELLED winner.
- **Owning test:**
  `backend/tests/integration/test_agent_run_concurrency.py::test_delayed_real_redis_running_writer_cannot_replace_cancelled_winner`.
  It uses a real PostgreSQL cancellation decision and real Redis, pausing the
  stale writer after WATCH/GET until the terminal publication commits.
- **Mutation:** neutralize only the shared transition decision in the writer's
  conditional (`or not _cache_transition_allowed(...)` becomes `or False`);
  the separate authorization check for a STOPPING candidate remains intact.
- **Command:** run from the Task 1 worktree with the shared focused environment
  above:

  ```sh
  PYTHONPATH=backend LANGCHAIN_TRACING_V2=false LANGSMITH_TRACING=false ENVIRONMENT=testing REDIS_URL="${REDIS_URL:?}" ORCHESTRATION_TEST_REDIS_URL="${ORCHESTRATION_TEST_REDIS_URL:?}" ORCHESTRATION_TEST_DATABASE_URL="${ORCHESTRATION_TEST_DATABASE_URL:?}" .venv/bin/pytest -c backend/pytest.ini -q -o log_cli=false backend/tests/integration/test_agent_run_concurrency.py::test_delayed_real_redis_running_writer_cannot_replace_cancelled_winner
  ```
- **Observed mutant failure:** the clean repeated mutant failed at the Redis
  winner assertion (`running` instead of `cancelled`, `1 failed`). Output:
  `.superpowers/sdd/2026-09-25-agent-orchestration-repairs/task1-round2-watch-guard-red-final.log`.
  The first attempt is preserved in
  `.superpowers/sdd/2026-09-25-agent-orchestration-repairs/task1-round2-watch-guard-red.log`;
  it showed the same Redis winner failure plus a test-proxy `get` logging
  error. The proxy was updated to forward ordinary Redis client methods, and
  the mutation was repeated cleanly.
- **Restore proof:** restored `/tmp/task1-round2-job_store-watch-guard.py`;
  saved and restored source hashes matched at
  `236eca83ab196f5aa421f2b5671874741518236476b43cda2f3fa0b8959cd29a`.
- **Restored result:** the same real-Postgres/Redis test passed (`1 passed`),
  output
  `.superpowers/sdd/2026-09-25-agent-orchestration-repairs/task1-round2-watch-guard-green.log`.

## Fix round 3 — normal publisher and terminal poll handoff

Evidence is against round-3 base `7cf7fa869d142bb2047c6aa45422f3947e51f7bc`.
The normal awaited `set_job` now receives a typed committed/refused/unavailable
Redis outcome. A compatible same-status winning snapshot is adopted by L1
only while the publisher still owns its exact terminal reservation and local
generation. A process-local completion event gates terminal poll snapshots;
the API checks both at entry and after its awaited Redis refresh. This keeps
the terminal reservation itself in L1 during Redis work, so the existing
absorbing-state guard continues rejecting stale synchronous progress writes.
Redis absence, EXEC errors, exhausted WATCH retries, and publisher cancellation
release waiting polls to the authorized L1 fallback. Redis outages still do
not establish a global completed-result winner across processes.

The three guards were mutated independently and each owning test failed on the
named behavior. Each mutation was restored by copying the pre-mutation file;
`sha256sum` matched the saved and restored bytes exactly:

- **Winner adoption:** changed `and outcome.state in {"committed", "refused"}`
  to `and False` in `job_store.set_job`. The real-Redis normal publisher/API
  poll test failed because the response returned `later local loser` instead
  of `original Redis winner` (`1 failed`). Test:
  `test_normal_completed_publication_and_poll_adopt_redis_winner`. Output:
  `task1-round3-adoption-mutation-red.log`. Restore hash:
  `45ac10296b230d6fa6329e1d9d2b45cf641d7380bef801f6cdffa09ddb4610a6`.
- **Generation/gate ownership:** changed the `finally` cleanup from
  `_retire_pending_publication(job_id, pending_publication)` to an unconditional
  `_retire_pending_publication(job_id)`. The older publisher removed the newer
  generation's pending gate; the test failed when the current generation entry
  was missing after the old completion (`1 failed`). Test:
  `test_poll_waits_for_current_superseding_terminal_generation`. Output:
  `task1-round3-generation-mutation-red.log`. Restore hash:
  `45ac10296b230d6fa6329e1d9d2b45cf641d7380bef801f6cdffa09ddb4610a6`.
- **Final visibility boundary:** replaced the post-Redis-refresh
  `await _get_job_for_poll(job_id)` with `None`. A poll that began on RUNNING
  and yielded in Redis returned the provisional terminal response before its
  publisher resolved (`1 failed`). Test:
  `test_poll_started_before_publication_rechecks_after_redis_await`. Output:
  `task1-round3-visibility-mutation-red.log`. Restore hash:
  `c8539050303a4ec493b6cb5591cebc55cb45858725ecd6f8aa62c2fd13c292c8`.

The final owning selection was rerun after all three byte restores. It includes
the existing delayed background writer, strict durable-decision rejection,
Redis terminal parity, actual API polling, nested JSON shape, the late poll
boundary, stale synchronous progress, superseding generation, poll and
publisher cancellation, Redis EXEC failure, and five-conflict exhaustion.
Exact command and output are in the round-3 report; observed result was
`182 passed, 1 baseline Pydantic warning in 8.54s`, log
`.superpowers/sdd/2026-09-25-agent-orchestration-repairs/task1-round3-affected-final.log`.

## Fix round 4 — durable-fallback publication boundary

Evidence is against round-4 base `185dd927153c26f1df0b7a2a4a48b78c88d945b3`.
After `get_run_fallback` returns, `get_job_status` re-enters the same
pending-aware local poll selector before returning a durable projection or a
missing-row 404. A local snapshot that appeared during the database await then
uses the existing owner validation and response shaping. With no local snapshot,
the tenant-filtered durable fallback is unchanged. The new scheduling test uses
fakes for the durable-read and Redis-publication boundaries; it does not claim
real PostgreSQL or Redis coverage. Its two cases hold a normal `set_job`
publisher while a poll's fallback read is released, covering both an existing
projection row and a missing row. The existing ordinary fallback test remains
the no-publication control.

- **Behavioral RED:**
  `test_poll_waits_for_publication_after_durable_fallback_read[True]` returned
  the durable COMPLETED response with no result before the publisher resolved;
  `[False]` returned 404 in the same interval. Both failed at the pending-poll
  assertion. Output:
  `.superpowers/sdd/2026-09-25-agent-orchestration-repairs/task1-round4-fallback-boundary-red.log`.
- **Mutation:** replaced only the post-fallback
  `job = await _get_job_for_poll(job_id)` selector with `job = None`. Both
  parametrized cases failed again at the same assertion, demonstrating that
  this boundary check prevents both premature projection responses and 404s.
  Output:
  `.superpowers/sdd/2026-09-25-agent-orchestration-repairs/task1-round4-fallback-boundary-mutation-red.log`.
- **Restore and GREEN:** restored the saved source byte-for-byte; SHA256 before
  mutation and after restore was
  `f821199089df2a7071355a123bcdec8e4d9bc22f62721d250a0b421a8f4027d6`.
  Both new cases plus the ordinary no-publication fallback control passed
  (`3 passed`), output
  `.superpowers/sdd/2026-09-25-agent-orchestration-repairs/task1-round4-fallback-boundary-restored-green.log`.
- **Focused command:**

  ```sh
  PYTHONPATH=backend LANGCHAIN_TRACING_V2=false LANGSMITH_TRACING=false ENVIRONMENT=testing .venv/bin/python -m pytest -c backend/pytest.ini -q -o log_cli=false --tb=short backend/tests/unit/api/test_agent_job_poll_fallback.py::test_poll_waits_for_publication_after_durable_fallback_read backend/tests/unit/api/test_agent_job_poll_fallback.py::test_redis_miss_falls_back_to_agent_runs_projection
  ```

- **Final affected selection:** reran the round-3 owning selection plus
  `backend/tests/unit/api/test_agent_job_poll_fallback.py` under the same
  disposable PostgreSQL/Redis environment shown in the round-4 report. Result:
  `199 passed, 1 baseline Pydantic schema_extra warning in 10.66s`, log
  `.superpowers/sdd/2026-09-25-agent-orchestration-repairs/task1-round4-affected-final.log`.
  This includes 12 actual-PostgreSQL concurrency cases. The Redis publication
  integration module includes real-Redis paths, while not every publication
  case uses Redis; the new poll/fallback interleaving itself uses fakes.

Round-3 hash provenance clarification: `c8539050303a4ec493b6cb5591cebc55cb45858725ecd6f8aa62c2fd13c292c8`
is the byte-identical immediate restore hash recorded by the round-3 mutation.
The committed execute.py at round-4 base `185dd927...` hashes to
`d897c0a16d9c73dd3a5de698654423563f584bb517438d1e053a51dcf0734434` because
Black subsequently collapsed the multiline `get_job_for_poll` import to one
line. Comparing the retained `c853...` snapshot with the committed base shows
only that import wrapping change. The round-4 source saved before its mutation
and restored byte-identically hashes to `f821199089df2a7071355a123bcdec8e4d9bc22f62721d250a0b421a8f4027d6`.

Round-4 scoped quality: Ruff and Black passed for `execute.py` and the existing
fallback test module; isort and `git diff --check` exited 0. The advisory mypy
check on this modified legacy module reports nine existing missing-annotation
errors. Running the same check against the round-4 base copy reports the same
nine errors on the same pre-existing functions; the new test and helpers have
annotations. Logs are `task1-round4-{ruff,black,isort,diff-check,mypy}.log` and
`task1-round4-mypy-baseline.log` in the scratch directory. No new Python file
was added.
