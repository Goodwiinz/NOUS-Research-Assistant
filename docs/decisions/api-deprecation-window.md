# Decision: deprecation window for orphaned C3/C4 routes

Status: accepted
Audit: C3 / C4 (audit plan PR #1123)

## Context

Two audit findings (C3, C4) identified backend routes marked `deprecated=True`
that already have zero live callers (frontend, CLI, scripts, synthetic
traffic) but were kept in place rather than deleted outright:

- **C3** (PR #1144): the orphaned "v2 threads" SSE dialect. Its only client
  was the frontend `services/streamingService.ts`, which is itself deprecated
  and has no live UI callers — the active chat page streams via
  `agentChatService` against `POST /api/v1/agent/stream` (LangGraph agent).
- **C4** (PR #1147): three public POST create-message routes coexist but only
  the flat `POST /api/v2/messages` (`create_message_standalone` in
  `api/threads/workspaces.py`) is called by any client. The two nested
  variants below are dead.

Routes marked `deprecated=True` on 2026-07-12:

| Route                                                                                                 | File                                    | Handler              |
| ----------------------------------------------------------------------------------------------------- | --------------------------------------- | -------------------- |
| `POST /api/v2/threads/{thread_id}/stream`                                                             | `backend/src/api/threads/stream.py`     | `stream_thread_chat` |
| `POST /api/v2/threads/{thread_id}/messages`                                                           | `backend/src/api/threads/threads.py`    | `create_message`     |
| `POST /api/v2/workspaces/{workspace_id}/conversations/{conversation_id}/threads/{thread_id}/messages` | `backend/src/api/threads/workspaces.py` | `create_message`     |

## Decision

Give these three routes a **2-week deprecation window** rather than deleting
them immediately, since they are wire-visible (any external caller we don't
control — e.g. a stale browser tab, a cached mobile build, a forgotten
integration script — could still be mid-flight against them):

- **Marked deprecated:** 2026-07-12
- **Deletion eligible:** 2026-07-26 (one release cycle later)
- **Deletion criteria** (both must hold at deletion time):
  1. Zero non-test callers, **re-verified** at deletion time (grep the
     frontend/CLI/scripts/synthetic-traffic sources again — don't rely on
     this document's snapshot).
  2. At least one full release cycle has elapsed since the `deprecated=True`
     marker landed (i.e. on/after 2026-07-26).
- **Owner:** the next autonomous loop tick that runs on or after
  2026-07-26 picks this up as a follow-up cleanup PR (delete the three
  handlers + their now-dead service code paths, per the audit's original
  "slated for removal in a follow-up cleanup PR" note on each docstring).

No route behavior changes in this PR — this is a paper trail for a
`deprecated=True` marking already in place; it fixes only the _process_ gap
(no recorded deletion criteria/date) that PR #1123's audit plan flagged.

## Outcome (2026-11-10)

Deletion criteria re-verified on `develop` at fecb5fdd8: no frontend, CLI,
script, QA or synthetic-traffic source POSTs to either nested route (the
frontend's `workspaceService.createMessage` uses `POST /api/v2/messages`; QA
scenarios only GET the nested list paths). Both nested POST handlers were
removed (audit C4). `stream.py` had already been deleted (bfeb8b89a). The GET
list routes on the same paths are unchanged, so a stale client now receives
405 instead of creating a message.

## Rollback

Purely additive (one new doc + three docstring one-liners). Revert by
deleting `docs/decisions/api-deprecation-window.md` and reverting the
docstring pointer lines; no functional/route change to revert.
