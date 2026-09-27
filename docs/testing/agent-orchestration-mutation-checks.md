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

## Task 2 — durable tool-operation claims/results (2026-09-26)

Evidence is against immutable Task 2 review base
`bc221b6eaff63e6701a8bdd9bb993e67c122ec0a`. The package adds one additive
`agent_tool_operations` table and leaves historical receipt rows intact. The
final source bytes for the three independently mutated guards match the
restored SHA256 values below; each focused mutant was run after source restore,
and the owning selections below passed after all final test/format edits.

The focused behavioral RED is preserved at
`.superpowers/sdd/2026-09-25-agent-orchestration-repairs/task-2-behavior-red.log`.
It recorded four implementation failures; the integration harness and fixture
failures are reported separately in the ignored Task 2 completion report.
Final focused GREEN commands, using externally supplied local test service
URLs, are:

```sh
PYTHONPATH=backend LANGCHAIN_TRACING_V2=false LANGSMITH_TRACING=false ENVIRONMENT=testing REDIS_URL="${REDIS_URL:?}" .venv/bin/python -m pytest -c backend/pytest.ini -q -o log_cli=false backend/tests/unit/agent/test_tool_receipts.py backend/tests/unit/agent/test_tool_operation_results.py backend/tests/unit/agent/test_tool_registry_policy.py backend/tests/unit/agent/test_tools_r7_hardening.py backend/tests/unit/services/test_agent_tools.py
PYTHONPATH=backend LANGCHAIN_TRACING_V2=false LANGSMITH_TRACING=false ENVIRONMENT=testing REDIS_URL="${REDIS_URL:?}" ORCHESTRATION_TEST_DATABASE_URL="${ORCHESTRATION_TEST_DATABASE_URL:?}" .venv/bin/python -m pytest -c backend/pytest.ini -q -o log_cli=false backend/tests/integration/test_agent_tool_operation_concurrency.py
```

Observed results were 81 unit tests and 11 real-PostgreSQL integration tests
passing. The integration tests create disposable schemas in local PostgreSQL,
then inspect effects and operation rows from fresh sessions after executor
return. They cover project, note, and document-link writes; rollback on a
result-storage failure; concurrent uniqueness; authenticated result isolation;
and idempotent result save/return/replay. Provider/model dispatch and draft
generation/status are offline fakes. The draft replay case exercises pending →
pending → completed with one generation call and the same task identity, and
checks there is no open SQL transaction across the status waits. No live model,
provider, external message, or third-party side effect was used. The local
Redis URL is required by test configuration; the database concurrency and
atomicity evidence is from PostgreSQL.

Each mutation below was applied to the named source bytes, run against the
focused owning test, then restored with byte-for-byte comparison. The logs in
`.superpowers/sdd/2026-09-25-agent-orchestration-repairs/` retain command,
baseline/mutant/restored hashes, exit code, and pytest output.

