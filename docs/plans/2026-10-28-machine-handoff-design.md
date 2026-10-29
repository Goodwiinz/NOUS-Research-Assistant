# Handing a coding task from the NOUS agent to Codex on a paired machine — Design

Date: 2026-10-28. Status: design approved by the owner; not implemented.
Read against `origin/develop` 749c4b4cd. Paths are under `backend/src/` unless noted.

## Goal

Today each chat run uses one agent: the NOUS LangGraph agent
(`execution_provider: "nous"`) or Codex on a paired machine (`"codex"`). A Codex
run never goes through LangGraph. This design lets a NOUS turn hand a coding
task to Codex on the user's paired machine, wait for it, and continue the same
turn with the result.

Why: heavy work (large files, builds, tests, multi-step coding) should run on
the user's own machine and on the user's own Codex subscription, not on the
backend. The backend graph only coordinates: it decides, starts, waits and
reads the result.

## Decisions

| Topic | Decision |
| --- | --- |
| What is handed over | A task brief (goal, files involved, what done looks like), not code. Codex works out the code. |
| Who starts a handoff | Both: the user asks, or the agent suggests. Either way the call goes through approval. |
| Machine and folder | The approval card proposes them; the user can change both. |
| While Codex works | The NOUS turn pauses; a live card shows Codex's progress and command approvals. |
| Offline or never-ending run | The card shows the machine as offline and the turn waits until the user cancels. No timeout. |
| Result given to the graph | Codex's final reply plus outcome facts: status, command text, exit codes, changed file paths. |
| Cloud `execute_code` (E2B) | Kept for quick Python cells. |
| Modes | General and research. |
| Mechanism | A durable pause with a child Codex run (approach A below). |
| Final answer delivery | A background task resumes the graph; the answer appears whole (no token streaming). |
| New message during a handoff | Blocked with a notice ("Codex is still working: cancel it or wait"). |
| Chats with no project | The tool refuses and tells the user to move the chat into a project. Codex runs require `thread.source_project_id` (`api/agent/harness_streaming.py:114`). |

Approaches considered:

- **A. Durable pause with a child run (chosen).** Survives restarts and holds
  nothing open while Codex works, which fits "wait until cancel".
- B. The tool waits inside the graph. Rejected: it holds a worker for as long as
  Codex runs, and a worker restart loses the wait.
- C. Three turns in a row (NOUS turn ends, normal Codex turn, automatic NOUS
  follow-up). Kept as the fallback: it reuses the most code, but the graph state
  does not carry across and the transcript shows three turns.

## Existing constraints this design must respect

- One non-terminal run per thread is a partial unique index,
  `uq_agent_runs_active_thread` (`models/agent_run.py:175`). Thread-scoped lookups use
  `scalar_one_or_none()` (`agent_run_service.get_active_run_for_thread`,
  `agent_submission_service._ensure_thread_idle`, Stop/resume in `api/agent/execute.py`).
- A new user message abandons the submitter's own `awaiting_confirmation` run
  (`agent_submission_service.py:468`). The next turn clears a stale pending
  confirmation (`agent_execution_service._clear_stale_pending_confirmation`). The
  sweeper fails awaiting runs after `AGENT_RUN_STALE_AWAITING_AFTER_SECONDS = 7200`.
- No path creates a Codex run from inside the graph. `bind_external_submission`
  flips an existing queued run (`services/harness/runs.py:130`). The harness
  prompt is built from the run's user message and thread history
  (`services/harness/prompt_context.py`).
- Every Codex run writes a required assistant chat row at its end
  (`services/harness/delivery.py:553-612`).
- NOUS turns stream from the in-process graph (`api/agent/streaming.py`). Codex
  runs stream saved events (`api/agent/harness_streaming.stream_harness_run`).
- Approvals are confirm/deny only. The resume value is `{interrupt_id: {"confirmed": bool}}`.
- The bridge forwards Codex tool items with name and status only
  (`packages/harness-bridge/src/adapters/codex.ts:605-636`). Backend payloads
  are strict and capped (`schemas/harness.py`).
- Code before `interrupt()` in a node runs again on resume.

## Design

### 1. Tool `run_on_machine`

- Arguments: `brief` (required). Also optional `machine` and `folder`, set only
  when the user named them.
- Policy: DESTRUCTIVE, so the existing interrupt node asks for approval.
  Descriptor: general intent and the research subgraph (next to `execute_code`).
- Must be the only tool call in its model step. Other calls in the same step get
  a refusal message, because resuming the step would re-run them.
- Refuses with a clear note when the thread has no project, or when the user
  has no paired machine.
- The tool description separates it from `execute_code`: a quick Python cell
  goes to the cloud; a multi-step coding task, or one on large local files, goes
  to the machine.

### 2. Approval with editable target

- Code, not the model, fills the proposed target. It picks the user's online
  device, then the folder last used for this project, skipping folders held by
  `uq_harness_active_workspace`. A machine or folder the user named in the chat
  wins.
- The card shows the brief, the machine and the folder. The user can change the
  machine and folder only; to change the brief, deny and say so.
