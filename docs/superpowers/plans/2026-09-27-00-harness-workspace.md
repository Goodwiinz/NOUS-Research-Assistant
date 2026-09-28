# NOUS Harness Workspace Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Connect NOUS chat bidirectionally to local Codex with reusable conversation context, explicit NOUS research tasks, observable execution, and persistent interactive artifacts.

**Architecture:** NOUS retains application authorization, canonical chat persistence, context documents and artifact ownership. A paired local bridge controls Codex, while an MCP facade exposes scoped tools and bounded NOUS LangGraph tasks. Context/artifact lifecycles survive a closed chat stream; LangSmith traces observed execution without owning product state.

**Tech Stack:** Existing FastAPI, Pydantic, SQLAlchemy/PostgreSQL, Next.js/React, TanStack Query, Zustand, and assistant-ui; Node 24 bridge, Codex App Server, and MCP stdio.

**Spec:** [NOUS harness bridge and artifact workspace](../../plans/2026-09-27-harness-bridge-and-artifacts.md). Read that design and the relevant subsystem plan together.

## Global Constraints

- "First release: only the paired device owner can launch runs."
- "The web app never talks directly to an unauthenticated localhost command server."
- "Never claim exactly-once shell execution."
- "Keep connection health separate from run outcome."
- "Default artifacts to private authorized access, even in a publicly visible chat workspace."
- "Use TanStack Query for artifact lists/metadata/content; never duplicate that server state in Zustand."
- "A later edit does not silently change a previously shared snapshot."
- Source toolchain: Node 24, pnpm 10.18.2, Ruff 0.15.15, Black 26.5.1, isort 5.13.2. Use the root lockfile and `workspace:` for internal dependencies.
- Preserve the [backend](../../engineering/backend.md), [frontend](../../engineering/frontend.md), [API](../../engineering/api-contracts.md), and [testing](../../engineering/testing.md) contracts. Backend services remain the sole persisted-chat writer.

## Review Focus

- A revoked device with an unacknowledged native turn retains uncertain writer ownership; it cannot resume privileged actions or start a duplicate turn. Covered in Plan 01 recovery tests.
- An approved action whose response is lost returns its existing receipt or `outcome_unknown`; neither an external retry nor a native replay repeats the mutation. Covered in Plan 02 action tests.
- A valid source ID from another project cannot escape the integration's project grant, even if the caller belongs to the same organization. Covered in Plan 02 read-scope tests.
- An upload or async draft that finishes after its chat run closes remains discoverable without appending to the closed run ledger. Covered in Plan 03 lifecycle tests.
- Generated HTML attempting network access or application impersonation is contained under real browser policy, with Playwright CSP bypass disabled. Covered in Plan 03 interactive-preview tests.

---

## Status and scope

Proposed implementation detail, authored 2026-09-27 against `16f824197f85436c84558769e72936e3c1decf2b`. No application implementation is included in these documents. The source design and this index were updated after the user's voice discussion to add conversation discovery, rich context documents, explicit NOUS workflows and tracing. New defaults are implementation proposals, not claims about current behavior.

This is the coordination index for five implementation plans. Each has its own acceptance boundary: local execution, NOUS tools, artifact workspaces, conversation context, and bounded research workflows with tracing. The requested change remains planning only.

## File structure and delivery order

| Order | Plan | Code ownership and working deliverable |
| --- | --- | --- |
| 1 | [Local Codex bridge](2026-09-27-01-local-codex-bridge.md) | Integration grants, provider-aware run lifecycle, local bridge package, Codex adapter, connection/approval UI. A real Codex turn can run from NOUS and recover safely. |
| 2 | [NOUS capabilities](2026-09-27-02-nous-capabilities.md) | Backend capability gateway/action policy and bridge MCP modules. An independent CLI or the managed Codex session retrieves authorized NOUS content; selected writes require durable authorization. |
| 3 | [Artifact workspace](2026-09-27-03-artifact-workspace.md) | Artifact storage/version/lifecycle APIs, local file publication, existing panel/card extensions, editing, isolated preview, authenticated sharing. Outputs remain usable after the producing run ends. |
| 4 | [Conversation context](2026-09-27-04-conversation-context.md) | Search/read explicitly authorized NOUS chats; maintain versioned context documents with source references, freshness, user corrections and Markdown export, including non-project chats. |
| 5 | [NOUS workflows and tracing](2026-09-27-05-nous-workflows-and-tracing.md) | Explicit bounded research tasks on native LangGraph; durable start/status/cancel/results; linked traces across bridge, MCP, context and backend boundaries. |

The default execution order is 1 → 2 → 3 → 4 → 5. Within a plan, follow its numbered prerequisites. Do not parallelize edits to shared schema, route-registration, package manifests, or event-contract files without one designated owner. Plan 01 must compile and run before MCP/artifact modules exist; later plans compose those modules through its explicit seams.

