# Kubernetes Secrets

Do not commit Kubernetes `Secret` manifests or base64-encoded secret values in
this directory.

Only the AWS dev environment is live (EKS `nous-dev-cluster`, namespace
`multimodal-rag-system`). Its secrets come from two places:

- Application and auth settings (`app-secrets`, `supabase-credentials`,
  `redis-credentials`, `langsmith-credentials`) are synced from Infisical by the
  chart's `InfisicalSecret` resources.
- The database credential `aws-database-credentials` is built by External
  Secrets Operator from the RDS-managed Secrets Manager credential. It does not
  come from Infisical.

See [../infisical-bootstrap.md](../infisical-bootstrap.md),
[../../../docs/infrastructure/infisical-setup.md](../../../docs/infrastructure/infisical-setup.md)
and the chart's
[externally managed secrets section](../../helm/knowledge-graph-analytics/README.md#externally-managed-secrets).
Staging and production have no live environment.

For local or break-glass cluster setup, the helper
[`scripts/setup-secrets.sh`](../../../scripts/setup-secrets.sh) exists in the
repository (checked 2026-10-17), or use
`kubectl create secret ... --dry-run=client -o yaml` and pipe the result
directly to `kubectl apply -f -`. Creating or rotating a secret in a cluster
needs explicit authorization for the exact target; read the script before use,
because it defaults `NAMESPACE` to `knowledge-graph-analytics`, not
`multimodal-rag-system`; set `NAMESPACE` explicitly.
