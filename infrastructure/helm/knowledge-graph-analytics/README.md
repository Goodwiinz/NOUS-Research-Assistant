# knowledge-graph-analytics Helm chart

Backend (FastAPI) + frontend (Next.js) + celery + Neo4j for the NOUS Multimodal Intelligence Platform.

AWS is the active GitOps deployment. The API, worker, scheduler and synthetic
traffic share one immutable backend image; the frontend deploys through Vercel.

| File | Status | Cluster / namespace |
| --- | --- | --- |
| `values-aws.yaml` | Active, `nous-dev-aws` Argo application | EKS `nous-dev-cluster` / `multimodal-rag-system` |
| `values-dev.yaml` | Retired DOKS rollback configuration | `do-nyc3-rag-system-cluster` / `rag-dev` |
| `values-staging.yaml` | Retired render fixture | `rag-staging` |
| `values-production.yaml` | Retired render fixture | `rag-production` |

Always render `values.yaml` plus the selected overlay. The AWS source and
namespace are declared in [the Argo application](../../argocd/applications/aws-dev.yaml).

## Externally managed secrets

AWS application SQL uses RDS through `aws-database-credentials`; Redis uses
ElastiCache. `app-secrets` and `supabase-credentials` supply application and
auth settings. Shared `envFrom` references and `secretKeyRef` entries are the
contract; values files contain no plaintext credentials. AWS sets
`SUPABASE_DB_URL` blank so an auxiliary Supabase connection cannot supersede
RDS. Inspect the AWS overlay and [Infisical templates](templates/infisical-secrets.yaml)
for the exact references. Update secrets through their declared provider;
do not decode secret values into logs or patch generated Secrets as a routine
verification step. Secret changes and workload restarts are operational
mutations requiring explicit authorization.

## Database migrations

AWS enables `backend.migrations.enabled` and disables
`backend.initContainers.runMigrations`. The chart rejects enabling both.
One ordinary, digest-pinned Job runs `alembic upgrade heads` with the backend's
shared secret references and environment. Argo applies shared resources in
wave 0, waits for the migration Job in wave 1 to complete, then updates the
API, worker, beat and synthetic CronJob in wave 2. A failure or the ten-minute
deadline blocks new consumers; old pods may continue serving during migration.
Migrations must remain compatible with those old pods during rollout.

The Job name hashes its image and pod configuration, so a new image/configuration
creates a new Job rather than modifying an immutable completed pod template.
Completed Jobs have no TTL: deleting them automatically would make Argo
self-heal rerun migrations. The next successful rollout prunes the previous
Job. The Job has no automatic retries; diagnose a failed migration before an
explicitly authorized retry. Provider-side secret content changes alone do not
change Job identity; a new image/configuration or an authorized retry is needed.

Use a full application Argo sync. Resource-selective sync and standalone Helm
upgrade do not enforce this wave barrier and must not be used to promote AWS
backend consumers. Render validation cannot prove live migration success.
The legacy dev/staging overlays retain their API init path; the retired
production overlay still opts out of automatic migration. Their rendering
remains covered without declaring them active.

Read-only rollout and environment checks (Helm 3.13.0, Python 3 + PyYAML):

```sh
python3 infrastructure/helm/knowledge-graph-analytics/tests/deployment_consistency_render_test.py
```
