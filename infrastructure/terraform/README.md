# Terraform (AWS)

Terraform for the AWS account that hosts the live NOUS dev environment: EKS
`nous-dev-cluster` in `us-east-1`. No GitHub workflow runs Terraform
(checked 2026-10-17), so state changes are manual operator actions. `plan` and
`apply` need explicit authorization for the exact account and state; this
document records what the `.tf` files declare, not what is currently applied.
Live drift is **not verified**.

For how the application reaches the cluster see
[../../docs/deployment/aws-overview.md](../../docs/deployment/aws-overview.md).

## Layout and state

- `main.tf` declares `required_version >= 1.5.0`, providers (`aws ~> 5.0`,
  `kubernetes`, `helm`, `kubectl`, `random`, `time`) and an empty `backend "s3" {}`.
  Backend settings are supplied at `terraform init` time through
  `-backend-config` flags (bucket, key, region, encrypt, DynamoDB lock table);
  the exact command is in the comment at the top of `main.tf`. The state bucket
  and lock table must exist before the first apply (`terraform_state_bucket`
  and `terraform_lock_table` variables).
- `variables.tf` defines inputs; `terraform.tfvars.example` shows lean dev
  sizing. Copy it to `terraform.tfvars` (not committed) and fill
  `owner_email` and `cluster_admin_role_arns`; the file warns that leaving the
  admin ARN list empty risks locking yourself out of the cluster.
- `outputs.tf` exposes cluster, VPC, RDS, ElastiCache, bucket, OIDC and
  `configure_kubectl` outputs.
- `modules/` holds a local `vpc` module and `main.tf`; the root module uses the
  registry `vpc`, `eks`, `rds` and `elasticache` modules.

## What each file manages

| File | Resources |
| --- | --- |
| `main.tf` | VPC (3 AZs, single shared NAT gateway), EKS cluster (core addons plus the EBS CSI driver), node and cluster IAM roles, application S3 bucket, RDS PostgreSQL 16 (`<project>-postgres`, encrypted gp3, 7-day backup retention, deletion protection on), ElastiCache Redis 7 replication group (TLS and at-rest encryption, single node, failover off), and the RDS/Redis security groups. |
| `ecr.tf` | ECR repositories `nous/backend` and `nous/frontend` (scan on push, lifecycle policies). |
| `acm.tf` | ACM certificate for `*.goodwiinz.tech` with DNS validation. DNS is Cloudflare, not Route 53: the validation CNAME is added by hand, and the file warns not to `apply -target` the validation resource before the CNAME exists. |
| `github-oidc.tf` | GitHub Actions OIDC trust: an ECR push role and an EKS deploy role. The provider itself is looked up, not created. The deploy role trust is limited to the `develop` branch; the ECR role trust also allows `migration/aws`. Which workflows currently assume these roles is **not verified**; `docker-build.yml` is the intended ECR consumer. |
| `alb-irsa.tf`, `alb-controller-iam-policy.json` | IRSA role and policy for the AWS Load Balancer Controller. |
| `cluster-autoscaler-irsa.tf` | IRSA role and policy for the Cluster Autoscaler. |
| `ebs-csi-irsa.tf` | IRSA role for the EBS CSI driver. |
| `external-secrets-rds-irsa.tf` | IRSA role letting service account `aws-rds-secret-reader` in `multimodal-rag-system` read the RDS-managed master credential from Secrets Manager. The chart's ExternalSecret turns it into `aws-database-credentials`. |
| `app-s3-irsa.tf` | IRSA role for the backend service account (`nous-dev-aws-knowledge-graph-analytics`) to reach the application S3 bucket. The bucket is referenced by name and was created outside this stack. |
| `admin.tf` | Cluster admin IAM role. |
| `backup.tf` | Backup S3 bucket with versioning, encryption and public-access block; AWS Backup vault, KMS key, and RDS plan (daily kept 30 days, weekly kept 90 days) and EKS plan; Velero IAM role; backup-failure alarm; verification Lambda role. The cross-region bucket, replication and RDS automated-backup replication only exist when `enable_cross_region_backup` is true. Whether restores have ever been tested is **not verified**. |
| `monitoring.tf` | CloudWatch log groups, a CloudWatch dashboard, alarms (cluster CPU and memory, database CPU and storage, Redis memory), an SNS topic `<project>-alerts` with optional email and Slack subscriptions, and an X-Ray sampling rule. |

## Alerting scope

Terraform defines CloudWatch and SNS alerting for the AWS resources. The
Prometheus, Alertmanager and Grafana files under
[../../monitoring/](../../monitoring/README.md) are a local compose stack, and
[../../deployment/monitoring/](../../deployment/monitoring/README.md) holds
unconsumed legacy manifests. Whether `ContainerInsights` metrics actually
exist for the cluster-level alarms is **not verified**.

## Safe read-only checks

```sh
cd infrastructure/terraform
terraform fmt -check -recursive
terraform init -backend=false && terraform validate
```

`validate` needs provider downloads and does not touch state. Never run
`apply`, `destroy` or `import` without explicit authorization, and keep
`terraform.tfvars`, state files and the Slack webhook out of commits.