| Guard proved | Owning test and temporary mutation | Observed failure | Restored source SHA256 / log |
| --- | --- | --- | --- |
| Unique claim winner | `test_postgres_same_scoped_operation_concurrently_creates_once`; remove `ON CONFLICT DO NOTHING` from `tool_operations.py`. | PostgreSQL reported duplicate `operation_id` primary key while both calls reached the insert. | `8c0e75e24cf93a8bac7531247daa733a3d7aa4ed31ef8f49b698d6055bf84eaa`; `task-2-mutation-uniqueness.log`. |
| Claim owner CAS | `test_postgres_only_claim_owner_can_record_completion`; remove the `owner_token` predicate from `_cas_operation`. | The stale owner completed the claim; test failed because it expected the guarded update to raise. | `8c0e75e24cf93a8bac7531247daa733a3d7aa4ed31ef8f49b698d6055bf84eaa`; `task-2-mutation-owner.log`. |
| Argument fingerprint CAS | Same owner-CAS test; remove the `args_hash` compare from `_cas_operation`. | A changed fingerprint completed the same operation identity; the fingerprint assertion failed. | `8c0e75e24cf93a8bac7531247daa733a3d7aa4ed31ef8f49b698d6055bf84eaa`; `task-2-mutation-full-scope.log`. |
| Thread identity-scope CAS | Same owner-CAS test; remove the `thread_id` predicate from `_cas_operation`. | The test changed the stored thread inside a savepoint; completion then succeeded, so the scope assertion failed with `DID NOT RAISE RuntimeError`. | `8c0e75e24cf93a8bac7531247daa733a3d7aa4ed31ef8f49b698d6055bf84eaa`; `task-2-mutation-thread-scope.log`. |
| Local effect/result atomicity | `test_postgres_local_effect_and_claim_roll_back_on_result_failure`; insert `await session.commit()` after the local helper returns but before `complete_operation`. | A fresh session found one durable project where rollback requires zero (`projects == 1`). | `a3db9052a5ffdcecac7d4e3e99c33ed9c562625cc30d76adc6b3f886ed38a90d`; `task-2-mutation-local-early-commit.log`. |
| Claim-store fail-closed | `test_postgres_external_claim_store_failure_never_dispatches`; after the claim-store exception, synthesize a claimed result and let control reach dispatch. | The fake external dispatcher ran once (`dispatch_count == 1`, expected 0). This proves storage-failure behavior, separately from actor/scope authorization. | `a3db9052a5ffdcecac7d4e3e99c33ed9c562625cc30d76adc6b3f886ed38a90d`; `task-2-mutation-store-fail-closed.log`. |
| Authenticated scope before result lookup | `test_postgres_authentication_precedes_saved_result_lookup`; temporarily make the operation-context matcher always true. | A second valid actor received the saved `project_id`; the test failed on the explicit no-result-leak assertion. | `a3db9052a5ffdcecac7d4e3e99c33ed9c562625cc30d76adc6b3f886ed38a90d`; `task-2-mutation-auth-scope.log`. |
| Same-fingerprint uncertain replay | `test_postgres_uncertain_external_effect_blocks_new_call_id_replay`; omit `unknown` from the same-turn external barrier states. | A fresh provider call ID dispatched a second time (`dispatch_count == 2`, expected 1). | `8c0e75e24cf93a8bac7531247daa733a3d7aa4ed31ef8f49b698d6055bf84eaa`; `task-2-mutation-uncertain-replay.log`. |
| Read invalidation after attempted mutation | `test_reads_invalidate_prior_cache_after_an_attempted_mutation`; neutralize the latest-mutation position update. | The later verification read reused the pre-mutation cached result. | `ab846a94248dc1e19a2b7cf140ca7fd1b351a783b83d9cdc98f0c3d5e87e3156`; `task-2-mutation-read-invalidation.log`. |

An initial weaker atomicity mutation changed the helper's `defer_local_commit`
argument to false. It was detected, but the legacy service's internal commit
closed the surrounding transaction before refresh, so the injected result
failure was not reached. That attempt is recorded in
`task-2-mutation-local-atomicity.log`; the early-commit mutation above is the
owning result-boundary proof. The first fingerprint mutator selector found two
matching `args_hash` predicates and stopped before modifying source or running
tests. The selector was narrowed to `_cas_operation` and then produced the
recorded behavioral failure. The independent `thread_id` CAS mutant proves the
row cannot be completed after its thread scope changes.

Migration validation found the single head `agent_ops_20260925`, directly
following `u3v4w5x6y7z8`; the exact migration/fixture are scoped to that
historical affected-schema parent. `alembic history` output is retained in
`task-2-migration-green.log`. Changed-file Ruff, Black, and isort passed, and
Mypy passed for all four newly added Python files. See the ignored
`task-2-report.md` for complete commands, operation limits, and the intentional
executor compatibility tightening: injected `db`/`current_user` mutation
calls now also require an authenticated checkpointed operation key. The only
production executor caller is the graph `_nodes_tools` path; tests verify an
unscoped injected mutation fails closed. `ProjectService` REST commit defaults
remain unchanged.

## Task 2 review repair round — 2026-09-26

This evidence records the Task 2 response to the scoped Astra review at code
base `fdd736e02a31c8d4bf8254e8616f2470965d2611`. The repository's current
backend and testing contracts remain in [`backend.md`](../engineering/backend.md)
and [`testing.md`](../engineering/testing.md). The final Task 2 application
hashes after the wording-only status-message correction are:

