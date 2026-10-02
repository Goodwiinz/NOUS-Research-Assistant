# Native agent approval identity

`confirmation_service.py` defines the shared contract for job and SSE
confirmations. A pending card carries `confirmation.approval_id`, an opaque
SHA-256 digest of the run, thread, actor, saved checkpoint/namespace, interrupt
ID, and complete interrupt value. Compute it before tool-argument redaction.
Never derive authorization from the card's display text or the thread alone.

Both `POST /agent/confirm/{job_id}` and `POST /agent/stream/confirm` require
`approval_id` alongside `confirmed`. The producer commits that ID in
`agent_runs.run_metadata` when parking the run. Claiming an approval atomically
matches the native provider, owner, run, awaiting status, cancellation marker,
and approval ID. Releasing a claim matches the same ID, so delayed cleanup
cannot release a newer approval. No database schema migration is needed.

The resume producer verifies the receipt against the saved interrupt before
dispatch and passes an interrupt-ID-keyed `Command.resume`. A deferred job
first verifies that it still owns the claimed approval; stale jobs cannot
execute or finalize a later phase. A nested gate gets a new receipt. LangGraph
can reuse an interrupt ID for sequential interrupts in one node, so the
checkpoint and action bindings are essential even with keyed resume values.

The graph's existing single-card behavior is retained: the receipt identifies
the first pending interrupt and its entire tool batch. This is not a new
parallel-approval or multi-reviewer protocol. Threadless SSE uses the same
receipt/checkpoint validation with its existing local/Redis coordination.

## Clients and rollout

Web, CLI, and Ink echo the server receipt unchanged. A UI generation ID may
reset buttons after a transport failure; it is separate from authorization.
Retries keep the original server receipt, while nested gates replace it.
Trigger.dev wait-token confirmations retain their separate token contract.

This is an intentional breaking request-contract change. Missing or malformed
IDs receive HTTP 422; mismatched job claims receive 409, and SSE mismatches
emit a conflict error. Deploy the backend and updated clients together.
Older cards and pre-upgrade parked runs without a committed receipt expire;
stop/restart their request. Do not backfill an ID from whichever action is
currently pending, and do not add a boolean-only compatibility fallback.
The OpenAPI compatibility gate requires the reviewed
`api-breaking-approved` label, as described in [API contracts](api-contracts.md).

## Validation

- `tests/unit/agent/test_confirmation_service.py`: deterministic replay,
  binding changes, legacy/foreign checkpoints, and two real sequential
  LangGraph interrupts in the same node.
- `tests/unit/agent/test_agent_run_service.py`: owner and receipt predicates,
  nested approval claims, fenced release, and receipt preservation through
  insert recovery (active-thread conflict and missing-thread FK).
- `tests/integration/test_agent_approval_conformance.py`: both native
  adapters against PostgreSQL run rows and `AsyncPostgresSaver`, competing
  decisions, cold replay, stale cards/tasks, and delayed release. It uses
  generated schemas and drops them on exit; it does not call a model provider.
- Web and Ink regressions assert exact receipt forwarding, nested receipt
  rotation, and retry/legacy-card behavior.

Run the database proof from `backend/` with a disposable local database:

```sh
ORCHESTRATION_TEST_DATABASE_URL=postgresql:///postgres \
  python -m pytest -q tests/integration/test_agent_approval_conformance.py
```

Receipt-service doubles in unrelated persistence/tracing unit tests are
explicitly imported from `tests/utils/agent_approval.py`. Identity conformance
tests use the real service and checkpoint state.

Mutation verification on 2026-10-02 (source restored byte-for-byte after each):

- Removing `confirmation_service.py:110`'s receipt comparison failed
  `python -m pytest -q tests/unit/agent/test_confirmation_service.py -k old_receipt`
  (one failure).
- Removing the approval predicate from `agent_run_service.py:792`'s claim or
  `:819`'s release each failed the following command (three failures apiece):

  ```sh
  ORCHESTRATION_TEST_DATABASE_URL=postgresql:///postgres python -m pytest -q \
    tests/unit/agent/test_agent_run_service.py \
    tests/integration/test_agent_approval_conformance.py \
    -k 'stale_claim_and_release or adapters_reject'
  ```

The receipt and run-service unit suites plus the PostgreSQL conformance suite
passed all 49 tests before mutation and again after restoration.

Removing `streaming.py:3478`'s ownership release before emitting a stale
approval error failed this regression; restoring it passed again:

```sh
python -m pytest -q tests/unit/api/test_agent_streaming_confirm_disconnect.py \
  -k postclaim_snapshot
```