Every task supplies its own file list, named interfaces, failing assertion, red/green commands, and scoped commit. "Create" paths are proposed; "Modify" paths exist at the baseline. Commands targeting a new script or test become runnable only after the task that creates it. A collection/import failure is only the first red signal; finish by proving the behavior with the assertions and required race-guard mutation check.

## Shared interface decisions

### Worked example: continue a research conversation in Codex

Ask in NOUS, with **Codex · My computer** selected: “Find my retrieval-evaluation conversation, use its decisions to compare the local evaluation results, and make an interactive report.” The same request can begin in an independently launched Codex session connected to the NOUS MCP facade.

1. **Find prior context.** Codex calls `search_nous_conversations` within the explicitly selected chats, then `get_conversation_context`. It sees the summary's revision, freshness and coverage, and uses `read_nous_conversation` for supporting messages. Non-project chats work through the separate thread grant. No matching authorized chat produces an honest empty result, without expanding the search to every chat.
2. **Work locally.** For the NOUS-launched session, the paired bridge sends the turn to Codex App Server in the registered folder. Codex works under its enforced permissions; NOUS displays normalized progress and approval requests. A standalone session keeps its own execution controls.
3. **Request additional research only when asked.** If the user also asks NOUS to research an evaluation method, `start_nous_workflow` starts a source-limited LangGraph task under the approved project policy. `get_nous_workflow` returns its durable status and, when complete, its answer and sources. The result goes to a dedicated child chat by default; reading the original chat never appends to it.
4. **Publish the result.** Codex explicitly publishes the report and chart. Saved file cards open as tabs in the artifact panel. Text edits create new versions; an HTML tool runs in the isolated preview. Sharing selects a saved version and requires its own authorization.
5. **Continue later.** NOUS refreshes context for chats that actually changed, preserving source links and owner corrections. An exported Markdown context file remains a snapshot. LangSmith, when enabled, helps inspect observed integration steps; persisted NOUS records determine task and artifact status.

This example requires all five plans. The smaller core milestone below supports execution, project reads and static artifacts; it does not include the entire journey.

### Contracts between subsystem plans

Plan 01 Task 1 owns `backend/src/schemas/integration_context.py` and `backend/src/services/integrations/context.py`. Its frozen `IntegrationContext` carries server-resolved user, organization, project, optional thread/run, and grant IDs. Call `resolve_integration_context(db, token, required_scope=...)`; do not accept those identities as model tool arguments.

HTTP integration calls use the existing NOUS CLI JWT in `Authorization` and an opaque restricted session token in `X-NOUS-Integration-Grant`. Verify both user and organization equality after resolving the grant. This avoids broadening global JWT authentication to a new unreviewed token audience. Standalone MCP sessions can omit thread/run binding and must not invent chat messages.

Scopes use colon-separated names: `harness:execute`, `tools:read`, `tools:write`, `context:read`, `artifacts:publish`, `artifacts:read`, `artifacts:edit`, `artifacts:share`; Plan 04 adds `threads:read`, and Plan 05 adds `workflows:run`. Context correction/pinning uses browser owner authorization, with no external write scope. Each gateway validates its required scope and current resource access on every request. An authorized task output publication is separate from authorization to mutate arbitrary NOUS data or publish publicly.

Keep the existing `IntegrationContext.project_id:UUID` contract strict. Plan 04 introduces a distinct thread-only grant kind and `ConversationContext` resolver for chats without a project; it does not make missing projects valid for document tools, local execution or research workflows. A protected local credential bundle selects the appropriate project/thread grant for each MCP tool family. Models never choose principals or supply grant tokens. Every chat can have native NOUS context; exporting/searching that context externally still requires explicit thread selection.

Plan 02 owns `invoke_read(db, context, invocation) -> ToolResult`, action state/decision APIs, and `createNousMcpServer(client)`. Plan 03 owns `publish_version(db, context, request) -> ArtifactVersionDTO` and the artifact-tool registration extension. Their task Interfaces blocks define the complete argument/result shapes. Never add artifact uploads to generic tool-result JSON or couple artifact lifecycle delivery to a live run.

Plan 04 owns conversation search/history/context APIs and their MCP extension. Its `read_context` result includes an immutable revision, source references and freshness/coverage. Plan 05 consumes authorized context revisions and Plan 02 source tools through a persisted restricted workflow profile; it owns start/status/cancel APIs and cross-service trace correlation. A history read is not an execution request. Normal NOUS chat and local Codex retain their own agent loops; no automatic reciprocal delegation is introduced.

Workflow launch authority is separate from direct-tool authority. Explicit browser workflow consent permits a server-only child grant with exactly `tools:read` and `artifacts:publish` for the fixed profile, selected sources and output destination. Its token never leaves the server, its restrictions are checked at every operation, and parent revocation stops it. A caller holding only `workflows:run` still cannot use the direct retrieval/publication routes. Plan 05 Task 1 owns and tests this delegation contract.

