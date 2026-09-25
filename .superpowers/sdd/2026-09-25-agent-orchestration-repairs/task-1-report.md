# Task 1 implementation report

## Scope and interfaces

Task 1 repairs durable run-state transitions, producer cancellation while a
queued graph is running, and final-answer extraction from checkpointed graph
history.

- `agent_run_service.upsert_run(...) -> Optional[AgentRun]` retains its public
  interface. Its existing-row and integrity-fallback updates now use SQL
  predicates that evaluate stored status, cancellation marker, and tenant
  ownership at write time. Terminal states are absorbing except for same-state
  idempotence; STOPPING cannot become success. Correlation fields are only
  backfilled when null, and a concurrent insert winner owned by another user
  is not modified.
- `_sanitize.py` adds the specified
  `current_turn_final_text(messages: list[BaseMessage]) -> str | None` and
  `normalize_terminal_messages(messages: list[BaseMessage], reason: str) ->
  list[BaseMessage]` interfaces. It also exposes internal helpers for API-safe
  message sanitation and graph-update additions.
- Both queued initial and resume graph calls use a producer-owned monitor that
  polls cancellation in a fresh, tenant/user/job-scoped session, cancels and
  awaits the graph task, and checks the marker again after graph completion.
  Both extract results only from assistant messages after the latest human
  message.
- General and specialist reflection terminals repair dangling tool calls and
  provide an honest current-turn failure when no answer exists. Existing
  sanitizer behavior and HTTP/API shape are preserved.

## RED evidence

The tests were written before the implementation. Against the original
implementation, the transition suite reproduced the audited race: the
terminal-update-versus-completion, terminal-update-versus-cancellation,
STOPPING-versus-completion, concurrent insert ownership/slot, and dangling
thread fallback cases did not preserve the winning durable row. The
`test_terminal_update_loses_to_completed` case was also used as the lifecycle
mutation proof below.

The actual compiled general graph test reproduced A04 with an existing
assistant answer, a new human turn, and three failing permission rounds: it
could surface the stale answer and leave pending tool calls. The runner tests
also reproduced stale previous-turn text being persisted when the current
turn had no content-bearing assistant message. The initial and resumed
cancellation cases showed that an accepted stop did not interrupt a blocked
graph await. These behaviors are covered by
`test_general_error_exhaustion_does_not_return_stale_answer`,
`test_error_exhaustion_cannot_return_previous_turn`,
`test_queued_initial_and_resume_results_use_current_turn_text`,
`test_queued_cancel_interrupts_graph`, and
`test_resumed_cancel_interrupts_graph`.

Review found one additional tenant-guard edge on a partially scoped legacy
row. `test_wrong_owner_cannot_backfill_legacy_organization` was run before
repair with a stored row owned by user A and no organization, then a user B
payload attempting to set an organization, a different thread, and an
idempotency key. RED observed the foreign row returned and its organization
changed. After repair, the same focused test passed and verified no status,
organization, owner, thread, or idempotency data changed; the mismatched
payload returns `None`.

RED command and output:

```sh
PYTHONPATH=backend REDIS_URL=redis://127.0.0.1:32775/15 LANGCHAIN_TRACING_V2=false LANGSMITH_TRACING=false ENVIRONMENT=testing .venv/bin/python -m pytest -c backend/pytest.ini -q --tb=short backend/tests/unit/agent/test_agent_run_service.py::test_wrong_owner_cannot_backfill_legacy_organization
```

Observed: exit 1, `1 failed in 2.88s`; the assertion reported the foreign
`AgentRun(..., org=<ORG_B>)` was returned instead of `None`. The same command
after repair reported `1 passed in 2.76s`.

## GREEN verification

Focused unit/package command:

```sh
PYTHONPATH=backend REDIS_URL=redis://127.0.0.1:32775/15 LANGCHAIN_TRACING_V2=false LANGSMITH_TRACING=false ENVIRONMENT=testing .venv/bin/python -m pytest -c backend/pytest.ini -q --tb=no backend/tests/unit/agent/test_agent_run_service.py backend/tests/unit/agent/test_agent_execution_service_seam.py backend/tests/unit/services/test_agent_cancellation.py backend/tests/unit/services/test_agent_graph_topology.py backend/tests/unit/services/test_agent_sanitize_messages.py backend/tests/unit/agent/test_current_turn_final.py
```

Observed on the final post-format run: `68 passed, 1 warning in 7.13s`. The
warning is the pre-existing Pydantic `schema_extra` deprecation; Redis writer
loop-close noise also appeared without failing tests.

PostgreSQL concurrency integration command:

```sh
ORCHESTRATION_TEST_DATABASE_URL='postgresql+asyncpg://orch_test:orch_local_test@127.0.0.1:32774/orch_test' PYTHONPATH=backend REDIS_URL=redis://127.0.0.1:32775/15 LANGCHAIN_TRACING_V2=false LANGSMITH_TRACING=false ENVIRONMENT=testing .venv/bin/python -m pytest -c backend/pytest.ini -q --tb=short backend/tests/integration/test_agent_run_concurrency.py
```

Observed on the final rerun: `1 passed, 1 warning in 1.99s`. This uses a
dedicated temporary schema in the disposable PostgreSQL service and drops it
after the test.

Changed/new Python files passed Ruff; new Python files passed mypy with
`--ignore-missing-imports --follow-imports=silent`. Black and isort formatted
the changed Python files. The task's required actual guard mutation proofs,
including exact failure and restoration commands, are documented in
`docs/testing/agent-orchestration-mutation-checks.md`.

## Files changed

- `backend/src/services/agent/agent_run_service.py`
- `backend/src/services/agent/agent_execution_service.py`
- `backend/src/services/agent/_sanitize.py`
- `backend/src/services/agent/graph.py`
- `backend/src/services/agent/_builders.py`
- `backend/src/services/agent/subgraphs/_factory.py`
- `backend/tests/unit/agent/test_agent_run_service.py`
- `backend/tests/unit/services/test_agent_cancellation.py`
- `backend/tests/unit/agent/test_current_turn_final.py`
- `backend/tests/integration/test_agent_run_concurrency.py`
- `docs/testing/agent-orchestration-mutation-checks.md`
- this report

## Risks, limitations, and exclusions

Cancellation polling is bounded at 0.5 seconds and uses separate database
sessions so it can observe a committed stop while the graph owns its own
session. A cancellation-marker read failure is propagated rather than treated
as successful completion. Cancellation does not roll back an external effect
that may already be in progress; its uncertainty semantics belong to Task 2
and were not changed here. The focused tests and isolated PostgreSQL guard
test passed; the broader integrated matrix is owned by the parent task.

No HTTP shape or public function signature changed. Mismatched durable-run
payloads are now rejected before they can alter another owner's row.
Historical audit documents and portable probes remain untouched. No Task 1
work remains unimplemented. Implementation, tests, and mutation evidence are
in commit `6e3a9797433cba4fd249e06095b6a9cf5c1a662e`. This report is included
in a documentation-only follow-up commit.
