# Account-wide harness connection

**Status:** Design, written 2026-10-14 against `origin/develop` `fdc9c00fc`, with open PR #1987 (`feat/harness-grant-resume`) and the `nous --rc` branch (`feat/nous-rc`, PRs #1983 and #1986) read but not merged. Decisions were made by the owner in a brainstorming session the same day; the "Decisions" table records them. Nothing here is implemented.

**Contracts touched:** [harness bridge](../engineering/harness-bridge.md), [bridge package](../../packages/harness-bridge/README.md), [`nous --rc` design](2026-10-12-nous-rc-design.md) (on `feat/nous-rc`), [live-proof runbook](../testing/harness-live-proof.md).

## Problem

A harness pairing binds to exactly one project or one NOUS workspace. A workspace pairing is MCP-only: it cannot run Local Codex from a chat. `nous --rc` therefore asks for a project, and using Codex across an account of 5 workspaces and 33 projects takes several pairings. The owner's words: "having connection for one projects complicated"; "I wanted to connect to the whole account".

## Decisions

| Question | Decision |
| --- | --- |
| What may one account approval allow? | Read tools, write requests, library read and library write (reversible library changes run without asking; deleting a folder and ingesting papers still ask), and running Local Codex. (Owner chose the widest of three options.) |
| Which workspaces does "the account" cover? | Workspaces in the user's organization that they own or belong to, excluding archived and deleted workspaces and projects. Evaluated as a query on every call, so new and removed projects apply immediately. |
| How do local folders relate to projects? | An account folder belongs to no project: any chat in any project in the account can run Codex in it. One run per folder at a time, as today. |
| Approach | A third binding kind, `account`, on approvals and grants. Each Local Codex run still gets a project-bound grant for the chat's project, under the account approval. (Chosen over "one approval creating a pairing per project" and "workspace pairings that can run Codex".) |
| Codex's NOUS tools inside a run | Pinned to the chat's project. Account-wide reach stays with a Codex the user starts. (Proposed by Claude; owner approved Section 3.) |
| Order of work | After the `nous --rc` live run, #1983, #1986 and #1987 merge. |

## 1. Data and database

All backend paths are under `backend/src/`.

- **`binding_kind`** (`project` / `workspace` / `account`, NOT NULL) on `integration_grant_requests` and `integration_grants` (`models/integration_grant.py`). The migration backfills existing rows from which of `project_id` / `workspace_id` is set. The `ck_*_one_binding` CHECKs become one CHECK per kind: `project` → `project_id` set and `workspace_id` NULL; `workspace` → the reverse; `account` → both NULL. "Both NULL" is never accepted without the kind: today it means "bound to nothing" and is denied in `schemas/integration_context.py` (`_one_binding`), `validate_binding`, `services/agent/tool_actions.py` (`_resolve_target`, `_target_reason`).
- **Grant kind versus approval kind.** An account approval (the browser click) issues an account-kind pairing grant to the device. Each Local Codex run gets a project-kind grant whose `request_id` is the account approval. Consent-lineage checks that compare `request_id` (`services/harness/delivery.py` `_authorize`) therefore hold unchanged.
- **Folders.** `bridge_workspace_bindings.project_id` becomes nullable; NULL is an account folder. Registering one (`bind_workspace`, `POST /integrations/devices/{id}/workspaces`) requires the device to hold a live account approval.
- **Actions.** `integration_tool_actions` records the authorising grant's kind, and a new nullable `target_workspace_id` holds where `create_folder` creates a folder (today `workspace_id` on that table means "the grant's workspace"). Its `some_binding` CHECK becomes kind-aware.
- **API shapes.** `IntegrationContext`, `GrantRequestCreate`, `GrantRequestDTO`, `WorkspaceBindingCreate` and `ConnectionConsent.kind` gain the account variant; `backend/openapi.json` and `frontend/src/types/generated/api.d.ts` are regenerated.
- **Migration.** One revision after the current head. Downgrade deletes account approvals, account grants, account folders and account-kind action rows, following `hb03_workspace_grants`' downgrade.

