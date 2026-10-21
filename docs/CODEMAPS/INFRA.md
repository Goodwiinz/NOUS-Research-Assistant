# NOUS — Infra CODEMAP

> Cluster topology, ArgoCD apps, Helm charts, Compose files, CI runners.
> Regenerated 2026-10-17 from the files named below. The earlier 2026-04-26 version described three DOKS environments, Qdrant and registry details that no longer match. Deployment truth lives in `docs/engineering/gotchas.md` and the workflows.

## Environments

Only `dev` is live, on AWS EKS (`nous-dev-cluster`, `us-east-1`). `values-staging.yaml` and `values-production.yaml` exist under `infrastructure/helm/knowledge-graph-analytics/` but are render fixtures only; do not deploy from them.

`infrastructure/argocd/applications/` holds two dev application definitions, both tracking `develop` and the chart `infrastructure/helm/knowledge-graph-analytics`:

| File | Values files | Destination namespace | Status |
|---|---|---|---|
| `aws-dev.yaml` (`nous-dev-aws`) | `values.yaml`, `values-aws.yaml` | `multimodal-rag-system` | **Live.** ECR images, RDS and ElastiCache, ALB ingress |
| `dev.yaml` (`nous-dev`) | `values.yaml`, `values-dev.yaml` | `rag-dev` | Retired DigitalOcean (DOKS) app; `values-dev.yaml` is frozen rollback material |

Source of truth: `docs/engineering/gotchas.md` and `.github/workflows/README.md`. Other applications there: `root.yaml`, `aws-dev-root.yaml`, `infisical-operator.yaml`, `nous-dev-aws-ingress.yaml`. Project definition: `infrastructure/argocd/project.yaml`.

**Rule:** never `kubectl patch` ArgoCD-managed resources directly; selfHeal reverts it. Commit the change instead.

## Release flow

After a green Test Pipeline on `develop`, `.github/workflows/release-dev.yml` builds the SHA and opens a digest-pinned GitOps PR to `values-aws.yaml`; merging it deploys. `docker-build.yml` pushes images to Amazon ECR (`REGISTRY` in that workflow). The frontend deploys separately via Vercel.

## Workflows (`.github/workflows/`, 11 files)

`agent-eval.yml`, `docker-build.yml`, `helm-validate.yml`, `opencode.yml`, `release-dev.yml`, `secret-scan.yml`, `supabase-migrations.yml`, `test-pipeline.yml`, `trigger-deploy.yml`, `workflow-lint.yml`, plus `README.md`.

## Helm and Kubernetes

| Path | Purpose |
|---|---|
| `infrastructure/helm/knowledge-graph-analytics/` | The deployed chart (values, values-dev, values-aws, templates, tests) |
| `infrastructure/helm/` | Values for `argocd`, `cert-manager`, `kube-prometheus-stack`; `monitoring-stack/` |
| `deployment/helm/rag-system/` | Older chart (`values.yaml` only); legacy, carries dead Qdrant references |
| `infrastructure/kubernetes/base`, `configmaps`, `secrets`, `manifests`, `monitoring`, `services`, `jobs` | Kustomize bases and one-off manifests |
| `infrastructure/kubernetes/overlays/aws-dev`, `overlays/production` | Overlays (`aws-dev` has ingress and kustomization) |
| `infrastructure/kubernetes/aws-bootstrap`, `aws-addons.md`, `infisical-bootstrap.md` | AWS cluster bootstrap notes and manifests |

## Terraform (`infrastructure/terraform/`)

AWS resources: ACM, ALB controller and IRSA roles, ECR, EBS CSI, cluster autoscaler, GitHub OIDC, app S3 access, external-secrets/RDS access, backup and monitoring (`*.tf` files plus `modules/`).

## CI runners

`infrastructure/arc/runner-deployment.yaml` (Actions Runner Controller) and `infrastructure/runner/README.md`.

## Docker Compose

Compose files live in `config/docker-compose/`. Root symlinks exist for only four: `docker-compose.yml`, `docker-compose.ci.yml`, `docker-compose.development.yml`, `docker-compose.prod.yml`. Others in the directory: `staging`, `services`, `observability`, `security`, `websocket`, `azure`. Local development does not use Docker (see `CLAUDE.md`); `docker-compose.services.yml` and the `development` file may still reference Qdrant, which is removed.

## Key service ports (local)

| Service | Port | Notes |
|---|---|---|
| PostgreSQL | 5432 | DB `multimodal_rag_dev`; container `rag-postgres-1` |
| Neo4j | 7687 (bolt) / 7474 (HTTP) | |
| Redis / Valkey | 6379 | Valkey is wire-compatible |
| Backend | 8000 | |
| Frontend | 3000 | |

## Monitoring

Configs in `monitoring/` (Prometheus rules, Grafana dashboards, Alertmanager routing); cluster manifests in `infrastructure/kubernetes/monitoring/`; Helm values in `infrastructure/helm/kube-prometheus-stack-values.yaml`; docs in `docs/operations/`.