- `backend/src/services/agent/tools_impl.py`:
  `27ce9a67082f88aed7075dcf01bfd86e906400beaad388d8fde5ee87ca516d46`
- `backend/src/services/agent/tool_operations.py`:
  `3fc0f894a10f8392b86e28e92a703eaaff6b54f0b4b22e97192591ce4c66db1b`
- `backend/tests/integration/test_agent_tool_operation_concurrency.py`:
  `dc0a015188f688f98e887673bd72f340bd8744777ec91db1bf3b426c1b4aad9c`

### Required regression results

| Review item | Owning behavior and proof | Mutant failure and restored evidence |
| --- | --- | --- |
| R1: keep a committed draft task identity across cancellation | The actual-PostgreSQL `test_postgres_draft_cancellation_preserves_dispatched_identity` cancels after the task ID is committed, verifies the dispatched row and identity survive, then replays to completed with one generation call. `test_postgres_mark_unknown_cannot_erase_recorded_dispatch` verifies the store predicate itself. | The combined mutant disabled the executor's dispatched-state cancellation guard and allowed `mark_unknown` to update a dispatched row. It failed because the durable row became `unknown` instead of `dispatched` (`task-2-round2-mutation-r1-combined-red.log`). The independent `mark_unknown` mutant failed because the expected CAS error was not raised (`task-2-round2-mutation-retention-red-v2.log`). Restored store-guard test passed (`task-2-round2-mark-unknown-green.log`); the restored full PostgreSQL file passed 18 cases (`task-2-round2-integration-final.log`). The combined mutant was restored to the saved pre-mutation copies; those copies hash to `598b8d5fd47ada8913308a293e7025d6d7c98f28bd5dc682ac475d8b4a279279` and `3fc0f894a10f8392b86e28e92a703eaaff6b54f0b4b22e97192591ce4c66db1b`. |
| R2: retain bounded failed/partial observations at the graph boundary | `test_postgres_graph_keeps_bounded_failed_observation_on_fresh_and_replay` asserts the actual saved observation and both graph messages preserve status plus task, project, and draft identities under the authoritative result cap. | Removing protected observation retention made the fresh graph result differ from the saved bounded observation (`task-2-round2-mutation-error-shaping-final-red.log`). Restoring the guard returned the same protected observation on fresh execution and replay (`task-2-round2-mutation-error-shaping-final-green.log`). Restored source hash before later status wording was `59bc7e2dca1bc046cb6760f2e5958f9463294da08ee94bcbc743ecab2d72df12`. |
| R3: cap pending and terminal recovered results | `test_postgres_draft_replay_uses_shared_status_and_bounds_saved_results` uses a multibyte, many-row shared status snapshot and checks both saved and returned pending and terminal results, completion replay equality, IDs, and omission metadata. | Bypassing pending-result rebounding produced 201,772 encoded bytes against the 32,768-byte cap (`task-2-round2-mutation-pending-cap-final-red.log`). Bypassing terminal-result rebounding produced 201,648 bytes (`task-2-round2-mutation-terminal-cap-final-red.log`). Both restored focused runs passed (`task-2-round2-mutation-pending-cap-final-green.log`, `task-2-round2-mutation-terminal-cap-final-green.log`). Restored source hash for these mutations was `59bc7e2dca1bc046cb6760f2e5958f9463294da08ee94bcbc743ecab2d72df12`. |
| R4: fail closed on recovery-store faults and return the committed CAS winner | `test_postgres_draft_recovery_store_failure_is_uncertain_and_retryable` forces a result-write failure, checks bounded uncertainty and preserved task/project/draft IDs, then retries after storage recovers. `test_postgres_concurrent_draft_recovery_returns_committed_winner` races real PostgreSQL completion updates and compares both callers with the fresh-session winner. | Returning the candidate result after storage failure removed `error_category` and failed the uncertainty assertion (`task-2-round2-mutation-store-failure-final-red.log`). Returning local uncertainty after a CAS loss diverged from the real committed winner (`task-2-round2-mutation-cas-winner-final-red.log`). Restored focused tests passed (`task-2-round2-mutation-store-failure-final-green.log`, `task-2-round2-mutation-cas-winner-final-green.log`). Restored source hash was `59bc7e2dca1bc046cb6760f2e5958f9463294da08ee94bcbc743ecab2d72df12`. |
| R5: use shared status recovery without a process-local waiter | The same PostgreSQL replay test starts with an empty process-local generation cache, exercises the real shared-status lookup against a fake Redis status store, observes pending then terminal status from a cold worker without sleeping or starting generation a second time, and checks saved/returned caps. | Replacing the shared snapshot lookup with the 105-second process-local waiter triggered the test's explicit forbidden-wait assertion and lost the result-bounds metadata (`task-2-round2-mutation-r5-local-wait-final-red.log`). Restored shared-status run passed (`task-2-round2-mutation-r5-local-wait-final-green.log`). Restored source hash was `59bc7e2dca1bc046cb6760f2e5958f9463294da08ee94bcbc743ecab2d72df12`. |

