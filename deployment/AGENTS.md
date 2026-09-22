# Deployment guidance

## Scope and sources of truth

This directory contains the older Helm/Kubernetes deployment tree and its
GitHub Actions deployment workflows. Treat it as a consumer-specific,
partly-retired surface, not as proof of what is deployed. Check the named
workflow or manifest consumer before editing a similarly named chart, values
file, manifest, or script.

The current status evidence is in
[`docs/engineering/gotchas.md`](../docs/engineering/gotchas.md):

> Only `dev` is live (ArgoCD auto-sync from `develop`, namespace `rag-dev`). `staging` + `production` ArgoCD apps retired 2026-04-29 (PR #442); their values files remain and the gitops workflow still bumps staging tags nothing consumes.
>
> Qdrant is removed — retrieval migrated to DO KB (`backend/src/services/do_kb/`, behind `DO_KB_ENABLED`, currently off; live retrieval is PostgreSQL fulltext). Legacy non-ArgoCD charts/manifests still carry dead Qdrant refs; not deployed.

The current live Argo CD source is instead
[`infrastructure/argocd/applications/dev.yaml`](../infrastructure/argocd/applications/dev.yaml),
and the current CI ownership and retirement notes are in
[`.github/workflows/README.md`](../.github/workflows/README.md). The older
[`deployment/README.md`](README.md) and
[`deployment/github-actions/workflows/deploy.yml`](github-actions/workflows/deploy.yml)
describe legacy material where they conflict with those sources.

## Invalid patterns

- Do not call the staging or production values files, the legacy
  `deployment/` chart, or the old deployment workflow live without proving a
  current consumer. Do not revive retired Argo CD paths or infer reachability
  from a filename, a directory tree, or an old README.
- Do not copy or synchronize same-named deployment and infrastructure
  manifests blindly. Trace the workflow, Argo CD application, or other
  concrete consumer first; keep Qdrant references explicitly historical until
  a current consumer proves otherwise.
- Do not turn a lint, render, kubeval, or Helm template result into permission
  to deploy. `kubectl apply`, Argo CD sync, Helm install/upgrade/rollback,
  namespace or infrastructure changes, and secret creation/rotation require
  explicit authorization for the exact target and environment.
- Never put kubeconfig material, secret values, tokens, webhook URLs, or
  rendered secret payloads in a commit, report, command output, or log.

## Required workflow

Before changing a deployment file, read the whole file and identify its
consumer and target environment. Search the workflow/manifests that reference
it, reconcile that evidence with `docs/engineering/gotchas.md` and
`.github/workflows/README.md`, and record whether the path is live, legacy, or
unresolved. Keep validation read-only and keep any later deploy, apply,
rollback, or secret operation as a separately authorized action.

The legacy chart has a narrow, read-only lint command:

```sh
helm lint deployment/helm/rag-system/
```

This checks only the legacy `deployment/helm/rag-system/` chart package. It
does not validate the live Argo CD path, which is the base-plus-dev overlay of
`infrastructure/helm/knowledge-graph-analytics`.

## Verification

Run the legacy lint above with the repository-compatible Helm binary when the
chart itself is in scope. A successful lint is syntax/chart evidence only; it
does not establish live reachability, cluster health, rollout success, or
authorization to mutate an environment. If the required binary or chart
consumer evidence is unavailable, report the check as `NOT RUN` or unresolved.
