# Frontend standards

## Chat state ownership

Three owners, deliberately different, on the chat surface:

- **TanStack Query** — not the owner of any chat data. `useChatStreaming`
  only reaches for `useQueryClient()` to invalidate project-data caches
  after a tool call finishes; it holds no chat state of its own.
- **Zustand (`@/store/chat-store`)** — canonical server-shaped chat state:
  workspaces/conversations/threads/messages, pagination, freshness, the
  streaming slice, and the request-identity coordinator.
  `frontend/src/store/chat-store.ts` is the **stable facade**: it creates
  the persisted store, combines the per-domain slices under
  `frontend/src/store/chat/` (`types.ts`, `initialState.ts`,
  `requestCoordinator.ts`, `recordIndex.ts`, `selectors.ts`,
  `slices/{selection,workspace,conversation,thread,message,streaming}Slice.ts`),
  and re-exports every selector/type an existing caller already imports.
  **Always import from `@/store/chat-store`** — never reach into
  `./chat/*` from outside the store itself.
- **Component/hook state** — transient, per-render UI concerns live in the
  focused hooks under `frontend/src/hooks/chat/` (`useChatSession`,
  `useChatStreaming`, `useChatThreadActions`, `useSlashCommands`,
  `useCitationPanel`, `useChatDrawer`, `useChatComposerActions`), composed
  by the route (`app/(dashboard)/chat/page.tsx`, a composition root — no
  service imports, no business logic) into `ChatSurface`/
  `ChatTranscriptState`.

Enforced by
`frontend/src/test/architecture/__tests__/maintenanceContracts.test.ts`:
the chat route imports no `@/services/*` module directly, and
`chat-store.ts` stays under its line-count ceiling and only composes slice
creators (no new state or a `create...Slice` defined inline).

## Server-state ownership

One cache owner per server entity. Server state is request-backed data the
backend owns; each entity has exactly **one** client-side cache, never two.

- **TanStack Query owns request-backed server state** — documents, analytics,
  workspace-data reads. If it comes from an endpoint and isn't the chat
  transcript, Query caches it.
