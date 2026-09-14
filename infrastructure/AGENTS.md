# Infrastructure guidance

## Scope and sources of truth

This directory owns the Argo CD application definitions, the
`knowledge-graph-analytics` Helm chart and environment overlays, Kubernetes
infrastructure helpers, and their render assertions. The live application is
[`infrastructure/argocd/applications/dev.yaml`](argocd/applications/dev.yaml):
Application `nous-dev` tracks `develop`, renders
`helm/knowledge-graph-analytics` with `values.yaml` plus `values-dev.yaml`,
and targets namespace `rag-dev` with automated sync. Current live/legacy status
and the removed Qdrant path are recorded in
[`docs/engineering/gotchas.md`](../docs/engineering/gotchas.md).

Use the chart README, [`helm-validate.yml`](../.github/workflows/helm-validate.yml),
[`.github/workflows/README.md`](../.github/workflows/README.md), and the chart
tests as the operational sources of
truth. The staging and production value files remain useful render inputs, but
their retired Argo CD applications do not make those environments live.

## Invalid patterns

- Do not infer a live consumer from a duplicate chart, manifest, values file,
  or README. Do not edit `deployment/` material as though it were the live
  `nous-dev` Argo CD source, and do not revive retired staging/production
  paths.
- Preserve base-plus-environment rendering. The base `values.yaml` is
  intentionally incomplete; lint and template each real overlay combination,
  never the base alone and never a hand-copied environment file.
- Keep secrets indirect: preserve Infisical-to-Kubernetes Secret references,
  `envFrom`, and `secretKeyRef` boundaries. Never add plaintext credentials,
  kubeconfig data, or rendered secret values to values files, output, or logs.
- Preserve immutable backend identity. A configured
  `backend.image.digest` is the pull identity; the full tested
  `backend.image.sourceSha` and tag are trace metadata. Keep the shared
  backend digest consistent for backend, migration init, Celery, beat, and
  synthetic workloads.
- Keep NetworkPolicy selection backend-only (`app.kubernetes.io/component:
  backend`). Neo4j and Celery share other selector labels, so broadening the
  selector can default-deny the wrong workloads. Preserve environment-specific
  policy gates and PDB behavior rather than assuming one cluster-wide default.
- Rendered YAML, `helm template`, and Compose config assertions are
  validation only. Never suggest or run `kubectl apply`, Argo CD sync, Helm
  install/upgrade/rollback, or another cluster mutation as verification
  without explicit authorization.

## Required workflow

Before editing, trace the exact Argo CD/workflow consumer and environment,
then inspect the chart helper/template and the relevant overlay. Review the
rendered selectors, image reference and full-SHA annotation, secret
references, NetworkPolicy ingress/egress gates, and PDB settings together.
Preserve the observed PDB split: dev enables `maxUnavailable: 1`, staging
disables the PDB, and production enables `minAvailable: 1`. Keep staging and
production render coverage even though their Argo CD applications are retired.

The read-only checks below mirror the Helm workflow. They require Bash, Helm
(the workflow pins 3.13.0), and Docker with a working Compose command for the
Compose assertion. They inspect or render configuration; they do not apply it
to a cluster.

```sh
CHART=infrastructure/helm/knowledge-graph-analytics
for env in dev staging production; do
  helm lint "$CHART" -f "$CHART/values.yaml" -f "$CHART/values-$env.yaml"
done

for env in dev staging production; do
  helm template rag "$CHART" \
    -f "$CHART/values.yaml" -f "$CHART/values-$env.yaml" >/dev/null
done

bash "$CHART/tests/networkpolicy_render_test.sh"
bash "$CHART/tests/backend_image_digest_render_test.sh"
bash "$CHART/tests/pdb_render_test.sh"
bash infrastructure/tests/docker_compose_development_config_test.sh
```

## Verification

Run the exact read-only block above for chart or infrastructure changes. A
passing render or assertion proves only the checked manifest/configuration
contract; it does not prove Argo CD reachability, live pod identity, scrape
health, secret materialization, or cluster state. Do not run cluster applies
or other external mutations as part of routine verification. If Helm, Docker,
or the required repository dependencies are unavailable, report the affected
check as `NOT RUN`.