## 2. Authorization

- **The account rule.** A SQL condition (not an id list: the asyncpg parameter limit is why `authorized_scope_filter` returns a condition) selecting Collections in workspaces of the user's organization that the user owns or is a live member of, with neither workspace nor Collection archived or deleted. It reuses the organization rule of `services/research_engine/project_access.accessible_research_workspace_ids_query` (the rule `/projects`, and so `nous --rc`, already uses) plus the archive filter. It must respect `docs/engineering/harness-bridge.md`'s note on `Workspace.organization_id` comparisons and the existing boundary tests.
- **Permissions on an account approval.** Allowed: `harness:execute`, `tools:read`, `tools:write`, `library:read`, `library:write` (`_SCOPE_REQUIRES` unchanged). Refused: `artifacts:publish`, `context:read`, `handoff:read`, `handoff:write` (they are bound to one project or chat; they stay available through project pairings). An account approval cannot be chat-bound (`thread_id`).
- **Tools** (`services/integrations/read_tools.py`). Account grants get the `project_id` selector that workspace grants get today (`_PROJECT_SELECTOR`, `workspace_bound` generalised to "selector required"); `_target_project` checks the selector against the account rule on every call; `list_library` lists the whole account with workspace names.
- **Actions** (`services/agent/tool_actions.py`). Selectors (`project_id`, `from_project_id`, `to_project_id`) must be in the account; `create_folder` takes `workspace_id`, which must be an account workspace; `create_project_note` takes `project_id`. `_authority_intact`, `_binding_matches`, `_consent_binding` and `_target_reason` re-check before the effect. Auto-run versus approval follows `library:write` exactly as for workspace grants today.
- **Grant validation** (`services/integrations/context.py`). An account-kind grant needs an account approval for the same device. A project-kind grant is valid under an account approval only while its project is in the account. `validate_binding` gains an account branch (user and organization live; device owned; no thread or run binding on the account grant). `_validate_grant`, `mint_integration_grant`, `exchange_request`, `renew_grant` / `_replace_grant` and #1987's `resume_grant` carry the kind.

## 3. Local Codex runs

- **Start** (`api/agent/harness_streaming.create_chat_context`). A folder is accepted when it is tied to the chat's project, or when it is an account folder and the device's account approval covers that project. `mint_integration_grant` then issues the run's project-kind grant for the chat's project under the account approval; `runs.bind_external_submission` and everything after it see a normal project grant.
- **The bridge socket** (`api/harness.py` `_grant_context`). Accepts an account pairing grant that holds `harness:execute`.
- **Leasing** (`delivery.lease_commands`, `lease_runs`). For an account pairing, the `AgentRun.project_id == context.project_id` filter becomes "the run's project is in the account and the run's device is this device"; `_authorize` runs per run.
- **Long runs.** `lease_commands` replaces `HarnessSession.grant_id` with the bridge's live pairing grant so runs outlive the 15-minute run grant. With an account pairing grant, `delivery._authorize`, `runs.authorize_external_submission`, `prompt_context.build_harness_prompt` and `approvals.native_request_context` take the project from the run (after checking it is in the account) instead of from the grant. This is the most delicate invariant and needs explicit tests.
- **Tool pinning.** The start command envelope (`schemas/harness.py` `Envelope`) carries the run's project id; the bridge passes it to the run's `nous-harness mcp` child, which then accepts only that project.
- **Unchanged.** One run per folder (`uq_harness_active_workspace`), locking, reconciliation, approval cards.

## 4. Bridge and `nous --rc`

