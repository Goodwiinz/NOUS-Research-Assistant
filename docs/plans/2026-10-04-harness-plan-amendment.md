# Harness bridge and artifact plans: 2026-10-04 amendment

**Status:** dated amendment. It replaces nothing. The 2026-09-27 plans stay as
written. **Checked against:** `origin/develop` at `e0d0fcf79`, 2026-10-04.
**Source:** retrospective review of the harness-bridge/artifact plan, 2026-10-04.

## Where the original text lives

Commit `f787a61a7` added the umbrella plan and six sub-plans on branch
`codex/nous-harness-bridge-pr` (PR #1715 head). Only Plan 01
([`2026-09-27-01-local-codex-bridge.md`](../superpowers/plans/2026-09-27-01-local-codex-bridge.md))
reached `develop`, in the #1715 squash; `f787a61a7` is not an ancestor of
`develop`, so the other plans exist only there. If that branch is deleted,
`git fetch origin refs/pull/1715/head` still restores the commit. To read the
others:

```sh
git show f787a61a7:docs/plans/2026-09-27-harness-bridge-and-artifacts.md
git show f787a61a7:docs/superpowers/plans/2026-09-27-00-harness-workspace.md
git show f787a61a7:docs/superpowers/plans/2026-09-27-02-nous-capabilities.md
git show f787a61a7:docs/superpowers/plans/2026-09-27-03-artifact-workspace.md
git show f787a61a7:docs/superpowers/plans/2026-09-27-04-conversation-context.md
git show f787a61a7:docs/superpowers/plans/2026-09-27-05-nous-workflows-and-tracing.md
```

Status labels in this document:

- **valid:** the task's premises still match `develop`, so it can run as written.
- **stale:** the goal still holds, but paths or premises have drifted. Rewrite the task against `develop` first.
- **obsolete:** a premise is false or the work is already covered. Do not implement it.

## What already shipped

| Original task | PR | Note |
| --- | --- | --- |
| Plan 02 T1, T2 | #1737, #1743 | read gateway, stdio MCP |
| Plan 02 T3 slice a, slice c | #1780, #1783 | `tool_actions.py` guard; MCP `request_action` and approval page |
| Plan 03 T1, T1b, T2 | #1751, #1758, #1759 | persistence, lifecycle outbox, publisher |
| Plan 03 T3 (part) | #1762 | generated-file cards and a single-version preview. No tabs |

## Status of each unimplemented slice

| Slice | Status | Evidence on `develop` | What to do |
| --- | --- | --- | --- |
| Plan 02 T3 slice b (send native `create_project_note` through the action guard) | obsolete | The premise was "`_tool_receipt_exists` currently fails open". That function is gone from `backend/src` (removed in #1716, `1b3ee4d50`); `git grep -n _tool_receipt_exists -- backend docs/audits` finds it only in an audit probe under `docs/audits/`. Native mutations now take a durable claim: `backend/src/services/agent/tools_impl.py:1731` routes `LOCAL_TRANSACTION` tools to `_execute_local_operation` (L1935), which calls `tool_operations.claim_operation` (L1959). `create_project_note` is `LOCAL_TRANSACTION` (`backend/src/services/agent/tools.py:674`). | Drop it. If one guard shared by native and external callers is still wanted, write a new plan against `backend/src/services/agent/tool_operations.py`. |
| Plan 02 T4 slice a (selected memories) | stale | PR #1784 is open and `CONFLICTING`. It carries `ic01_integration_context`. | Re-parent `ic01` onto the current single head, read grants through `GrantKeeper.fetch` (#1786, `packages/harness-bridge/src/grants.ts`), regenerate both contracts. |
| Plan 02 T4 slice b (frozen skills) | valid, but the deferral reason is stale | #1784 deferred this slice because the skill flags were "off everywhere and absent from `deployment/`". Yet `infrastructure/helm/knowledge-graph-analytics/values-aws.yaml:173-176` and `values-dev.yaml:167-170` set `PROJECT_SKILL_CATALOG_ENABLED` and `PROJECT_SKILL_RUNTIME_ENABLED` to `"true"` (`values-aws.yaml` since #1691, `values-dev.yaml` since #1245). The running pod was not checked here. | Start it after slice a merges. |
| Plan 02 T5 (acceptance) | valid | None of the Plan 02 T5 files to create exist on `develop`: `backend/tests/integration/test_integration_capabilities_acceptance.py`, `packages/harness-bridge/src/mcp/acceptance.test.ts`, `docs/testing/integration-capabilities-acceptance.md`. `HARNESS_BRIDGE_ENABLED`, `NOUS_MCP_ENABLED` and `ARTIFACTS_ENABLED` default to `False` (`backend/src/core/config.py:106-110`). They appear nowhere under `deployment/` or `infrastructure/`. | Blocked until live proof exists. |
| Plan 03 T3, the rest (tabs, `ArtifactScope`, `setScope`/`openVersion`/`closeTab`, `activeVersionId`) | valid | `frontend/src/store/artifactPanelStore.ts:11-15` keeps a single artifact whose union has exactly one `generated` kind (`artifactId`, `versionId`, `title`). `git grep` finds no `openVersion`, `closeTab`, `ArtifactTabs` or `activeVersionId` under `frontend/src`. | Do it first within Plan 03 (after #1784, per the order below). Frontend only, no migration. |
| Plan 03 T4 (editing, bounded previews) | valid, scope check missing | `artifacts:read`, `artifacts:edit` and `artifacts:share` are accepted by `STANDARD_SCOPES` (`backend/src/schemas/integration_context.py:9-20`), but nothing enforces them. The only enforced `artifacts:*` scope is `artifacts:publish` (`backend/src/api/artifacts.py:42`). The other enforced scopes are `harness:execute` (`backend/src/api/harness.py:109`), `tools:read` (`backend/src/api/integrations/tools.py:28`) and `tools:write` (`backend/src/api/integrations/actions.py:39`). `ARTIFACT_EDITING_ENABLED` (`config.py:111`) is never read. | The edit route must require `artifacts:edit` for CLI callers, and must read the flag. |
| Plan 03 T5 (isolated HTML) | valid | `ARTIFACT_PREVIEW_ENABLED` (`config.py:112`) is never read. | Run after T4, since it extends T4's `ArtifactPreview`. |
| Plan 03 T6 (sharing) | valid, carries a migration | `artifacts:share` is not enforced. `ARTIFACT_SHARING_ENABLED` (`config.py:113`) is never read. | Migration-bearing: follow the serial-migration rule in [gotchas](../engineering/gotchas.md). |
| Plan 03 T7 (native completion, rollout) | valid | No `enqueue_native_artifact` under `backend/src`. | Run last in Plan 03. |
| Plan 04, all tasks | stale | It inspected `16f824197`. It puts the backfill at repo-root `scripts/backfill_conversation_context.py`, but the repo's operational backfills live in `backend/scripts/` (`backfill_do_kb.py`, ...) and none lives in repo-root `scripts/`. It puts native routes at `backend/src/api/threads/conversation_context.py`, but `/api/v2/threads/...` is composed in `backend/src/api/threads/workspace_routes/` (`__init__.py:47`, `threads.py:296`). `test_workspace_boundaries.py` scans only that directory (`ROUTES_DIR`, L74), so a module beside it escapes the guard. Its source revision depends on new triggers, while the only trigger on `chat_messages` today maintains search vectors (`b2c3d4e5f6g7_add_fulltext_search_for_threads.py:104-107`). It carries two migrations (`cc01`, `cc02`). | Rewrite against `develop`. Decide between a trigger and service-owned revision increments, and land the migrations serially. |
| Plan 05, all tasks | stale | Plan 05 says "no pending relay exists". In fact `dispatch_pending` (`backend/src/services/harness/delivery.py:105`) leases `harness.execute` outbox rows with `FOR UPDATE SKIP LOCKED` (L139-150), and `src.tasks.harness_dispatch.dispatch_harness` drives it every 5 s (`backend/src/tasks/celery_app.py:167-170`). Grants expire after 15 minutes (`backend/src/services/integrations/context.py:182`) and #1786 renews them, so a long workflow cannot anchor authority on one grant. It requires Plan 01 grants, the Plan 02 read gateway/MCP and Plan 03 publication; Plan 04 revision reads are optional (`WorkflowStart.context_revision_ids` defaults to `[]`). | T3 must extend the existing relay with a second outbox kind, not add a parallel `relay_nous_workflows` beat. Anchor workflow authority on `consent_id`. Record an eval baseline under [`evals/baselines/`](../../evals/baselines/) before T2 changes the graph profile. |

## Recommended order for later plans

1. Rebase the open migration-bearing PR #1784 (Plan 02 T4 slice a) and land it. Plan 02 T4 slice b can follow.
2. Plan 03 T3 tabs, then Plan 03 T4.
3. Plan 03 T5, then Plan 03 T6.
4. Plan 03 T7, after live proof of Plans 01/02 (T7's own gate).
5. Plan 04, rewritten.
6. Plan 05, last (a sequencing preference, since its Plan 04 dependency is optional), with its eval baseline recorded first.

Branch each migration-bearing slice only after the previous one has merged and
`check_alembic.py` reports a single head on fresh `origin/develop`. On
2026-09-30 alone, five PRs were opened to repair Alembic head forks: #1769,
#1771, #1787 and #1789 merged, and #1792 was closed. #1769 records the cause:
two migrations that each passed CI against their own base forked the chain
once both landed. The rule now lives in [gotchas](../engineering/gotchas.md).

## 2026-10-05: Plan 06, same-project harness continuation

A standalone Codex session can now continue another session's work in the
same NOUS project and chat. The plan is
[`2026-10-05-same-project-harness-continuation.md`](2026-10-05-same-project-harness-continuation.md).
It adds to the order above and replaces nothing in it.

| Slice | Scope | PR / branch | Migration |
| --- | --- | --- | --- |
| 1 | Project artifact list `GET /api/v1/artifacts/projects/{id}`, `list_project_artifacts` read tool, project Files tab | #1879 (`feat/project-artifact-discovery`) | none |
| 2 | Chat binding on consent (`thread_id`), carried to grants and publication | #1882 (`feat/plan06-slice2`) | `hb05_grant_request_thread` |
| 3 | Versioned chat handoffs: `integration_handoffs`, integration and thread routes, MCP tools | #1885 (`feat/plan06-slice3`) | `hb06_integration_handoffs` |
| 4 | Bridge: binding reuse, `status`, handoff CLI and offline queue | #1886 (`feat/plan06-slice4`) | none |
| 5 | PostgreSQL acceptance journey, live-proof runbook Phase 3, this section, `publish_version` flush-order fix | PR TBD (`feat/plan06-slice5`) | none |

Decisions as applied:

- **D1, publication is explicit.** Files reach NOUS only through
  `artifacts_publish` or the CLI. Nothing uploads automatically, and no Codex
  hooks are wired in v1.
- **D2, `--chat` on `connect`.** `nous-harness connect --project <id> --chat <thread>`
  binds the consent, and every grant renewed from it, to one existing chat.
  The approval page shows the chat title. Creating a chat from the CLI is out
  of v1.
- **D3, a handoff is not an LLM summary.** It is structured data the harness
  writes (goal, decisions, remaining, results that name artifact version ids,
  harness session). Plan 04 is unaffected.
- **D4, Slice 2 went ahead without waiting for #1784.** `hb05` was parented on
  the head of the day. Whichever of #1784 and Slice 2 merges second must
  re-parent its migration, following the serial-migration rule above.
- **D5, conflicts answer 409 with the latest version.** A stale or
  ahead-of-chain `expected_parent_version` gets 409 plus the latest handoff.
  The caller merges and retries; rejected content is never stored. A replayed
  `handoff_id` with the same body returns the stored version.

Evidence: the Slice 5 journey
(`backend/tests/integration/test_same_project_continuation.py`) passed on a
local PostgreSQL 14 on 2026-10-05. It is skipped in hosted CI, so hosted is
NOT RUN. Live proof is runbook Phase 3
([`docs/testing/harness-live-proof.md`](../testing/harness-live-proof.md)),
NOT RUN.

## 2026-10-06: Plan 07, NOUS MCP alphaXiv parity

The harness bridge's MCP server gets the job coverage of alphaXiv's MCP (find
and read papers, look up researchers, curate a library) over NOUS data, under
the existing grant and consent model. The plan is
[`2026-10-05-nous-mcp-alphaxiv-parity.md`](2026-10-05-nous-mcp-alphaxiv-parity.md)
and the approved design is
[`2026-10-05-nous-mcp-alphaxiv-parity-design.md`](2026-10-05-nous-mcp-alphaxiv-parity-design.md).
Both are dated records; the live contract is
[`harness-bridge.md`](../engineering/harness-bridge.md). This section adds to
the order above and replaces nothing in it. **Checked against:** `origin/develop`
at `c04521730`, 2026-10-06.

| Slice | Scope | PR / branch | Migration |
| --- | --- | --- | --- |
| 1 | Workspace-scoped grants, `library:read` and `library:write` scopes, `list_library`, `connect --workspace` and `--library`, consent-page labels | PR TBD (`feat/plan07-s1-workspace-grant-v2`, rebuilt from `feat/plan07-s1-workspace-grant`) | `hb03_workspace_grants` |
| 2 | Read tools: arXiv full text, ingested paper content, passage retrieval | #1883, merged | none |
| 3 | Library writes; reversible actions run inline under `library:write` | PR TBD (`feat/plan07-s3-library-actions`), depends on Slice 1 | none |
| 4 | Researcher tools over knowledge-graph PERSON entities | #1887, merged | none |
| 5 | `discover_papers` composite | not opened: eval-gated, and closed as not needed if the primitives pass the eval | none |

Slice 1 carries the plan's only migration, so the serial-migration rule above
applies to it. #1784 (`ic01_integration_context`) merged after the Slice 1
branch was cut, and it also revises `hb06_integration_handoffs`, as
`hb03_workspace_grants` does. Before the Slice 1 PR opens, merge fresh
`origin/develop` and point `hb03_workspace_grants` at the head
`check_alembic.py` prints there (`ic01_integration_context` on 2026-10-06).
Update every place that names its parent: the migration, its test
(`backend/tests/unit/test_workspace_grants_migration.py`), Gate 0a in
[`harness-live-proof.md`](../testing/harness-live-proof.md) and the migration
test paragraph in [`harness-bridge.md`](../engineering/harness-bridge.md).
Two heads fail the blocking `migration-check`.

## Not verified here

- Whether the running dev pod has the skill flags on.
- PostgreSQL `SKIP LOCKED` / compare-and-swap behavior for any slice.
- Live harness proof on the dev cluster.
