# deployment/monitoring (legacy)

> **Status: legacy, unconsumed.** No Argo CD application or workflow applies
> these manifests (checked 2026-10-17). They are not the live alerting path.

Raw Kubernetes manifests for a self-contained Prometheus, Alertmanager and
Grafana stack in namespace `rag-monitoring`:

| Component | Files |
| --- | --- |
| Prometheus | `prometheus-{configmap,deployment,service,service-account}.yaml` |
| Alertmanager | `alertmanager-{configmap,deployment,service,service-account}.yaml` |
| Grafana | `grafana-{configmap,dashboards,deployment,provisioning,service,service-account}.yaml` |

Related, current material:

- Local monitoring compose stack: [../../monitoring/README.md](../../monitoring/README.md)
- Operations monitoring docs: [../../docs/operations/monitoring/README.md](../../docs/operations/monitoring/README.md)
- Terraform-managed alerting: see [../../infrastructure/terraform/README.md](../../infrastructure/terraform/README.md)

Whether any cluster currently runs these manifests is not verified. Applying
them to a cluster needs explicit authorization for the exact target
(see [../AGENTS.md](../AGENTS.md)).