The focused command template below uses externally supplied disposable service
URLs. The owning integration file passed with 18 tests on local PostgreSQL;
generation and provider calls were offline fakes, while shared Redis status
was a fake backing store. Tracing was disabled. No live provider/model call was
made.

```sh
PYTHONPATH=backend REDIS_URL="${REDIS_URL:?}" ORCHESTRATION_TEST_DATABASE_URL="${ORCHESTRATION_TEST_DATABASE_URL:?}" LANGCHAIN_TRACING_V2=false LANGSMITH_TRACING=false ENVIRONMENT=testing .venv/bin/python -m pytest -c backend/pytest.ini -q -o log_cli=false --tb=short backend/tests/integration/test_agent_tool_operation_concurrency.py
```

The separate restored quality selections were 81 owning unit tests, 88 direct
compatibility tests, and 9 operation-result tests. New operation/integration
files passed mypy with no issues in two source files. A root-side normalized
comparison of `tools_impl.py` diagnostics against the review base reported 48
baseline messages and 47 current messages, with no new message or increased
multiplicity; one pre-existing `no-any-return` message was removed. The earlier
combined 55-error attempt remains only a failed diagnostic attempt and is not
recorded as a passing check. Full local logs are preserved under the ignored
`.superpowers/sdd/2026-09-25-agent-orchestration-repairs/` scratch directory.

The accepted unsaved-dispatch window remains a recovery limit: if draft
generation accepts work but task identity persistence fails or cancellation
occurs before the recorder commits, the row remains non-replayable `unknown`.
The code cannot prove whether that unrecorded external task was accepted, so it
does not dispatch it again automatically. Post-dispatch cancellation and status
read failures preserve the committed identity and return a bounded,
non-retryable pending observation.

## Task 3 whole-batch capability preflight

Evidence dated 2026-09-26 against base
`8afdb76ee1775fa678673aa40a896162a7cecdd5`. Both execution boundaries must
reject an entire mixed batch before dedupe, approval, effect dispatch, or a
durable operation claim. The tests supply a valid Task 2 operation protocol,
turn ID, user, organization, and thread, so the no-prefix assertions do not
pass because of an unrelated missing-scope guard.

### Main tool node

- **Source and guard:** `backend/src/services/agent/_nodes_tools.py:752-756`,
  the `if unavailable` branch in `tool_node`.
- **Owning test:**
  `backend/tests/unit/agent/test_task3_batch_contract.py::test_main_tool_node_rejects_whole_batch_before_mutation_claim`.
- **Mutation:** temporarily changed that condition to
  `if False and unavailable`, allowing both the supported write and the
  unregistered sibling to reach `_execute_single_tool`.
- **Command:**

  ```sh
  PYTHONPATH=backend REDIS_URL="${REDIS_URL:?}" LANGCHAIN_TRACING_V2=false LANGSMITH_TRACING=false ENVIRONMENT=testing .venv/bin/python -m pytest -c backend/pytest.ini -q -o log_cli=false --tb=short backend/tests/unit/agent/test_task3_batch_contract.py::test_main_tool_node_rejects_whole_batch_before_mutation_claim
  ```

- **Observed mutant failure:** exit 1; the expected zero executor awaits were
  two. The failure specifically reports both calls reached the effect seam.
  Full final-source output:
  `.superpowers/sdd/2026-09-25-agent-orchestration-repairs/task-3-batch-guard-mutation-v2-red.log`.
