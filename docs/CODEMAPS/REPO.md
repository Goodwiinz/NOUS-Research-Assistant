# NOUS — Repo CODEMAP

> One-screen overview of the top-level directories.
> Regenerated 2026-10-17 from `ls` of the repository root (the earlier 2026-04-26 version listed a `RAG_system/` root, a planned `ops/` directory and 8 workflows, none of which match the tree). Re-verify with `ls` before relying on it.

## Top-level layout

```
backend/          FastAPI app, LangGraph agent, Celery workers, Alembic, openapi.json
frontend/         Next.js app (App Router in app/ and src/), Vitest, Playwright config
terminal/         Terminal client source (React/Ink-style app: src/, tests/)
nous              Launcher script for the terminal client (defaults to the shared dev backend)
packages/         Workspace packages: chat-runtime, harness-bridge
src/trigger/      Trigger.dev jobs (agent, arxiv, documents, scheduled, webhooks)
evals/            Agent eval harness and recorded baselines
tests/            Root cross-system suites: e2e, load (k6), integration, security, contract, ...
infrastructure/   Terraform, Kubernetes manifests, Helm charts, ArgoCD applications, runners
deployment/       Helm chart (deployment/helm/rag-system) and deployment helpers
supabase/         Supabase config, migrations and templates (auth / auxiliary service)
database/         Raw SQL schema files and database notes
config/           Environment templates and compose variants
monitoring/       Prometheus / Grafana / Alertmanager configs
scripts/          Root utility and CI scripts (scripts/ci, scripts/docs, ...)
docs/             Engineering contracts, architecture, API refs, plans, audits, archive, CODEMAPS
security/, brand/, notebooks/, specs/, projects/, tools/, nginx/, feature-flags/
.github/workflows/  CI/CD workflows (11 files, listed in INFRA.md)
```

Root compose files: `docker-compose.yml`, `docker-compose.ci.yml`, `docker-compose.development.yml`, `docker-compose.prod.yml`. Local development does not use Docker; see `docs/engineering/README.md`.

## Entry points

| Concern | File |
|---|---|
| Backend app | `backend/src/main.py` |
| Agent graph | `backend/src/services/agent/_builders.py` (`graph.py` is the re-export facade) |
| LangGraph config | `backend/langgraph.json` |
| OpenAPI snapshot | `backend/openapi.json` |
| Frontend | `frontend/app/layout.tsx` |
| Terminal client | `./nous` launching `terminal/src/index.tsx` |
| Trigger.dev config | `frontend/trigger.config.ts` (resolves `./src/trigger` relative to `frontend/`; a root `trigger.config.ts` also exists, so prove which one a deploy loads) |
| API rewrites | `frontend/next.config.js` |
| CI entry | `.github/workflows/test-pipeline.yml` |

## Key docs

- Engineering contracts → `docs/engineering/`
- Architecture overview → `docs/architecture/`
- API reference → `backend/openapi.json` (generated), `docs/api/` (hand-written, partly stale)
- Deployment guide → `docs/deployment/`
- Operations runbooks → `docs/operations/`, `docs/runbooks/`
- Database schema → `docs/database/`
- This CODEMAP index → `docs/CODEMAPS/`
