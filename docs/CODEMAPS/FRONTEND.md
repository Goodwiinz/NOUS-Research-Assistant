# NOUS — Frontend CODEMAP

> Next.js 15 App Router structure, feature index, store slices, services index.
> Last generated: 2026-04-26; entry points, services, stores and dedup tables corrected 2026-10-17 against `ls frontend/app frontend/src`. The route tree and LOC figures are still the April snapshot; re-verify with `ls` and treat `frontend/AGENTS.md` and `docs/engineering/frontend.md` as the maintained contracts.

## Entry points

| File                    | Purpose                                        |
| ----------------------- | ---------------------------------------------- |
| `app/layout.tsx`        | Root layout — fonts, theme, providers          |
| `app/providers.tsx`     | QueryClient, AuthProvider, ToastProvider       |
| `app/page.tsx`          | Root redirect → dashboard                      |
| `app/not-found.tsx`     | 404 page                                       |
| `terminal/src/index.tsx` | Terminal client launched by `./nous` (separate workspace at `terminal/`, not under `frontend/`) |

## App Router tree (`app/`)

```
app/
  (auth)/
    login/, register/, forgot-password/, reset-password/, verify-email/, cli-auth/
  (dashboard)/
    layout.tsx    Shell: sidebar, nav, auth guard
    dashboard/    Overview / home
    chat/         Conversational agent (1,647 LOC page — split pending)
    documents/    Document library + detail view
    entities/     Knowledge graph entity browser (1,179 LOC — split pending)
    projects/     Research projects list + detail (965 LOC — split pending)
    research/     Research assistant (citations, drafts, notes)
    research-engine/  Multi-step pipeline builder
    analytics/    Usage dashboards
    search/       Semantic search
    settings/     User + workspace settings
    arxiv/        arXiv paper search + ingest
    design-system/ Component showcase
    diagnostics/  Retrieval diagnostics
    quality-metrics-demo/  QA demo
  api/            Next.js API routes (minimal — most traffic goes to FastAPI via rewrite)
  globals.css     Tailwind base + CSS variables
  nous-tokens.css NOUS brand tokens (Erebus, Selene, Sol, Helios, Apollo)
```

## `src/` layout

```
src/
  components/
    ui/             shadcn primitives (button, dialog, input, …)
    layout/         Sidebar, nav bar, header shell pieces
    evaluation/     Evaluation god-components (~15,000 LOC — split pending)
    chat/           Chat-specific widgets
    documents/      Document card, uploader
    knowledge-graph/ Graph visualizer
    research/       Citation cards, draft editor
    …               Feature widgets not yet moved to features/
  services/         API and real-time service layer (about 40 files)
    api-client.ts   Shared REST client
    searchService.ts, documentService.ts, projectService.ts, workspaceService.ts,
    agentChatService.ts, streamingService.ts, threadSearchService.ts, artifactService.ts,
    researchEngineService.ts, integration*Service.ts, harnessService.ts, ...
    analytics/      Analytics-specific clients
  store/            Zustand slices (import chat state only via `@/store/chat-store`)
    chat-store.ts + chat/   Conversation transcript (the one sanctioned Zustand cache of server data)
    agentChatStore.ts, llm-chat-store.ts, projectChatStore.ts, citationStore.ts,
    projectStore.ts, pipelineStore.ts, research-engine-store.ts, sidebar-store.ts,
    artifactPanelStore.ts, notificationStore.ts
  stores/           Remaining legacy stores (authStore, graphStore, monitoringStore, analyticsStore, ...)
  hooks/            Cross-feature React hooks
  lib/              Framework glue (cn, Supabase client, animations)
  types/            Cross-feature TypeScript types
  utils/            Shared utilities (formatUtils.ts, analytics/formatUtils.ts, ...)
  contexts/         React contexts (auth, theme)
  providers/        Provider wrappers
  styles/           Additional CSS
```

## API rewrites (Next.js → FastAPI)

Defined in `next.config.js → rewrites()`:

| Next.js path | Destination                                        |
| ------------ | -------------------------------------------------- |
| `/api/v1/*`  | `${BACKEND_URL \|\| NEXT_PUBLIC_API_URL}/api/v1/*` |
| `/api/v2/*`  | `${BACKEND_URL \|\| NEXT_PUBLIC_API_URL}/api/v2/*` |

Set `BACKEND_URL` in env for server-side rewrites. Vercel builds fail if neither `BACKEND_URL` nor `NEXT_PUBLIC_API_URL` is configured, because otherwise all API rewrites would target `http://localhost:8000`. Do NOT set localhost fallbacks in `env:` block — they get baked into the prod bundle.

## Store slices (src/store/)

State that comes from requests is owned by TanStack Query; the chat Zustand store is the single exception. See `docs/engineering/frontend.md` for the one-cache-owner rule and `frontend/src/store/README.md` for the slice list.

## Known deduplication targets

| Concept | Files | Note |
|---|---|---|
| Store root | `src/store/` and `src/stores/` | `src/stores/` still holds several stores; migrate deliberately |
| Format utils | `src/lib/format-utils.ts`, `src/utils/formatUtils.ts`, `src/utils/analytics/formatUtils.ts` | Three files, no single `utils/format.ts` exists |

## Adding a new feature

1. Create route: `app/(dashboard)/<feature>/page.tsx` — keep it <300 LOC (server component shell).
2. Create feature components: `src/components/<feature>/` or new `src/features/<feature>/` directory.
3. Add store slice if needed: `src/store/<feature>-store.ts` (Zustand).
4. Add service: `src/services/<feature>Service.ts` using the canonical API client (`api.ts`).
5. Add types: `src/types/<feature>.ts` for cross-component shapes.
6. Add tests: `src/components/<feature>/__tests__/` and `src/__tests__/services/`.