- **Restore proof:** repeated against the formatted final source after the
  earlier working-source run. The source was restored from its byte-exact
  pre-mutation copy; `cmp -s` succeeded and both source hashes were
  `db2b6534b61e2b9cae4d7c88eda5ff98c0128a859e7925d1dbb4e72f7d61b00c`.
- **Restored result:** same command, exit 0; `1 passed`. Output:
  `.superpowers/sdd/2026-09-25-agent-orchestration-repairs/task-3-batch-guard-mutation-v2-green.log`.

### Filtered specialist tool node

- **Source and guard:** `backend/src/services/agent/_nodes_tools.py:988-994`,
  the `if unavailable` branch in `make_filtered_tool_node`.
- **Owning test:**
  `backend/tests/unit/agent/test_task3_batch_contract.py::test_filtered_node_rejects_whole_mixed_batch_before_mutation_claim`.
- **Mutation:** temporarily changed that condition to
  `if False and unavailable`, allowing the supported write prefix to reach
  `_execute_single_tool` before the out-of-scope sibling was skipped.
- **Command:**

  ```sh
  PYTHONPATH=backend REDIS_URL="${REDIS_URL:?}" LANGCHAIN_TRACING_V2=false LANGSMITH_TRACING=false ENVIRONMENT=testing .venv/bin/python -m pytest -c backend/pytest.ini -q -o log_cli=false --tb=short backend/tests/unit/agent/test_task3_batch_contract.py::test_filtered_node_rejects_whole_mixed_batch_before_mutation_claim
  ```

- **Observed mutant failure:** exit 1; `_execute_single_tool` was awaited once
  for `create_project_note`, violating the zero-prefix assertion. Full output:
  `.superpowers/sdd/2026-09-25-agent-orchestration-repairs/task-3-filtered-guard-mutation-v2-red.log`.
- **Restore proof:** repeated against the formatted final source after the
  earlier working-source run. The source was restored from its byte-exact
  pre-mutation copy; `cmp -s` succeeded and both source hashes were
  `db2b6534b61e2b9cae4d7c88eda5ff98c0128a859e7925d1dbb4e72f7d61b00c`.
- **Restored result:** same command, exit 0; `1 passed`. Output:
  `.superpowers/sdd/2026-09-25-agent-orchestration-repairs/task-3-filtered-guard-mutation-v2-green.log`.

## Task 3 review repair round — 2026-09-26

These focused mutation checks cover the Task 3 round-2 candidate delta from
`9efd2f58248d0d86d70a6fcbbc662f4ed1e90851`. They exercise the specialist
whole-batch routing order, caller-scope rejection for ephemeral snapshots, and
the post-provider soft-delete filter. The provider/model calls are offline
fakes. Each source file was copied before mutation, restored from that copy,
compared byte-for-byte with `cmp -s`, and retested using the same selection.

### Specialist rejection precedes approval and terminal routes

- **Source and guard:**
  `backend/src/services/agent/subgraphs/_factory.py`, `should_continue`; the
  `unavailable_tool_calls(...)` preflight must precede approval, error-circuit,
  and loop-ceiling routing.
- **Owning selection:** direct router checks plus the compiled writing,
  research, and data parent-graph checks for under-budget, ceiling, and
  error-limit states:
  `tests/unit/agent/test_task3_runtime_projection.py` filtered by
  `specialist_router_sends_unavailable_batch_to_preflight_first or
  compiled_specialist_rejects_unavailable_batch_before_terminal_branches`.
- **Mutation:** temporarily changed the preflight condition to
  `if False and unavailable_tool_calls(...)`.
- **Observed mutant failure:** exit 1; 11 failed and 1 passed. The direct
  writing router selected interrupt, forced synthesis, or reflection under
  pressure, and compiled parent tests observed forbidden interrupt/forced/
  reflection routes instead of paired batch errors. The retained compiled
  assertions include zero executor/operation claims, no further model call,
  and one parent memory-save final.
- **Restore proof:** restored from `/tmp/task3-factory.pre-mutation.py`;
  `cmp -s` succeeded and the restored source SHA-256 was
  `c2e2ea80f0c6a8d74ad11c405e88a0e4cf4716b7908104bace0e26414c8fe0d5`.
