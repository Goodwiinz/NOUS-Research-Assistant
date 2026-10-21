# deployment/ (legacy)

> **Status: legacy, not the live deployment path.** Nothing in
> `.github/workflows/` consumes this directory (checked 2026-10-17), and no
> Argo CD application references it. Do not treat it as proof of what is
> deployed.

## Where the live path is

The live deployment is AWS EKS `nous-dev-cluster` (`us-east-1`), namespace
`multimodal-rag-system`, rendered by Argo CD from
`infrastructure/helm/knowledge-graph-analytics` with `values.yaml` plus
`values-aws.yaml`, tracking `develop`. Start with:

- [AWS deployment overview](../docs/deployment/aws-overview.md)
- [AWS on-call commands](../docs/operations/aws-on-call.md)
- [Chart contract (secrets, migrations)](../infrastructure/helm/knowledge-graph-analytics/README.md)
- [Argo CD application](../infrastructure/argocd/applications/aws-dev.yaml)
- [CI and release workflows](../.github/workflows/README.md)
- [Agent guidance for this directory](AGENTS.md)

## What is actually here

| Path | What it is |
| --- | --- |
| `AGENTS.md` | Scope rules for this directory. |
| `deploy.sh` | Legacy `kubectl apply` script. It defaults `ENVIRONMENT=staging`, which has no live environment, and applies `kubernetes/...` paths relative to the working directory, which do not exist under `deployment/`. Do not run it against a cluster. |
| `helm/rag-system/` | Legacy chart (`rag-system` 1.0.0). It still renders Redis and Neo4j Deployments and older API/ingestion services. Check it with `helm lint deployment/helm/rag-system/` only; a pass is syntax evidence, not deployability. |
| `monitoring/` | Raw Prometheus, Alertmanager and Grafana manifests. See [monitoring/README.md](monitoring/README.md). |

The earlier README of this directory described `k8s/`, `terraform/{aws,gcp,azure}/`,
`github-actions/`, `helm/monitoring`, `helm/dependencies`, an ELK stack,
blue-green rollout and an SLA. None of those exist here; the claims were removed
rather than carried forward. For Terraform see
[../infrastructure/terraform/README.md](../infrastructure/terraform/README.md).
