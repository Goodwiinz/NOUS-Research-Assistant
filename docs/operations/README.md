# Operations

Operational runbooks, on-call material, monitoring and observability references.
These documents span deployment generations; use the current engineering
operational notes to identify the target before applying a procedure.

[Documentation home](../README.md) · [Current operational notes](../engineering/gotchas.md)

## Start here

* [AWS on-call commands](aws-on-call.md) (live EKS target)
* [AWS deployment overview](../deployment/aws-overview.md)
* [On-call procedures](on-call-procedures.md) (escalation and incident process; commands reconciled to AWS)
* [Operations runbook](OPERATIONS_RUNBOOK.md) (historical, bannered)
* [Worker monitoring](WORKERS_MONITORING.md)
* [Infrastructure and DNS runbooks](../runbooks/README.md)
* [Deployment references](../deployment/README.md)
* [Chart rollout and migrations](../../infrastructure/helm/knowledge-graph-analytics/README.md)

## Monitoring and observability

| Collection | Entry points |
| --- | --- |
| [Monitoring](monitoring/README.md) | [Prometheus setup](monitoring/PROMETHEUS_SETUP_COMPLETE.md), [alerting](monitoring/ALERTING_GUIDE.md), [troubleshooting](monitoring/MONITORING_TROUBLESHOOTING.md) |
| [Observability](observability/README.md) | [Architecture](observability/observability-architecture.md), [runbooks](observability/observability-runbooks.md) |

Monitoring and observability documents carry a status banner. Terraform-defined AWS alerting is described in [the Terraform README](../../infrastructure/terraform/README.md); the repository-root `monitoring/` stack is local Compose only.

A runbook or setup report describes its own prerequisites and recorded target.
Confirm configuration and live state before interpreting it as deployed health.