- **The chat Zustand store (`@/store/chat-store`) is the one sanctioned
  exception** — the server-canonical owner of the chat transcript
  (workspaces/conversations/threads/messages). The backend is still the
  writer; the store is the client cache. Going forward it's the *only* store
  sanctioned to own a server entity outside Query. Other stores
  (`projectStore`, `projectChatStore`, `agentChatStore`, `pipelineStore`)
  predate this rule and already hold server-derived state; they are grandfathered,
  not a license to add more. See [Chat state ownership](#chat-state-ownership).
- **Never dual-cache the same entity** in Query and the store. If some future
  need genuinely requires both, it demands an explicit written reconciliation
  contract (who wins, when each invalidates) — not two caches drifting apart.
- **Server Actions must not become a third cache.** If Next Server Actions are
  ever adopted for reads/writes, each carries its own authz check and
  participates in coordinated revalidation — it does not shadow-cache what
  Query or the store already owns.
- **A persisted user-scoped key carries its owner or gets cleared.** Anything
  user-scoped written to `localStorage`/`sessionStorage` must either carry
  `ownerUserId` and be checked against the verified user before it is read
  (`chat-storage` via `store/chat/persistedSelection.ts`,
  `default-workspace-object` in `services/workspaceService.ts`), or be
  removed in `clearUserScopedClientState` (`stores/authStore.ts`). The
  origin-level stamp (`lib/client-owner.ts`) only decides whether a load
  clears everything; it cannot vouch for a payload another tab wrote later.
  Storage access in that path is best-effort: a refused write or removal
  must never abort the clear. Enforced by
  `src/store/__tests__/auth-client-owner.test.ts` and
  `src/store/__tests__/auth-store-storage-failures.test.ts`.

## Canonical writer + terminal reconciliation

- The backend is the sole persisted-chat writer. A hook that already
  delegates persistence to `useChatStreaming`/the store (e.g.
  `useChatComposerActions`) must never call a second create-message
  endpoint of its own.
- Terminal reconciliation (settling a turn's final state) is owned by
  `chat-store`'s `refreshMessages`: a request-identity guard (the
  module-scope `newestPageRequests` map, deliberately kept outside Immer
  state so an `AbortController` is never proxied/persisted) rejects a stale
  response after a fast thread switch, and `expectationMet` keeps
  `messageFreshness` at `'stale'` — preserving the optimistic overlay —
  until the expected persisted/runtime message id actually appears in the
  fetched page. See `docs/testing/chat-mutation-checks.md` for the guard's
  exact file:line and the mutation-verified proof it actually gates
  something.

## Legacy server-state stores — audit verdicts & contracts

Four Zustand stores predate the one-cache-owner rule (see "Server-state
ownership") and hold server entities outside TanStack Query. Audited
2026-07: exactly one live dual-cache existed; the rest are single-owner.
Verdicts:

- **`projectStore`** — *the* dual-cache: `projectDocuments`/`projectNotes`
  are also Query-cached by `useProjectWorkingFolders` under
  `['project', id, 'documents' | 'notes']` (5-min staleTime).
  **Reconciliation contract:** the Query side is read-only (queries only,
  no Query mutations); every writer invalidates the Query keys — store
  mutations (`addDocument`/`removeDocument`/note CRUD) via
  `src/lib/query-client.ts`'s registered client, agent tools via
  `useChatStreaming` (/chat) and `agentChatStore` (global widget). The
  store side refetches through its own actions (page effects +
  `projectDataVersion`). `currentProject`/`projects`/`bibliography` have no
  Query duplicate. New consumers of project documents/notes/drafts should
  use the Query keys, not new store fields.
- **`agentChatStore`** — sanctioned transcript owner, same status as the
  chat store: server-canonical cache of the global agent widget's threads
  and messages (`/api/v1/agent/*`), which no Query cache duplicates. Its
  outbound obligation is the invalidation above whenever a
  project-mutating tool completes.
- **`projectChatStore`** — exemption: `linkedThreads` has no Query
  duplicate. Its only second copy is the chat store's thread→project
  binding mirror, reconciled by the in-file `threadBindingTokens`
  contract (most recent link/unlink per thread wins).
- **`pipelineStore`** — exemption: `PipelineState` has a single consumer
  (`ResearchPipeline`), no Query duplicate, and per-project supersession
  tokens; migrating it to Query would fix no live hazard.

Don't add a second cache for any of these entities; if one becomes
necessary, extend the contract here first.

## Generated vs. hand-authored HTTP types

- `frontend/src/types/generated/api.d.ts` is generated from
  `backend/openapi.json` via `pnpm --dir frontend generate:api-types` —
  never hand-edited. See [api-contracts.md](api-contracts.md).
- **Adopt-on-touch**, not all-at-once: touching a file that owns an HTTP
  request/response shape is the trigger to alias it from
  `components['schemas'][...]` (see
  `frontend/src/types/api/workspace-contract.ts`) instead of re-typing it
  by hand. `frontend/src/types/README.md` tracks current adopters.
- Presentation components under `components/chat` don't import
  `types/generated/api.d.ts` directly when a domain adapter already exists
  (`workspace-contract.ts`, `services/workspaceService.ts`) — go through
  the adapter. Enforced by `maintenanceContracts.test.ts`.

## Ratchets

- **Blocking**: `pnpm --dir frontend lint:changed` (ESLint JSON +
  `scripts/ci/check_frontend_quality.mjs` against
  `frontend/quality-baseline.json` — no new errors in changed files, no
  growth of the global error/warning totals) and
  `pnpm --dir frontend quality:exclusions`
  (`scripts/ci/check_tsconfig_exclusions.py` — no new `tsconfig.json`
  production exclusion without an explicit, reviewed baseline update).
- **Coverage**: `pnpm --dir frontend test:coverage --
  --coverage.reporter=json-summary` then
  `node scripts/ci/check_frontend_coverage.mjs
  frontend/coverage/coverage-summary.json` — fails if any global metric
  (lines/branches/functions/statements) drops below the floor committed in
  `frontend/quality-baseline.json`.
- Full `pnpm --dir frontend lint` and the full-tree type-check stay
  advisory until their debt reaches zero — see [testing.md](testing.md) for
  how to move a floor.

## Toolchain

- Node 24, `pnpm@10.18.2` (root `packageManager` + `.nvmrc`); root
  `pnpm-lock.yaml` is the **only** JS lockfile (no nested
  `frontend/pnpm-lock.yaml`). No `npm run`/`npm install` fallback in any
  active script or Dockerfile — enforced by
  `backend/tests/unit/ci/test_toolchain_contract.py`.
- Local-to-CI parity: `pnpm install --frozen-lockfile`,
  `pnpm --dir frontend lint`, `pnpm --dir frontend type-check`,
  `pnpm --dir frontend test` are the exact commands CI's
  `lint-frontend`/`frontend-tests` jobs run.

## Commands

```sh
pnpm install --frozen-lockfile
pnpm --dir frontend lint
pnpm --dir frontend lint:changed
pnpm --dir frontend quality:exclusions
pnpm --dir frontend type-check
pnpm --dir frontend test
pnpm --dir frontend validate   # lint + type-check + test in one gate
pnpm --dir frontend test:coverage -- --coverage.reporter=json-summary
node scripts/ci/check_frontend_coverage.mjs frontend/coverage/coverage-summary.json
```
