# store

Client state management for NOUS, using [Zustand](https://github.com/pmndrs/zustand). All stores are plain `create()` calls; heavier stores layer on the `immer` middleware for ergonomic nested mutations, `persist` for localStorage hydration, and `devtools` / `subscribeWithSelector` where debugging or fine-grained subscriptions are needed. There is no Redux and no global store barrel — each store is imported directly by the components that need it. React context is used sparingly (for example `src/contexts/sidebar-context.tsx`, `hooks/useAuth.tsx` and `components/chat/ChatRagContext.tsx`), not for shared server data.

## Ownership rules

- TanStack Query owns request-backed server data. The chat Zustand store (`chat-store.ts`) is the single sanctioned exception, for the chat transcript. Several stores below (`projectStore`, `pipelineStore`, `research-engine-store`) still fetch from services and keep the results in Zustand; `projectStore` explicitly invalidates the matching Query caches after mutations. Do not add a second cache for an entity that already has an owner.
- Import chat state only from `@/store/chat-store`. Never import from `store/chat/*`; those are internal slices.
- The backend is the sole persisted-chat writer, so hooks must not add a second create-message call. Terminal reconciliation belongs to `chat-store`'s `refreshMessages`.
- See [`frontend/AGENTS.md`](../../AGENTS.md) and [`docs/engineering/frontend.md`](../../../docs/engineering/frontend.md) for the full contract.

## two store directories

State lives in two sibling directories, which is easy to mix up:

- `src/store/` (this directory, singular) holds the chat, project, pipeline, research-engine, notification, citation and sidebar stores listed below, plus `artifactPanelStore.ts`.
- `src/stores/` (plural) holds `authStore.ts`, `agentActivityStore.ts`, `analyticsStore.ts` (and a `analyticsStore.new.ts` rewrite), `enhancedGraphStore.ts`, `graphStore.ts`, `monitoringStore.ts`, and an `analytics/` subfolder (`analyticsStore`, `graphStore`, `realtimeStore` behind an `index.ts`). Its tests are in `src/stores/__tests__/` (only `agentActivityStore.test.ts`); the auth-store tests are in `src/store/__tests__/`.

## chat store layout

`chat-store.ts` is the stable public entry point. It creates the persisted store and combines the internal modules in `chat/`:

| path                          | role                                                                  |
| ----------------------------- | --------------------------------------------------------------------- |
| `chat/slices/`                | per-domain slices: workspace, conversation, thread, message, selection, streaming |
| `chat/requestCoordinator.ts`  | per-thread newest-page request coordination and the (deprecated) active stream AbortController |
| `chat/selectors.ts`           | selectors re-exported by `chat-store.ts`                               |
| `chat/recordIndex.ts`         | reverse indexes for parent lookups                                     |
| `chat/persistedSelection.ts`  | the persisted workspace / conversation / thread selection              |
| `chat/initialState.ts`        | initial state shape                                                    |
| `chat/types.ts`               | `ChatStore` and related types                                          |

`artifactPanelStore.ts` is a small separate store for what the chat artifact panel is showing (document, note, draft, generated artifact, citations, handoff).

## stores

| file                         | domain                                        | key state                                                                                                                                                                                                                                             | key actions / selectors                                                                                                                                                                                                                                                                                                                |
| ---------------------------- | --------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `chat-store.ts`              | workspace · conversation · thread · message   | workspaces, conversations (keyed by workspace), threads (keyed by conversation), messages (keyed by thread); reverse indexes (`conversationToWorkspace`, `threadToConversation`, `messageToThread`) for O(1) parent lookups (GOO-86); streaming state | `loadWorkspaces`, `createConversation`, `loadThreads`, `bulkResolveThreads`, `bulkDeleteThreads`, `streamMessage` (deprecated v2 path), `initializeDefaultWorkspace`, `setThreadProjectBinding`, `registerThread`; selectors: `selectCurrentWorkspace`, `selectCurrentThread`, `selectCurrentThreadProjectId`, `resolveBoundProjectId` |
| `agentChatStore.ts`          | LangGraph agent panel                         | messages, threads, streaming state, HITL `pendingConfirmation`, `currentPlan`, `pageContext`, `projectDataVersion`, `uiMode` (closed / panel / sidebar)                                                                                               | `sendMessage` (SSE → Trigger.dev polling fallback), `confirmAction`, `stopGeneration`, `retryLastMessage`, `loadThreads`, `loadThreadMessages`, `setPageContext`                                                                                                                                                                       |
| `projectStore.ts`            | research projects                             | projects list, `currentProject`, `projectDocuments`, `projectNotes`, bibliography                                                                                                                                                                     | `fetchProjects`, `createProject`, `updateProject`, `addDocument`, `createNote`, `toggleNotePin`, `fetchBibliography`, `downloadBibliography`                                                                                                                                                                                           |
| `projectChatStore.ts`        | project ↔ thread links                        | `linkedThreads` (keyed by projectId), per-project loading / error maps                                                                                                                                                                                | `fetchProjectThreads`, `startChatFromProject`, `linkThreadToProject` (mirrors binding into `chat-store`), `unlinkThreadFromProject`, `saveThreadToNote`; selectors: `selectProjectThreads`, `selectProjectLoading`, `selectProjectError`                                                                                               |
| `citationStore.ts`           | RAG citations                                 | `citations` (capped at 500), `citationsByMessage` index                                                                                                                                                                                               | `fetchCitationsForMessage`, `addCitation`, `clearCitations`                                                                                                                                                                                                                                                                            |
| `notificationStore.ts`       | app-level notifications                       | `notifications` (channel, severity, read)                                                                                                                                                                                                             | `addNotification`, `markRead`, `markAllRead`, `dismiss`, `clearAll`; selectors: `selectUnreadCount`, `selectByChannel`, `selectRecent`                                                                                                                                                                                                 |
| `pipelineStore.ts`           | research pipeline wizard                      | `pipeline` (current step, completed, skipped, invalidated steps, step data)                                                                                                                                                                           | `fetchPipeline`, `advanceStep`, `skipStep`, `goToStep`, `updateStepData`, `resetPipeline`                                                                                                                                                                                                                                              |
| `research-engine-store.ts`   | research-engine runs                          | `projects`, `activeProject`, `activeRun`, `runEvents` (SSE step events)                                                                                                                                                                               | `setActiveProject`, `setActiveRun`, `addRunEvent`, `clearRunEvents`                                                                                                                                                                                                                                                                    |
| `sidebar-store.ts`           | sidebar layout                                | `state` (expanded / collapsed / mini), `position`, `isMobile`, `isTablet`, `mobileOpen`                                                                                                                                                               | `toggleSidebar`, `expandSidebar`, `setMiniMode`, `setIsMobile`, `setIsTablet`; hook: `useSidebarKeyboardShortcuts` (Cmd+B, Escape)                                                                                                                                                                                                     |
| `llm-chat-store.ts`          | legacy LLM chat UI chrome                     | `shortcutsDialogOpen`, `copiedMessageId`                                                                                                                                                                                                              | `setShortcutsDialogOpen`, `setCopiedMessageId`                                                                                                                                                                                                                                                                                         |

## middleware usage

| middleware               | used in                                                                                                                                                      |
| ------------------------ | ------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| `immer`                  | `chat-store`, `agentChatStore`, `projectChatStore`                                                                                                            |
| `persist` (localStorage) | `chat-store` (persists `currentWorkspaceId`, `currentConversationId`, `currentThreadId`, `sidebarCollapsed`), `sidebar-store` (persists `state`, `position`) |

## tests

Unit tests live in `__tests__/` alongside the store files. Coverage includes:

- `agentChatStore.test.ts` — message sending, streaming, HITL confirmation, abort
- `agentChatStore.project-sync.test.ts` — `projectDataVersion` bump after mutating tool calls
- `auth-store-login/signout/signup.test.ts` — authentication flows
- `chat-store-streaming.test.ts`, `chat-store-bulk-operations.test.ts`, `chat-store-project-binding.test.ts` — streaming lifecycle, bulk thread ops, project binding
- `citationStore.test.ts` — dedup and cap enforcement
- `projectChatStore.test.ts` — link/unlink, optimistic updates
- `sidebar-store.test.ts` — responsive state transitions
