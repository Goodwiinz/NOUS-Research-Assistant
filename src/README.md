# src/

The repository-root `src/` tree contains only Trigger.dev code under
`src/trigger/`: scheduled jobs, webhook handlers, document and arXiv tasks,
an agent task, an `example.ts`, and shared helpers in `_lib/` (backend client
and response schemas).

It is loaded by the root `trigger.config.ts` (`dirs: ["./src/trigger"]`) and
deployed by `.github/workflows/trigger-deploy.yml` from the repository root on
pushes to `main`. `frontend/trigger.config.ts` is an identical copy that loads
only `frontend/src/trigger/example.ts`. The backend FastAPI code is in
`backend/src/`, not here. Run `pnpm trigger:dev` / `pnpm trigger:deploy` only
with Trigger.dev credentials. Editing rules and the loader proof are in
[`AGENTS.md`](AGENTS.md).
