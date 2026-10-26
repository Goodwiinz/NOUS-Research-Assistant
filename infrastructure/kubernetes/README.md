# infrastructure/kubernetes

> **Mostly legacy.** Only two paths here are consumed by the live AWS Argo CD
> setup (checked 2026-10-17). Everything else is unconsumed reference material.
> The earlier README of this directory described a `k8s/{base,overlays,charts,scripts}`
> layout with `rag-backend`/`rag-frontend` charts that does not exist; it was
> replaced.

The live application is the Helm chart
[`../helm/knowledge-graph-analytics`](../helm/knowledge-graph-analytics/README.md),
deployed by Argo CD application `nous-dev-aws` into namespace
`multimodal-rag-system` on EKS `nous-dev-cluster`
(see [`../argocd/applications/aws-dev.yaml`](../argocd/applications/aws-dev.yaml)
and the [AWS overview](../../docs/deployment/aws-overview.md)).

## Layout

| Path | Status |
| --- | --- |
| `overlays/aws-dev/` | **Live.** Kustomize overlay consumed by Argo CD app `nous-dev-aws-ingress` (adds the websocket Ingress for the ALB controller). The Helm chart owns the API hostname. |
| `aws-bootstrap/` | **Live, applied outside Argo CD.** Holds the cluster-scoped gp3 `StorageClass`, which the `nous-platform` AppProject does not whitelist. |
| `aws-addons.md` | Record of the out-of-band addon installs on the EKS cluster (ALB controller, autoscaler, and others). |
| `infisical-bootstrap.md`, `secrets/` | Secret bootstrap and policy. See [infisical-bootstrap.md](infisical-bootstrap.md) and [secrets/README.md](secrets/README.md). |
| `cloudflared-argocd.yaml` | Cloudflare tunnel Deployment for the Argo CD dashboard (namespace `argocd`). No Argo CD application references it; how it is applied is not verified. |
| `overlays/production/` | Legacy blue-green manifests, no kustomization, no consumer. |
| `base/`, `manifests/`, `helm/rag-system/`, `services/`, `configmaps/`, `jobs/`, `monitoring/`, `namespace.yaml`, `secrets.yaml` | **Legacy, unconsumed.** No Argo CD application or workflow references them. They carry dead references (for example Qdrant, which is removed) and pre-AWS namespaces. Do not apply them to a cluster and do not copy them into the live chart. |

The legacy chart in `helm/rag-system/` is separate from
`../../deployment/helm/rag-system/`; neither is deployed.

## Rules

- Trace the Argo CD application or workflow consumer before editing any file
  here; see [../AGENTS.md](../AGENTS.md).
- Never commit secret values or kubeconfig material.
- Applying, syncing or deleting anything in a cluster needs explicit
  authorization for the exact target.
