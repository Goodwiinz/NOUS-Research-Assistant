# AWS on-call commands

Read-only first-response commands for the live dev environment: EKS
`nous-dev-cluster` (`us-east-1`), namespace `multimodal-rag-system`, Helm
release `nous-dev-aws`. Names come from rendering the chart
(`helm template nous-dev-aws infrastructure/helm/knowledge-graph-analytics -f values.yaml -f values-aws.yaml`)
and from the Argo CD manifests, checked 2026-10-17. Behavior against the live
cluster is **not verified**. Background:
[AWS deployment overview](../deployment/aws-overview.md). The older
[on-call procedures](on-call-procedures.md) holds escalation and
incident-process text.

Rules: the commands below only read. Do not decode Secrets, print environment
values, restart workloads, scale, delete the migration Job, or change Argo CD
without explicit authorization. Argo CD `selfHeal` reverts manual edits to the
backend and beat Deployments.

## Access

```sh
aws eks update-kubeconfig --name nous-dev-cluster --region us-east-1
kubectl config current-context
```

AWS sessions are short-lived; re-authenticate on `ExpiredToken` (see
[aws-addons.md](../../infrastructure/kubernetes/aws-addons.md)).

## Workload names

| Workload | Kind and name | `app.kubernetes.io/component` |
| --- | --- | --- |
| API | Deployment `nous-dev-aws-knowledge-graph-analytics-backend` (Service port 80 to container port 8000) | `backend` |
| Celery worker | Deployment `nous-dev-aws-celery-worker` (replicas owned by the HPA) | `celery-worker` |
| Celery beat | Deployment `nous-dev-aws-celery-beat` | `celery-beat` |
| Neo4j | StatefulSet `nous-dev-aws-knowledge-graph-analytics-neo4j` | `neo4j` |
| Synthetic traffic | CronJob `nous-dev-aws-synthetic-traffic` | `synthetic-traffic` |
| Migration | Job `nous-dev-aws-migrate-<hash>` (the name changes with image and config) | not set |

There is no `document-processor` deployment; document processing runs in the
Celery worker. There is no in-cluster PostgreSQL or Qdrant.

## First look

```sh
NS=multimodal-rag-system
kubectl -n $NS get pods,jobs,cronjob,hpa,pdb,ingress
kubectl -n $NS get events --sort-by=.lastTimestamp | tail -20
kubectl -n $NS rollout status deploy/nous-dev-aws-knowledge-graph-analytics-backend
kubectl -n $NS top pods
```

## Logs

```sh
kubectl -n $NS logs deploy/nous-dev-aws-knowledge-graph-analytics-backend --tail=200
kubectl -n $NS logs deploy/nous-dev-aws-celery-worker --tail=200
kubectl -n $NS logs deploy/nous-dev-aws-celery-beat --tail=100
kubectl -n $NS logs job/<migration-job-name>   # find it with: kubectl -n $NS get jobs
```

## Argo CD

```sh
kubectl -n argocd get applications.argoproj.io aws-dev nous-dev-aws nous-dev-aws-ingress infisical-operator
kubectl -n argocd describe application nous-dev-aws | tail -40
```

A stuck rollout is often a failed or running migration Job (wave 1 blocks wave
2). Diagnose from the Job's logs and events; retrying it needs authorization.

## Health and endpoints

```sh
curl -fsS https://dev-api.goodwiinz.tech/health
curl -fsS https://dev-api.goodwiinz.tech/health/readiness   # database and Redis
```

The ALB health check path is `/health`. The AWS Load Balancer Controller is
installed as a cluster addon (see aws-addons.md); check it with
`kubectl -n kube-system get pods -l app.kubernetes.io/name=aws-load-balancer-controller`
(the namespace is the documented install location; live state not verified).

## Secrets sync (names only)

```sh
kubectl -n $NS get externalsecret,secretstore
kubectl -n $NS get infisicalsecret
kubectl -n infisical-operator get pods
```

`aws-database-credentials` comes from External Secrets; the other application
secrets come from Infisical. Read status conditions with `describe`; do not
output Secret data.

## RDS and ElastiCache

The identifiers below come from `infrastructure/terraform/main.tf` with
`project_name = nous` in the example tfvars; confirm the live names first.

```sh
aws rds describe-db-instances --db-instance-identifier nous-postgres --region us-east-1 \
  --query 'DBInstances[0].[DBInstanceStatus,Endpoint.Address,PubliclyAccessible]'
aws elasticache describe-replication-groups --replication-group-id nous-redis --region us-east-1 \
  --query 'ReplicationGroups[0].[Status,NodeGroups[0].PrimaryEndpoint.Address]'
```

Terraform also defines CloudWatch alarms (database CPU, free storage, Redis
memory, cluster CPU and memory) that notify the SNS topic `nous-alerts`; see
[infrastructure/terraform/README.md](../../infrastructure/terraform/README.md).
RDS and Redis live in private subnets, so connectivity tests must run from a
pod or host inside the VPC. The Terraform sets RDS `deletion_protection` on and
7-day automated backups; restore procedures are **not verified**.

## Escalation

Escalation contacts, paging tools and dashboards are not defined in the
repository; the placeholders in [on-call-procedures.md](on-call-procedures.md)
are unverified.
