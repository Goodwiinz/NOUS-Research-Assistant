# NOUS — Ops CODEMAP

> Compose files, monitoring stack, scripts catalog, CI workflows.
> Regenerated 2026-10-17 from `ls` of `config/docker-compose/`, `monitoring/`, `scripts/` and `.github/workflows/`. The earlier 2026-04-26 version named files that no longer exist (`docker-compose.graph-services.yml`, `deploy-graph-services.sh`, `build-push.yml`, `security-scan.yml`).

## Compose files

Local development does not use Docker (see `CLAUDE.md`), and the root `Makefile` targets that `exec` into containers are not reliable. Compose files live in `config/docker-compose/`; the root symlinks cover only `docker-compose.yml`, `.ci.yml`, `.development.yml` and `.prod.yml`.

| File | Use case |
|---|---|
| `docker-compose.development.yml` | Full local stack |
| `docker-compose.yml` | Minimal backend and databases |
| `docker-compose.services.yml` | Infrastructure only; may still reference Qdrant, which is removed |
| `docker-compose.ci.yml` | CI pipeline |
| `docker-compose.prod.yml`, `docker-compose.staging.yml` | Prod/staging-equivalent local tests (staging and production deploys are dead) |
| `docker-compose.observability.yml`, `docker-compose.security.yml`, `docker-compose.websocket.yml`, `docker-compose.azure.yml` | Specialised variants |

## Monitoring stack (`monitoring/`)

| File / Dir | Purpose |
|---|---|
| `prometheus.yml`, `prometheus/`, `prometheus-rules/` | Scrape config, recording and alerting rules |
| `alert_rules.yml`, `performance_alerts.yml` | Alert definitions |
| `alertmanager.yml`, `alertmanager/` | Routing and templates |
| `grafana/`, `grafana-dashboard.json` | Dashboards |
| `otel-collector-config.yaml`, `otel-collector/` | OpenTelemetry collector |
| `loki-config.yaml`, `promtail-config.yml`, `filebeat/`, `logstash/` | Log aggregation and shipping |
| `docker-compose.monitoring.yml`, `setup.sh` | Stack compose and setup |

Docs: `docs/operations/`. Cluster manifests: `infrastructure/kubernetes/monitoring/`.

## Scripts (`scripts/`, about 78 entries)

| Script | Purpose |
|---|---|
| `ci/run_local_ci.sh` | Run the blocking CI gates locally against a base branch |
| `ci/generate_openapi.py`, `ci/check_alembic.py` | OpenAPI snapshot and drift check; Alembic single-head and revision-id length check |
| `ci/select_ci_jobs.py`, `ci/detect-changes.sh`, `ci/stages.sh`, `ci/shard_tests.py` | Selective CI and test sharding |
| `ci/check_*.py`, `ci/check_*.mjs` | Tool-version, tsconfig-exclusion, feature-map, frontend quality and release-protection gates |
| `docs/check_dir_docs.py` | Directory README lint behind `make docs-lint` |
| `deploy.sh`, `deploy_performance_optimizations.sh` | Older deploy helpers; verify before use |
| `health-check.sh` | Health probe |
| `backup/backup_database.sh`, `backup/disaster_recovery.py` | Backup and recovery helpers |
| `sync-to-obsidian.sh` (root and `scripts/`) | Push docs/memory to an Obsidian vault |

`scripts/` also holds many one-off `test_*.py` (13), `check_*.py` (6) and `debug_*.py` exploration scripts written before the test suite; they are deletion candidates once confirmed unreferenced by `.github/`.

## CI/CD (`.github/workflows/`)

| Workflow | Purpose |
|---|---|
| `test-pipeline.yml` | Lint, type-check, unit and integration tests (Python 3.11) |
| `docker-build.yml` | Build images and push to Amazon ECR |
| `release-dev.yml` | After a green pipeline on `develop`, open the digest-pinned GitOps PR for dev |
| `helm-validate.yml`, `workflow-lint.yml`, `secret-scan.yml` | Chart, workflow and secret validation |
| `supabase-migrations.yml` | Supabase migration checks/apply |
| `trigger-deploy.yml` | Trigger.dev job deploy |
| `agent-eval.yml` | Agent eval harness |
| `opencode.yml` | Agent automation workflow |

Frontend CI uses `pnpm` only; `pnpm-lock.yaml` at the root is the single lockfile.

## Runbooks and docs

- Monitoring and observability → `docs/operations/`, `docs/runbooks/`
- Cluster monitoring manifests → `infrastructure/kubernetes/monitoring/`
- Prometheus/Grafana Helm values → `infrastructure/helm/kube-prometheus-stack-values.yaml`
