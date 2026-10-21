# Infisical Setup — NOUS Platform

Centralized secret management for the NOUS deployment.

> **Current target (verified against the repo 2026-10-17):** EKS
> `nous-dev-cluster` (`us-east-1`), application namespace
> `multimodal-rag-system`, operator namespace `infisical-operator`, Infisical
> environment slug `dev` (set in `values-aws.yaml`). Only the AWS dev
> environment is live. The `rag-dev` DOKS namespace is frozen rollback
> material, and the `staging`/`prod` environments and namespaces below are not
> live. See [the AWS overview](../deployment/aws-overview.md).
>
> **The database credential is not from Infisical on AWS.** Application SQL
> uses RDS PostgreSQL through `aws-database-credentials`, which an External
> Secrets Operator `ExternalSecret` builds from the RDS-managed Secrets Manager
> credential (`infrastructure/helm/knowledge-graph-analytics/templates/aws-rds-secret.yaml`).
> The Infisical `/database` folder only feeds the DOKS rollback secret
> `database-credentials`. See the chart's
> [externally managed secrets section](../../infrastructure/helm/knowledge-graph-analytics/README.md#externally-managed-secrets).

## Architecture

```
Infisical Cloud (app.infisical.com)
└── project: nous-platform
    ├── env: dev      ──┐
    ├── env: staging  ──┤  ← machine identity: nous-k8s-operator (Universal Auth)
    └── env: prod     ──┘                       nous-ci-build       (Universal Auth)

K8s cluster (EKS nous-dev-cluster)
├── ns: infisical-operator   ← Helm release (ArgoCD app `infisical-operator`)
└── ns: multimodal-rag-system   (legacy DOKS rollback: rag-dev)
    ├── Secret: infisical-universal-auth     ← bootstrapped manually, never in Git
    ├── InfisicalSecret CRDs (Helm-templated, in Git)
    └── Secret: database-credentials, app-secrets, …  ← materialized by operator
```

## Project layout (folders per environment)

Create these folders inside each environment (`dev`, `staging`, `prod`). Each
folder maps 1:1 to a K8s Secret the backend already consumes via `envFrom`.

| Folder            | Materialized as Secret     | Keys (examples)                                                                                   |
| ----------------- | -------------------------- | ------------------------------------------------------------------------------------------------- |
| `/database`       | `database-credentials`     | `DATABASE_URL`, `POSTGRES_USER`, `POSTGRES_PASSWORD`                                              |
| `/app`            | `app-secrets`              | `SECRET_KEY`, `JWT_SECRET`, `ENCRYPTION_KEY`                                                      |
| `/spaces`         | `spaces-credentials`       | `S3_ACCESS_KEY`, `S3_SECRET_KEY`                                                                  |
| `/supabase`       | `supabase-credentials`     | `SUPABASE_URL`, `SUPABASE_SERVICE_ROLE_KEY`                                                       |
| `/redis`          | `redis-credentials`        | `REDIS_URL`, `REDIS_PASSWORD`                                                                     |
| `/azure-openai`   | `azure-openai-credentials` | `AZURE_OPENAI_CHAT_*` (endpoint, api-key, deployment, ver)                                        |
| `/langsmith`      | `langsmith-credentials`    | `LANGSMITH_API_KEY`, `LANGSMITH_PROJECT`                                                          |
| `/do-kb`          | `do-kb-credentials`        | `DO_API_TOKEN`, `DO_KB_REGION`, `DO_KB_PROJECT_ID`, `DO_KB_EMBEDDING_MODEL_UUID`, `DO_KB_ENABLED` |
| `/frontend-build` | _(not synced to cluster)_  | `NEXT_PUBLIC_*`, `SENTRY_AUTH_TOKEN` — read by GitHub Actions                                     |

The mapping from folder → Secret name is defined in
`infrastructure/helm/knowledge-graph-analytics/values.yaml` under
`infisical.secrets`. Override per env if you want different folders.

## One-time setup

### 1. Create the Infisical project

In https://app.infisical.com:

1. New Project → name: `NOUS Platform`, slug: `nous-platform`
2. Environments → confirm `dev`, `staging`, `prod` exist (slugs match)
3. In each env, create the folders listed above and populate the keys

### 2. Create machine identities

Org Settings → Access Control → Identities → Create Identity:

- **`nous-k8s-operator`** — Auth Method: Universal Auth. Add to project
  `nous-platform` with a role granting **Read** on Secrets in `dev`, `staging`,
  `prod`.
- **`nous-ci-build`** — Auth Method: Universal Auth. Add to project with **Read**
  on the `/frontend-build` path across all envs.

For each identity, copy the Client ID and generate a Client Secret. Store as:

- `nous-k8s-operator` → bootstrapped into K8s (see below)
- `nous-ci-build` → GitHub repo Secrets `INFISICAL_CLIENT_ID`, `INFISICAL_CLIENT_SECRET`

### 3. Install the operator

The ArgoCD Application at
`infrastructure/argocd/applications/infisical-operator.yaml` is picked up by the
root app and installs the operator into the `infisical-operator` namespace.

The root app (`aws-dev`) watches that directory, so the operator is installed
by Argo CD from `develop`. Do not `kubectl apply` it by hand without explicit
authorization for the target cluster.

### 4. Bootstrap the auth Secret per namespace

See `infrastructure/kubernetes/infisical-bootstrap.md`.

### 5. Enable in Helm values

`values-aws.yaml` already sets `infisical.enabled: true` and `envSlug: "dev"`.
The staging and production overlays are render fixtures with no live Argo CD
application, so enabling Infisical there has no deployed effect.

### 6. Cut over the old secrets

This step was written for the DOKS cutover and is kept for reference. On the
AWS target, for the namespace, after the InfisicalSecret CRDs are healthy and the
Operator-owned Secrets are populated, delete any pre-existing kubectl-created
Secrets with the same names — the InfisicalSecret will recreate them under its
own ownership.

```bash
# Verify before doing anything destructive
kubectl -n multimodal-rag-system get infisicalsecret -o wide
kubectl -n multimodal-rag-system describe infisicalsecret infisical-database-credentials | tail -20
```

If the InfisicalSecret status shows synced but the Secret already exists with a
different `ownerReferences`, the operator won't overwrite it (creationPolicy:
Orphan). To hand over ownership:

