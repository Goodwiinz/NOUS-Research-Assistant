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
  `_transition_predicate`, line 272. The default transition
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
  `_invoke_graph_with_cancellation_monitor`, line 2304. The
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
