# Harness services

Server side of the external coding-agent harness (the local bridge in `packages/harness-bridge` drives it through `backend/src/api/harness.py`, mounted under `/api/v1/harness`). PostgreSQL owns commands, deduplication and lifecycle state; the bridge only executes what the database hands it.

## Files

| File | Purpose |
| --- | --- |
| `runs.py` | Atomic external acceptance of a run and recording of provider observations. |
| `delivery.py` | Durable bridge delivery: commands, dedupe and lifecycle live in PostgreSQL, so delivery is safe across workers and retried by `backend/src/tasks/harness_dispatch.py`. |
| `approvals.py` | Owner-mediated, one-shot decisions for exact live Codex callbacks. |
| `prompt_context.py` | Bounded canonical same-thread input for fresh native Codex sessions. |

## Related code

- Models: `harness_session.py` and `bridge_device.py` under `backend/src/models/`.
- Agent streaming into the harness: `backend/src/api/agent/harness_streaming.py`.
- Mutations requested through a harness go through the fail-closed guard in `backend/src/services/agent/tool_actions.py` (request, decide, execute, with stuck rows swept to `outcome_unknown` and never re-run).

## Rules

- An approval decision is one-shot and bound to the exact callback; do not make it reusable.
- Delivery state changes are database transactions, not in-memory flags, so that a killed worker's work is redelivered rather than lost.
- Keep the router transport-only and let these services own the transaction.