```bash
kubectl -n multimodal-rag-system delete secret <secret-name>   # needs explicit authorization
# Operator recreates within resyncInterval (default 60s)
```

## CI integration

Build-time `NEXT_PUBLIC_*` vars (which Next.js bakes into the client bundle) are
public-by-design but environment-specific. They live in Infisical
`/frontend-build` and are pulled into GitHub Actions via
`Infisical/secrets-action@v1.0.9`.

`SENTRY_AUTH_TOKEN` (server-side, NOT public) is also fetched from
`/frontend-build` but passed via BuildKit `--mount=type=secret` so it never
appears in image layers.

GitHub repo secrets needed (`Settings → Secrets and variables → Actions`):

- `INFISICAL_CLIENT_ID` — from `nous-ci-build` identity
- `INFISICAL_CLIENT_SECRET` — from `nous-ci-build` identity

After cutover, you can delete `NEXT_PUBLIC_SUPABASE_URL`, `NEXT_PUBLIC_SUPABASE_ANON_KEY`,
`NEXT_PUBLIC_SENTRY_DSN`, `SENTRY_AUTH_TOKEN` from GitHub repo secrets — they live
in Infisical now.

## Local development

For `pnpm dev` against local services, the Infisical CLI works but is
optional — you can keep using `frontend/.env.local` and `backend/.env`. If you
want consistency with cluster behavior:

```bash
brew install infisical/get-cli/infisical
infisical login
infisical init  # binds the working dir to the nous-platform project + dev env
infisical run -- pnpm dev   # in frontend/
infisical run -- uvicorn src.main:app --reload  # in backend/
```

## Troubleshooting

**InfisicalSecret stuck on "creating"**: check the operator logs.

```bash
kubectl -n infisical-operator logs -l control-plane=controller-manager --tail=100
```

**Auth failure**: verify the bootstrap Secret has the right keys
(`clientId`, `clientSecret` — note camelCase, not snake_case).

```bash
kubectl -n multimodal-rag-system get secret infisical-universal-auth -o jsonpath='{.data}' | jq 'keys'
# Should show ["clientId","clientSecret"]
```

**Secret not materializing**: confirm the folder path and env slug match
exactly. Slugs are case-sensitive.

**Operator can't reach app.infisical.com**: NetworkPolicy or egress rules. The
operator needs HTTPS egress to `app.infisical.com:443`.

## Verification status

Not verified from the repository alone: the contents of the Infisical project,
which GitHub secrets currently exist, and whether `Infisical/secrets-action` is
still used by a workflow (the only workflow found referencing
`INFISICAL_CLIENT_ID` is `trigger-deploy.yml`, 2026-10-17). The CI integration
section above describes the original design and may be stale; the frontend
deploys through Vercel.