- The edits ride in the resume payload next to `confirmed`. The server re-checks
  them: the binding belongs to the user and is not deleted, and the folder's
  project is the thread's project or is covered by the account pairing
  (`harness_streaming.py:58-89`).

### 3. `handoff` node (general graph and research subgraph)

Runs after approval. It is idempotent by `tool_call_id`.

1. Create the child run in one transaction (new `create_handoff_run`):
   - an `AgentRun` with `execution_provider="codex"`, `status=queued`,
     `parent_run_id` = the NOUS run, the parent's `thread_id`, and
     `project_id = thread.source_project_id`;
   - `run_metadata.handoff_brief`;
   - a `HarnessSession`;
   - an outbox row of kind `harness.execute`;
   - a `run.created` event.

   The pairing grant is minted by trusted internal code (`mint_integration_grant`,
   `services/integrations/context.py`). It uses the same project-or-account rule
   as `_run_consent_kind`.
2. Set the parent to the new status `delegated`, and emit `handoff.started`
   (child run id) as the parent stream's last frame.
3. `interrupt({"handoff_run_id": ...})`. On resume, return a tool message with
   the outcome.

`prompt_context` gets a branch: a run with `handoff_brief` sends the brief, not
the thread history.

### 4. Child runs

- New nullable column `agent_runs.parent_run_id`. The unique index predicate
  gains `AND parent_run_id IS NULL`. Every thread-scoped active or latest lookup
  filters `parent_run_id IS NULL`.
- A child does not write its own assistant chat row. Its final text is stored on
  the run (`run_metadata` or a run event) for the parent to read. Children are
  excluded from the thread's resume and reattach logic.
- Codex command approvals work as today (APPROVAL_REQUIRED events on the child).

### 5. Live card

The frontend sees `handoff.started` and opens the child's saved-event stream by
run id. This needs a new run-id-addressed endpoint; today's resume is
thread-scoped. The card shows progress, command approvals, the offline state and
a Cancel button.

### 6. Outcome facts from the bridge

- The bridge adds command text, exit code and changed file paths to completed
  tool items. The exact Codex app-server item fields are confirmed during
  planning.
- Caps: command ≤ 500 characters, at most 50 commands and 50 files in the
  outcome, final reply ≤ 4,000 characters. Everything stays within the 16 KiB
  payload cap.
- Command text passes through the existing redaction (`redact_tool_args`,
  `redact_pii`) before it is saved.

### 7. Resume

- When the child reaches a terminal state, `finalize_submission`'s transaction
  also writes a resume record for the parent. Use a new table, or an outbox keyed
  by `(run_id, kind)`: `uq_agent_outbox_run` allows only one row per run.
- A Celery task claims the record once, adapting the confirmation-claim pattern
  to `delegated`. It resumes the graph with `Command(resume=outcome)`, reusing
  the non-streaming resume path in `agent_execution_service`. It does not rely
  on the Redis job record (TTL 1 h). The approval receipt for the handoff
  interrupt is computed at handoff time and stored in `run_metadata`.
- The sweeper re-queues unclaimed resume records.
- The graph writes NOUS's answer and the run completes. The frontend refetches
  messages when it sees the parent complete.

### 8. `delegated` status rules

- It is active: it is added to the index predicate, `ck_agent_runs_status` and
  `_ACTIVE_RUN_STATUSES`.
- `_ensure_thread_idle` returns 409 with "Codex is still working: cancel it or
  wait"; it never abandons a delegated run.
- `_clear_stale_pending_confirmation` skips it. The sweeper skips it while the
  child is non-terminal.
- Stop or Cancel cancels the child first (`request_run_cancellation` → interrupt
  command). The parent resumes with `cancelled` only after the child's terminal
  state is recorded. Unknown execution is never terminal (`services/harness/runs.py`).

## Error handling

| Case | Behaviour |
| --- | --- |
| Machine offline, or the run never ends | The card shows offline; the turn waits until Cancel. |
| Cancel or Stop | The child is cancelled; once its end is recorded, the graph resumes with `cancelled`. |
| Codex fails (for example a usage limit) | The child ends as failed; the graph resumes with `failed` and the reason. |
| Denied approval | The existing denial path; no child is created. |
| Edited target outside the grant, or a folder in use | The resume is refused and the card shows why. |
| Grant lapse during a long run | Existing lineage renewal in `delivery._authorize`. |

## Testing

- A child insert succeeds while the parent is delegated. A second top-level run
  is still refused.
- Resuming the handoff node does not create a second child.
- Two terminal deliveries produce one resume.
- Target edits outside the grant are refused; folders in use are skipped by the default.
- A new message during a handoff returns 409.
- Stop and Cancel work while the child is queued, running or recovering (unknown state).
- No duplicate assistant row.
- Redaction and caps on the bridge's outcome fields.
- Account pairing and project pairing both work.
- A fake-bridge end-to-end test, then a live run on the owner's machine.

## Scope

- Backend: migration (`parent_run_id`, `delegated`), tool, node, child-run
  creation, resume record and task, status rules, run-id stream endpoint.
- Bridge: richer tool events.
- Frontend: editable approval card, child-run live card, blocked composer.

Review: an independent model review checked this design against the code before
approval; its blockers are folded into sections 3, 4, 7 and 8.
