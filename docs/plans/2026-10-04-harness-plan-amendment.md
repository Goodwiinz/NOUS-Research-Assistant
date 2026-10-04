# Harness bridge and artifact plans: 2026-10-04 amendment

**Status:** dated amendment. It replaces nothing. The 2026-09-27 plans stay as
written. **Checked against:** `origin/develop` at `e0d0fcf79`, 2026-10-04.
**Source:** retrospective review of the harness-bridge/artifact plan, 2026-10-04.

## Where the original text lives

Commit `f787a61a7` added the umbrella plan and six sub-plans. Only Plan 01,
[`2026-09-27-01-local-codex-bridge.md`](../superpowers/plans/2026-09-27-01-local-codex-bridge.md),
is still on `develop`. To read the others:

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
| Plan 02 T3 slice b (send native `create_project_note` through the action guard) | obsolete | The premise was "`_tool_receipt_exists` fails open". That function is gone from `backend/src` (removed in #1716, `1b3ee4d50`); `git grep _tool_receipt_exists` finds it only in an audit probe under `docs/audits/`. Native mutations now take a durable claim: `tools_impl.py:1731` routes `LOCAL_TRANSACTION` tools to `_execute_local_operation` (L1935), which calls `tool_operations.claim_operation` (L1959). `create_project_note` is `LOCAL_TRANSACTION` (`tools.py:674`). | Drop it. If one guard shared by native and external callers is still wanted, write a new plan against `backend/src/services/agent/tool_operations.py`. |
| Plan 02 T4 slice a (selected memories) | stale | PR #1784 is open and `CONFLICTING`. It carries `ic01_integration_context`. | Re-parent `ic01` onto the current single head, read grants through `GrantKeeper.fetch` (#1786, `packages/harness-bridge/src/grants.ts`), regenerate both contracts, then retarget. |
| Plan 02 T4 slice b (frozen skills) | valid, but the deferral reason is stale | #1784 deferred this slice because the skill flags were "off everywhere and absent from `deployment/`". Yet `infrastructure/helm/knowledge-graph-analytics/values-aws.yaml:173-176` and `values-dev.yaml:167-170` set `PROJECT_SKILL_CATALOG_ENABLED` and `PROJECT_SKILL_RUNTIME_ENABLED` to `"true"` (`values-aws.yaml` since #1691, `values-dev.yaml` since #1245). The running pod was not checked here. | Start it after slice a merges. |
| Plan 02 T5 (acceptance) | valid | No acceptance test files on `develop`. `HARNESS_BRIDGE_ENABLED`, `NOUS_MCP_ENABLED` and `ARTIFACTS_ENABLED` default to `False` (`backend/src/core/config.py:106-110`). They appear nowhere under `deployment/` or `infrastructure/`. | Blocked until live proof exists. |
| Plan 03 T3, the rest (tabs, `ArtifactScope`, `setScope`/`openVersion`/`closeTab`, `activeVersionId`) | valid | `frontend/src/store/artifactPanelStore.ts:11-15` keeps a single artifact whose union has exactly one `generated` kind (`artifactId`, `versionId`, `title`). `git grep` finds no `openVersion`, `closeTab`, `ArtifactTabs` or `activeVersionId` under `frontend/src`. | Do it first. Frontend only, no migration. |
| Plan 03 T4 (editing, bounded previews) | valid, scope check missing | `artifacts:read`, `artifacts:edit` and `artifacts:share` are accepted by `STANDARD_SCOPES` (`backend/src/schemas/integration_context.py:9-20`), but nothing enforces them. The only enforced `artifacts:*` scope is `artifacts:publish` (`backend/src/api/artifacts.py:42`). The other enforced scopes are `harness:execute` (`backend/src/api/harness.py:109`), `tools:read` (`backend/src/api/integrations/tools.py:28`) and `tools:write` (`backend/src/api/integrations/actions.py:39`). `ARTIFACT_EDITING_ENABLED` (`config.py:111`) is never read. | The edit route must require `artifacts:edit` for CLI callers, and must read the flag. |
| Plan 03 T5 (isolated HTML) | valid | `ARTIFACT_PREVIEW_ENABLED` (`config.py:112`) is never read. | Run after T4, since it extends T4's `ArtifactPreview`. |
| Plan 03 T6 (sharing) | valid, carries a migration | `artifacts:share` is not enforced. `ARTIFACT_SHARING_ENABLED` (`config.py:113`) is never read. | Migration-bearing: follow the serial-migration rule in [gotchas](../engineering/gotchas.md). |
| Plan 03 T7 (native completion, rollout) | valid | No `enqueue_native_artifact` under `backend/src`. | Run last in Plan 03. |
| Plan 04, all tasks | stale | It inspected `16f824197`. It puts the backfill at repo-root `scripts/backfill_conversation_context.py`, but every backfill lives in `backend/scripts/` (`backfill_do_kb.py`, ...). It puts native routes at `backend/src/api/threads/conversation_context.py`, but `/api/v2/threads/...` is composed in `backend/src/api/threads/workspace_routes/` (`__init__.py:47`, `threads.py:296`). `test_workspace_boundaries.py` scans only that directory (`ROUTES_DIR`, L74), so a module beside it escapes the guard. Its source revision depends on new triggers, while the only trigger on `chat_messages` today maintains search vectors (`b2c3d4e5f6g7_add_fulltext_search_for_threads.py:104-107`). It carries two migrations (`cc01`, `cc02`). | Rewrite against `develop`. Decide between a trigger and service-owned revision increments, and land the migrations serially. |
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

## Not verified here

- Whether the running dev pod has the skill flags on.
- PostgreSQL `SKIP LOCKED` / compare-and-swap behavior for any slice.
- Live harness proof on the dev cluster.