- **`nous --rc` is account-only.** Probe Codex, check the folder, sign in, pair the account (`harness:execute`, `tools:read`, `tools:write`, `library:read`, `library:write`), register the current folder as an account folder, run. No project picker. Summary: `Connected: your NOUS account ↔ <folder>  (Codex <v>)`, then "In any chat on NOUS, choose Local Codex → <device> → <folder>."
- **From a project pairing.** The first account `nous --rc` replaces it (one pairing per computer), prints where to revoke the old approval, and re-registers the stored folders as account folders.
- **`nous-harness connect --account`** alongside `--project` / `--workspace`. Always includes `harness:execute` and `tools:read`; `--write` and `--library` as today; refuses `--publish`, `--context`, `--chat`, `--handoff`. `workspace add` registers account folders for an account pairing. The `WORKSPACE_MCP_ONLY` refusals stay for workspace pairings only.
- **Standalone Codex.** `mcp install` prints the account registration; tools take `project_id`; `list_library` lists the choices.
- **State.** `LocalState` records `account: true` instead of a project or workspace; folder entries may lack `projectId`; older state files still load (`storedConnection`).

## 5. Website

- **Approval page** (`frontend/app/(dashboard)/integrations/approve/page.tsx`, `src/lib/integrations/scopeLabels.ts`). Account requests show "<device> wants access to your whole NOUS account", a "Covers" row with the account rule and today's workspace and project counts, account-specific permission wording, and an explicit warning for `library:write`.
- **Connected devices** (`components/integrations/ConnectedDevices.tsx`, `services/integrations/connections.py`). Account approvals appear as "Whole account" with Revoke; `_binding_label` no longer drops a consent with no project or workspace label. Account folders are listed.
- **Composer** (`components/chat/HarnessSelector.tsx`, `hooks/chat/useHarnessConnection.ts`). Lists only computers that can serve the chat (an account approval, or a project approval for the chat's project) and only folders the chat can use (account folders plus folders tied to the chat's project). A refusal shows "This computer isn't connected to this project" instead of the generic error (`streaming.py` maps it).

## 6. Failures, rollout and testing

**Failures.**
- A project leaves the account mid-run: the next command is refused and the chat says the project is no longer reachable from this computer.
- A tool call names a project outside the account: refused; no project: `project_id_required`.
- The account approval is revoked: the bridge disconnects, as for a revoked project approval.
- A server without account support: `--account` and `nous --rc` print "this NOUS server doesn't support account connections yet"; project and workspace pairings are unchanged.

**Rollout.** `ACCOUNT_CONNECTIONS_ENABLED` (default `false`) gates creating account approvals, like the existing bridge flags; turn it on in `values-aws.yaml` for dev after merge, off again instantly if needed.

**Testing.**
- Backend on PostgreSQL (the repo's local recipe, no Docker): migration up and down; the account rule (owner, member, another organization excluded, archived and deleted excluded); grant validation across kinds; runs leased from two projects with per-run re-checks, the long-run grant swap, approval cards; tools and actions including `create_folder` with a workspace; Connected devices. The account rule and the approval matching are mutation-checked.
- Website: approval page, Connected devices, composer filter.
- Bridge: `connect --account`, `nous --rc` without a picker, state compatibility, run tool pinning.
- Live on dev: `nous --rc` pairs the account; chats in two projects run Codex in one folder in turn; a self-started Codex lists folders across workspaces; a reversible library change runs without asking and a folder deletion asks.

**Delivery.** After #1983, #1986 and #1987 merge, three PRs: (1) backend data, rules, tools, actions, approvals and connections; (2) Local Codex runs plus the bridge (`--account`, `nous --rc`); (3) website pages and composer.

## Out of scope

- Publishing, shared project memories and chat handoffs through an account approval.
- Workspaces outside the user's organization, and archived work.
- Several pairings per computer.

## Risks

- **Blast radius.** One approval allows unattended library changes across every project in the account; the approval page must say so plainly, and revocation must be easy to find.
- **The long-run grant swap** (Section 3) is the most delicate change; regressions there would let a run continue under the wrong authority. Mutation-checked tests are required.
- **Three access rules exist today** (integration grants admit cross-organization members; `/projects` is same-organization; thread access admits public workspaces). This design picks the `/projects` rule for account approvals; project and workspace pairings keep theirs.
- **Conflicts.** #1987 and `feat/nous-rc` edit the same functions (`connect`'s reuse block, `renew_grant`); this work starts only after both merge.
