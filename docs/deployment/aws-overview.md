# AWS deployment overview

How the live dev environment is deployed. Compiled from repository files on
2026-10-17 (`develop` at `76da9f1c4`); anything that needs the live cluster is
marked **not verified**. Operational mutations (sync, restart, rollback,
secret changes) need explicit authorization for the exact target.

[Deployment references](README.md) · [AWS on-call commands](../operations/aws-on-call.md) · [Terraform](../../infrastructure/terraform/README.md)

## What is live

| Piece | Value | Source |
| --- | --- | --- |
| Cluster | EKS `nous-dev-cluster`, `us-east-1` | [aws-dev.yaml](../../infrastructure/argocd/applications/aws-dev.yaml), [Terraform](../../infrastructure/terraform/README.md) |
| Namespace | `multimodal-rag-system` | same |
| Backend API | `dev-api.goodwiinz.tech` (ALB ingress, ACM certificate) | [values-aws.yaml](../../infrastructure/helm/knowledge-graph-analytics/values-aws.yaml) |
| Websocket host | `dev-ws.goodwiinz.tech` | [overlays/aws-dev](../../infrastructure/kubernetes/overlays/aws-dev/ingress.yaml) |
| Frontend | Vercel, `goodwiinz.tech` (no in-cluster manifests) | chart README, [gotchas](../engineering/gotchas.md) |
| Database | RDS PostgreSQL via `aws-database-credentials` (External Secrets Operator) | [chart README](../../infrastructure/helm/knowledge-graph-analytics/README.md#externally-managed-secrets) |
| Cache and broker | ElastiCache Redis, via `redis-credentials` | chart README, Terraform `main.tf` |
| Graph | Neo4j StatefulSet in the chart | chart templates |
| Retrieval | PostgreSQL full-text. Qdrant is removed | [gotchas](../engineering/gotchas.md) |
| Images | ECR `nous/backend` and `nous/frontend` | [ecr.tf](../../infrastructure/terraform/ecr.tf) |

Retired or frozen: DOKS `rag-dev` with `values-dev.yaml` (rollback only), and
`values-staging.yaml` / `values-production.yaml` (render fixtures with no live
Argo CD application; those apps were retired 2026-04-29).

## Argo CD applications

All track `develop` with automated sync (prune and self-heal), project
`nous-platform`, in namespace `argocd`:

| Application | Path | Purpose |
| --- | --- | --- |
| `aws-dev` (root) | `infrastructure/argocd/applications` (excludes `root.yaml` and `dev.yaml`) | App of apps. Picks up the children below. |
| `nous-dev-aws` | `infrastructure/helm/knowledge-graph-analytics` with `values.yaml` + `values-aws.yaml` | The application: backend, Celery worker, Celery beat, synthetic CronJob, Neo4j, secrets, migration Job. |
| `nous-dev-aws-ingress` | `infrastructure/kubernetes/overlays/aws-dev` | Websocket Ingress patched for the AWS Load Balancer Controller. |
| `infisical-operator` | Helm chart `secrets-operator` 0.10.4 into namespace `infisical-operator` | Syncs Infisical folders into Kubernetes Secrets. |

`nous-dev-aws` ignores drift on the worker Deployment's replica count (the
worker HPA owns it) and on `InfisicalSecret` status. The cluster-scoped gp3
`StorageClass` is applied outside Argo CD from
`infrastructure/kubernetes/aws-bootstrap/`.

## Release flow

1. A PR merges to `develop`; a full `Test Pipeline` push run succeeds and
   publishes **Release Gate**.
2. `release-dev.yml` (jobs `prepare`, `build`, `promote`) checks the source is
   still current, verifies branch protection, builds that exact SHA through
   `docker-build.yml` (OIDC into AWS, push to ECR), and opens a digest-pinned
   PR that edits `values-aws.yaml` on a `codex/release-dev-<sha>` branch. It
   runs Helm lint and render first, closes superseded proposals, and enables
   squash auto-merge.
3. When the PR merges to `develop`, Argo CD syncs `nous-dev-aws`.
4. The frontend is deployed separately by Vercel.

Details, required repository settings and the `RELEASE_PR_TOKEN` secret are in
[the workflows README](../../.github/workflows/README.md) and
[the secrets reference](../../.github/SECRETS_SETUP.md).

## Migration Job and wave order

From the [chart contract](../../infrastructure/helm/knowledge-graph-analytics/README.md#database-migrations)
and the rendered templates:

| Wave | Resources |
| --- | --- |
| 0 | Shared resources: Secrets, ExternalSecret and SecretStore, ServiceAccounts, config. |
| 1 | One digest-pinned migration Job running `alembic upgrade heads`. Ten-minute deadline, no automatic retries, no TTL. |
| 2 | Backend Deployment, Celery worker, Celery beat, synthetic CronJob. |

A failed Job blocks wave 2 and is kept for diagnosis; deleting or retrying it
is an authorized operational action. Use a full application sync: selective
resource sync and standalone `helm upgrade` skip the wave barrier. Migrations
must stay compatible with old pods that keep serving during the rollout.

## Rollback

- **Application rollback:** revert the values change in Git (the release PR
  that moved the digest in `values-aws.yaml`); Argo CD then syncs the earlier
  digest. A reverted digest does not undo a database migration.
- **Back to DigitalOcean:** `values-dev.yaml` and the DOKS child
  `infrastructure/argocd/applications/dev.yaml` are the frozen rollback
  material. The ordered steps are in the dated
  [cutover runbook](../runbooks/2026-09-14-aws-cutover.md) (Step 9). The
  [decommission runbook](../runbooks/aws-decommission.md) is marked BLOCKED and
  says DigitalOcean Spaces and Knowledge Base remain EKS dependencies. Whether
  DOKS and its data are still intact today is **not verified**.
- **Frontend:** Vercel rollback, as described in the cutover runbook.

## Not verified here

Live Argo CD sync and health, the image digest currently in the cluster, DNS
authority (values comments mention Namify; see
[cloudflare-dns-migration.md](../runbooks/cloudflare-dns-migration.md)),
whether backup restores work, and which workflows assume the EKS deploy IAM role.