- **Restored result:** same selection, exit 0; 12 passed. Red and green logs:
  `task-3-r2-r1-router-mutation-red.log` and
  `task-3-r2-r1-router-mutation-restored-green.log`.

### Ephemeral hydration rejects an explicitly anchored caller

- **Source and guard:**
  `backend/src/services/agent/runtime_snapshot.py`, the threadless/jobless
  ephemeral `identity_matches` exception. The fallback is permitted only when
  the supplied and checkpoint caller state has no thread or job anchor.
- **Owning selection:** the four explicit thread/job argument/state cases and
  the unanchored ephemeral control in
  `tests/unit/agent/test_task3_runtime_projection.py` filtered by
  `threadless_ephemeral`.
- **Mutation:** temporarily removed the checks for
  `expected_thread_id is None` and `not expected_job_id`.
- **Observed mutant failure:** exit 1; 3 explicit-anchor cases failed and 2
  controls passed. Caller-supplied thread and job anchors incorrectly hydrated
  the anchorless row.
- **Restore proof:** restored from `/tmp/task3-runtime_snapshot.pre-mutation.py`;
  `cmp -s` succeeded and the restored source SHA-256 was
  `6a3df195ef459dd5133ba245ba245f6e1f3bfb7854bf70e3a6b50d8a31fac386`.
- **Restored result:** same selection, exit 0; 5 passed. Red and green logs:
  `task-3-r2-r4-ephemeral-guard-mutation-red.log` and
  `task-3-r2-r4-ephemeral-guard-restored-green.log`.

### DO resolution excludes a source deleted after provider dispatch

- **Source and guard:** `backend/src/services/do_kb/resolve.py`, the
  `Document.is_deleted == False` predicate on post-provider resolution.
- **Owning selection:** actual compiled writing search-by-title → exact
  selected-ID retrieval → DO provider fake → post-provider resolution, filtered
  by `compiled_writing_resolves_named_local_source_before_retrieval and
  do-deleted` in `tests/unit/agent/test_task3_compiled_context.py`. The fixture
  models the selected document becoming soft-deleted after dispatch and returns
  its stale chunk only if the query predicate is absent.
- **Mutation:** temporarily removed `.where(Document.is_deleted == False)`.
- **Observed mutant failure:** exit 1; the compiled flow accepted the stale
  result and returned the deleted document's chunk instead of the expected
  `no_scoped_chunks` response.
- **Restore proof:** restored from `/tmp/task3-resolve.pre-mutation.py`;
  `cmp -s` succeeded and the restored source SHA-256 was
  `102d65f39db9d3647e54233b8b64ec89321a45de16b478578bd0d4e463fc26ee`.
- **Restored result:** same selection, exit 0; 1 passed. Red and green logs:
  `task-3-r2-r4-deleted-guard-mutation-red.log` and
  `task-3-r2-r4-deleted-guard-restored-green.log`.

The three commands above ran from `backend/` using `../.venv/bin/pytest` and
saved complete output under the ignored `.superpowers/sdd/2026-09-25-agent-orchestration-repairs/`
directory. The restored hashes are the exact pre-mutation files, not hashes
of normalized text.

## Task 4 Astra repair round — 2026-09-26

This dated evidence covers required review repairs on top of immutable
candidate `40f069f6dd85b5a5961368760e80a430673d1fdc`. Tests use local fakes for
model/provider boundaries; the connected identity path uses the task-owned
PostgreSQL fixture. No external provider or network calls were made. Commands
run from the repository worktree root. Test environment values are supplied by
the task harness and are omitted here.

### Duplicate identity observations are idempotent

- **Source and guard:** `backend/src/services/agent/identity_ledger.py:842`,
  `merge_identity_ledger`; an observation is merged only if its stable
  observation ID is not already in `processed_set`.
- **Owning test:**
  `backend/tests/unit/agent/test_identity_ledger.py::test_duplicate_specialist_observation_replay_preserves_one_provenance_record`.
  It caps a real tool result, merges the same specialist observation twice,
  then checks the ledger is unchanged, processed provenance appears once, and
  tool/call/turn provenance remains stable.
- **Mutation:** changed
  `if observation_id and observation_id not in processed_set:` to
  `if observation_id:`.
