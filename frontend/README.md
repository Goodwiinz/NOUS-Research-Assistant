# frontend

The NOUS web app: Next.js 16 (App Router) on React 18 with Zustand 5 for
client state, TanStack Query for request-backed data, Vitest for unit tests and
Playwright for browser tests. It calls the same FastAPI backend as the terminal
client.

Rules for changing it live in [`AGENTS.md`](AGENTS.md) and
[`docs/engineering/frontend.md`](../docs/engineering/frontend.md). Read those
first. Older migration notes that used to sit in this directory are archived in
[`docs/archive/frontend/`](../docs/archive/frontend/README.md).

## Toolchain

Node 24 (`engines.node` is `24.x`) and `pnpm@10.18.2` only. The root
`pnpm-lock.yaml` is the single lockfile (this package is a member of the root
`pnpm-workspace.yaml`); do not add a nested lockfile or use `npm`/`yarn`.

```sh
pnpm install --frozen-lockfile        # from the repository root
pnpm --dir frontend dev:offline       # next dev; `dev` wraps `infisical run` and needs Infisical
```

## Routes (`app/`)

| path                  | purpose                                                                                                         |
| --------------------- | --------------------------------------------------------------------------------------------------------------- |
| `app/(auth)/`         | `login`, `register`, `forgot-password`, `reset-password`, `verify-email`, `cli-auth`                              |
| `app/(dashboard)/`    | the signed-in app, see below                                                                                    |
| `app/(public)/`       | unauthenticated pages (`arxiv`)                                                                                 |
| `app/auth/`           | OAuth callbacks (`callback`, `orcid`)                                                                           |
| `app/api/`            | Next route handlers: `backend-capabilities`, `health`, `trigger`, `webhooks/github`                              |
| `app/page.tsx`        | landing page                                                                                                    |

Dashboard sub-routes under `app/(dashboard)/`: `chat` (including `new` and
`[id]`), `dashboard`, `documents` (`[id]`, `upload`), `projects` (`[id]`),
`research` (`[id]`), `research-engine` (`projects/[id]/blueprint`,
`projects/[id]/graph`, `runs/[id]`), `search`, `entities`, `analytics`,
`diagnostics`, `notifications`, `artifacts/shared/[shareId]`,
`integrations` (`actions`, `approve`, `context`, `devices`) and `settings`
(`api-keys`, `organization`).

Demo and fixture routes, not product features:

- `app/(dashboard)/design-system/` is a showcase of the NOUS design-system
  components.
- `app/(dashboard)/quality-metrics-demo/` is a demo page for the
  `QualityMetricsCard` component.
- `app/agent-plan-demo/` renders the `agent-plan-demo` UI component.
- `app/visual-test/` holds fixtures (`agent-citations`, `chat-mobile-controls`,
  `response-renderer`) rendered by the visual Playwright configs.

`app/(dashboard)/chat/page.tsx` is a composition root: no service imports and
no business logic.

## Source layout (`src/`)

`components/`, `hooks/`, `services/`, `lib/`, `types/` (with generated API types
in `types/generated/api.d.ts`, never hand-edited), `contexts/`, `page-components/`,
`theme/`, `utils/` and two client-state directories:

- `src/store/` (singular): chat, agent chat, project, pipeline, research-engine,
  citation, notification, sidebar and artifact-panel stores.
- `src/stores/` (plural): auth, agent activity, analytics, graph, enhanced
  graph and monitoring stores.

Which store owns what, and the import rules for the chat store, are in
[`src/store/README.md`](src/store/README.md).

## Scripts (`package.json`)

| group       | scripts                                                                                                                              |
| ----------- | ------------------------------------------------------------------------------------------------------------------------------------ |
| dev / build | `dev` (Infisical), `dev:offline`, `build`, `build:cloud`, `start`, `analyze:bundle`                                                   |
| unit tests  | `test`, `test:watch`, `test:ui`, `test:coverage`, `test:mutants`                                                                      |
| Playwright  | `test:e2e`, `test:e2e:smoke`, `test:e2e:regression`, `test:e2e:accessibility`, `test:e2e:visual`, `test:e2e:mobile`, `test:e2e:agent-qa`, ... (see `package.json`) |
| quality     | `lint`, `lint:fix`, `lint:changed`, `quality:exclusions`, `format`, `format:check`, `type-check`, `validate`                           |
| API types   | `generate:api-types`, `check:api-types`                                                                                               |
| misc        | `capture`, `cli`                                                                                                                      |

`lint:changed` needs an ESLint JSON report, so run it through
`scripts/ci/run_local_ci.sh --frontend`. Run a single unit test with
`pnpm --dir frontend exec vitest run path/to/file.test.ts`.

Browser tests exist in two places: the Playwright configs in this directory
(`playwright*.config.ts`, specs under `e2e/` and `tests/`) and the separate
workspace package [`tests/e2e`](../tests/e2e/README.md), run with
`pnpm --dir tests/e2e exec playwright test --project=chromium` against a
running app.

Ratchets in `quality-baseline.json` and the tsconfig exclusions only move
stricter, in the change that earns the move.