Proposed backend flags default to false: `HARNESS_BRIDGE_ENABLED`, `NOUS_MCP_ENABLED`, `ARTIFACTS_ENABLED`, `ARTIFACT_EDITING_ENABLED`, `ARTIFACT_PREVIEW_ENABLED`, `ARTIFACT_SHARING_ENABLED`. A disabled launch flag blocks new work while preserving reads, cancellation, reconciliation, and audit visibility. The bridge adapter initially supports the inspected Codex `0.153.4`; broaden the range only after native-protocol fixtures pass. The secure artifact snapshot helper adds an explicit Python 3.11+ requirement on macOS/Linux; failure to find it disables local publication with a clear explanation.

Plan 04 adds `CONVERSATION_CONTEXT_ENABLED` for generation and `NOUS_CONVERSATION_MCP_ENABLED` for external recall; Plan 05 adds `NOUS_WORKFLOWS_ENABLED`, all default false. External recall requires both its flag and `NOUS_MCP_ENABLED`; new external workflow starts require both workflow and MCP flags. These gates do not remove ordinary authenticated browser access to already-saved state. External recall disabled means new external reads return unavailable; grant revocation and authorized task status/cancellation/reconciliation remain available.

## Spec coverage and staged release

| Design requirement | Implementation owner |
| --- | --- |
| Both connection directions; local execution; provider separate from model | Plans 01 and 02 |
| Device grants, local folders, effective native permissions, no arbitrary RPC proxy | Plan 01 |
| Accepted submission, reliable delivery, uncertain starts, Stop, approvals, replay | Plan 01 |
| Registry-backed tools, project/organization access, citations, action receipts | Plan 02 |
| Selected context/skills; no wholesale internal memory or prompt export | Plan 02 |
| Search/read existing NOUS chats, including non-project chats; selected external corpus | Plan 04 |
| One maintained context document per chat, source coverage, revisions, corrections and export | Plan 04 |
| Explicit NOUS research workflows through MCP; dedicated output chats; bounded execution | Plan 05 |
| Trace correlation, content redaction, limited native visibility, evaluation fixtures | Plan 05 |
| Immutable outputs, private storage, explicit publication, post-run notifications | Plan 03 |
| File cards, tabs, mobile/focus/pinning, text editing, version conflicts, previews | Plan 03 |
| Live HTML/JS containment and exact-version authenticated sharing | Plan 03 |
| Compatibility contracts, off switches, local/CI/live evidence distinction | All five plans |

The default sequential execution completes each plan before the next. A smaller core milestone can complete Plan 01, Plan 02 Tasks 1–2 plus the read-only acceptance/gate checks from Task 5, and Plan 03 Tasks 1–3. Leave other capabilities unavailable until their owning tasks and acceptance tests pass. That core milestone does not fulfill the expanded request: conversation handoff needs Plan 04, full NOUS research delegation needs Plan 05, and the remaining artifact tasks deliver editing, interactive previews and sharing.

Deferred product expansions remain explicit: a second real harness adapter, importing existing Codex Desktop sessions, wholesale memory export, unrestricted recursive delegation, automatic two-way chat synchronization, public sharing, arbitrary React/npm builds, richer binary editors, and applying edited artifacts back to local files. Explicit bounded NOUS research tasks are included. The adapter interface supports future providers, but conformance fixtures are not evidence that every CLI works. Simple interactive HTML/JS tools are included; unknown executable/binary formats remain source/download only.

## Review and execution handoff

The author completed personal self-review of all 29 tasks across the five plans: coverage is mapped above; task interfaces, permissions, release dependencies and Review Focus tests are specified. New corrections include a separate grant type for non-project chats, permanent invalidation of superseded context revisions, source filtering before retrieval, bounded native execution and trace redaction across descendant spans. These are document checks, not passing runtime tests.

Final planning review resolved the workflow-only grant's internal read/publication delegation and aligned the architecture's initial tool allowlist with Plan 02. The worked example above connects the complete requested journey. Local validation checks all seven documents, 29 task contracts, 72 relative links, balanced code fences and new-file whitespace; repository directory-doc lint separately covers 49 tracked directory documents.

Before implementation, review these plans and select **subagent-driven** or **native** execution. Subagent-driven is recommended because cross-process permissions, retry behavior, and executable previews benefit from an independent review at each task boundary. Use the chosen execution skill and preserve the existing checkout's unrelated work; resolve branch/worktree setup at execution time.

Release evidence must name the exact commit, locally passing checks, hosted CI producer, native session/run IDs, and persisted artifact checksums separately. Tests against a fake process establish protocol behavior, not real Codex execution; a UI screenshot establishes rendering, not stored artifact integrity. Do not deploy or claim live acceptance solely because the plan or local tests are complete.