- **Command:**

  ```sh
  PYTHONPATH=backend LANGCHAIN_TRACING_V2=false LANGSMITH_TRACING=false ENVIRONMENT=testing .venv/bin/python -m pytest -c backend/pytest.ini -q --tb=short -o log_cli=false backend/tests/unit/agent/test_identity_ledger.py::test_duplicate_specialist_observation_replay_preserves_one_provenance_record
  ```

- **Observed mutant failure:** exit 1; the exact ledger equality assertion at
  `test_identity_ledger.py:183` failed after duplicate replay changed the
  retained record.
- **Restore proof:** `cmp -s` succeeded after restoring the saved source.
  Pre-mutation and restored SHA-256:
  `46a52106586a5613d3dc39b20caf93143850fb8dd2d762067aaee6d70ee69ea3`.
  Mutant SHA-256: `62c2c850d67dea84028fdceb36ddd689daf070ab6cde0da04f5b5ed2bd9098dd`.
- **Restored result:** same command, exit 0; `1 passed`.

### Completed-mutation priority is scoped to the active turn

- **Source and guard:** `backend/src/services/agent/identity_ledger.py:704`,
  `_completed_mutation`; completed mutation priority requires the record's
  `turn_id` to equal the current turn.
- **Owning test:**
  `backend/tests/unit/agent/test_identity_ledger.py::test_completed_mutation_retention_is_limited_to_active_turn`.
  The saturated ledger checks explicit pinning, current-turn mutation
  priority, eviction of an old mutation, and next-turn loss of the old
  mutation's special priority.
- **Mutation:** replaced
  `and record.get("turn_id") == current_turn_id` with `and True`.
- **Command:**

  ```sh
  PYTHONPATH=backend LANGCHAIN_TRACING_V2=false LANGSMITH_TRACING=false ENVIRONMENT=testing .venv/bin/python -m pytest -c backend/pytest.ini -q --tb=short -o log_cli=false backend/tests/unit/agent/test_identity_ledger.py::test_completed_mutation_retention_is_limited_to_active_turn
  ```

- **Observed mutant failure:** exit 1; the stale completed mutation remained
  in the retained records, failing the stale-ID eviction assertion at
  `test_identity_ledger.py:506`.
- **Restore proof:** `cmp -s` succeeded. Pre-mutation and restored SHA-256:
  `46a52106586a5613d3dc39b20caf93143850fb8dd2d762067aaee6d70ee69ea3`.
  Mutant SHA-256: `09988f7ab135c2407fe933444bf69d87426903522569c1edf19d7514e44e0493`.
- **Restored result:** same command, exit 0; `1 passed`.

### Active draft request matching preserves exact instructions and source IDs

- **Source and guards:**
  `backend/src/services/research/draft_generation_service.py:340-341`,
  `_request_hash`; canonical request identity retains the exact validated
  instruction string and effective selected document IDs. A different hash
  returns a conflict before another worker or status publication.
- **Owning test:**
  `backend/tests/unit/services/test_draft_generation_llm_client.py::test_active_generation_conflicts_on_exact_instruction_or_source_change`.
  The two cases call the real service registration method with a matching
  active request whose instructions differ only by trailing whitespace, or
  whose selected document differs. They assert conflict, no task ID, no
  `_fire_and_forget`, and no status publication.
- **Instruction mutation:** changed the hashed `instructions` value to
  `instructions.strip()` when non-null.
- **RED command:**

  ```sh
  PYTHONPATH=backend LANGCHAIN_TRACING_V2=false LANGSMITH_TRACING=false ENVIRONMENT=testing .venv/bin/python -m pytest -c backend/pytest.ini -q --tb=short -o log_cli=false 'backend/tests/unit/services/test_draft_generation_llm_client.py::test_active_generation_conflicts_on_exact_instruction_or_source_change[exact-instruction-whitespace]'
  ```

- **Instruction RED:** exit 1; stripping trailing whitespace incorrectly
  reused the active task, so the direct conflict assertion at
  `test_draft_generation_llm_client.py:331` failed because
  `result.get("error_category")` was `None`; the returned result instead had
  the active task ID and “already in progress” status.
- **Selection mutation:** changed the hashed `"document_ids": document_ids`
  to `"document_ids": []`.
- **Selection RED command:**

  ```sh
  PYTHONPATH=backend LANGCHAIN_TRACING_V2=false LANGSMITH_TRACING=false ENVIRONMENT=testing .venv/bin/python -m pytest -c backend/pytest.ini -q --tb=short -o log_cli=false 'backend/tests/unit/services/test_draft_generation_llm_client.py::test_active_generation_conflicts_on_exact_instruction_or_source_change[different-document-selection]'
  ```

- **Selection RED:** exit 1; clearing selected IDs incorrectly reused the
  active task, so the same direct conflict assertion failed because
  `result.get("error_category")` was `None`; the active task ID was returned.
- **Restore proof:** each mutant was restored from its separate saved source;
  `cmp -s` succeeded after both restorations. Pre-mutation and restored SHA-256
  for `draft_generation_service.py`:
  `7c470666737a21da0b5ef926a76235f417e95e12179750fc035088a9beea065b`.
  Instruction mutant SHA-256:
  `82939f6f4dbe3c02369d2d0bb86b531910e8b3b756caf600d5091fb42ef467e1`.
  Selection mutant SHA-256:
  `d0fdfaea162860c6c268d39bf982e4e21782368b69242e7e01355026e71f6868`.
- **Restored result:** the same owning test filtered by
  `-k active_generation_conflicts_on_exact_instruction_or_source_change`,
  exit 0; `2 passed, 12 deselected`.

These mutation checks are intentional RED evidence and are excluded from the
green test totals. They do not establish distributed request coalescing,
external-provider behavior, or live model prompt-injection resistance.

## Task 4 Astra follow-up: specialist receipt replay remains idempotent

- **Source and guard:**
  `backend/src/services/agent/identity_ledger.py:850`,
  `merge_identity_ledger`. A previously processed `observation_id` must not
  merge its records or provenance a second time.
- **Connected owning test:**
  `backend/tests/integration/test_task4_connected_identity_postgres.py::test_search_identity_survives_real_compaction_checkpoint_and_scoped_followup`,
  with the replay equality assertion at line 539. It dispatches the registered
  `create_project_note` mutation through the real writing tool node, verifies
  the completed PostgreSQL operation receipt and single persisted note, then
  repeats the same tool call ID and turn. The second dispatch returns the
  persisted receipt, leaves one database effect, and leaves the identity
  ledger unchanged.
- **Mutation:** changed
  `if observation_id and observation_id not in processed_set:` to
  `if observation_id:`.
- **Command:**

  ```sh
  PYTHONPATH=backend LANGCHAIN_TRACING_V2=false LANGSMITH_TRACING=false ENVIRONMENT=testing REDIS_URL="${TASK_OWNED_REDIS_URL:?}" ORCHESTRATION_TEST_REDIS_URL="${TASK_OWNED_REDIS_URL:?}" ORCHESTRATION_TEST_DATABASE_URL="${TASK_OWNED_DATABASE_URL:?}" .venv/bin/python -m pytest -c backend/pytest.ini -q --tb=short -o log_cli=false backend/tests/integration/test_task4_connected_identity_postgres.py
  ```

- **Observed mutant failure:** exit 1; the equality assertion at
  `test_task4_connected_identity_postgres.py:539` failed because duplicate
  specialist replay changed the persisted identity ledger.
- **Restore proof:** restored from the saved pre-mutation file using its
  absolute worktree destination and verified the SHA-256. Pre-mutation and
  restored `identity_ledger.py` SHA-256:
  `f4c94ecb9a635cdb32b7924dea273a0de0d4ad5af3d35d5528a5c5d689f5d956`.
  Mutant SHA-256:
  `9a6e4a889968699c17cf33ff9e5234a3ba2b769fd6b4e2ce8f7247c1ecefad17`.
- **Restored result:** same command, exit 0; `1 passed`, with the existing
  Pydantic v2 `schema_extra` rename warning.

The Task 4 follow-up also adds a real `revise_draft` root identity mapping:
the completed service result now carries the saved draft title, and extraction
retains that title plus its project relation through both uncapped extraction
and capped Task 2 receipt replay. That contract is covered by
`test_uncapped_revision_result_keeps_draft_title_and_project_relation` and the
revision case in `test_result_receipts_keep_mutation_root_labels_and_relations_for_replay`.
