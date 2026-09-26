# DigitalOcean → AWS Migration Design

**Date:** 2026-09-14
**Status:** Approved design (sections 1-4 approved by user)

## Decision Summary

| Decision | Choice |
|---|---|
| Scope | Full exit from DigitalOcean |
| Compute | EKS (port existing Helm/ArgoCD stack) |
| Cutover | Simple freeze-and-migrate, hours of downtime acceptable |
| Account/Region | Existing AWS account (`aws login`), us-east-1 |
| Cost target | Lean config, ~$220-275/mo (DO baseline ≈ $200-250/mo) |
| Alternatives rejected | B: managed-heavy rework (Neptune/OpenSearch — rewrite cost); C: bridge phase (two migrations, cross-cloud latency) |

## Current DO Stack (migrating from)

- DOKS cluster `do-nyc3-rag-system-cluster`, ArgoCD auto-sync develop→dev, Helm charts
- DO managed PostgreSQL (`multimodal_rag`), DO managed Redis/Valkey
- In-cluster StatefulSets: Neo4j 5.26, Qdrant 1.13
- DO Spaces bucket `rag-system-storage` (nyc3), DO Container Registry
- DNS: Cloudflare via external-dns; frontend on Vercel (unchanged)

## Section 1: AWS Infrastructure (Terraform)

New `infrastructure/terraform/aws/`:

- **VPC**: 3 AZs, public+private subnets, 1 NAT gateway (dev)
- **EKS**: v1.31, **1× t3.large (spot) + cluster autoscaler** (lean, approved), addons: aws-load-balancer-controller, ebs-csi-driver (gp3), external-dns, secrets-store CSI
- **RDS PostgreSQL 16**: `db.t4g.small` (lean, approved), single-AZ, 7d automated backups
- **ElastiCache Valkey**: `cache.t4g.small`, 1 node
- **S3**: `nous-storage-us-east-1`, versioning on, SSE-S3
- **ECR**: repos for backend/frontend/worker images
- **IAM**: IRSA roles per service account (ECR pull, S3 access, secrets access)
- **Secrets**: AWS Secrets Manager + external-secrets → k8s secrets

Estimated cost: ~$220-250/mo lean (control plane $73, spot node ~$36, RDS ~$35, ElastiCache ~$25, NAT ~$33, ALB ~$18, storage/misc ~$15).

## Section 2: Workload Migration

- Reuse `infrastructure/helm/rag-system` + `knowledge-graph-analytics` charts unchanged
- New `values-aws.yaml`: ECR image repos, AWS SDK default S3 endpoint (drop Spaces path-style), RDS/ElastiCache endpoints, storageClass `gp3`
- Ingress → aws-load-balancer-controller ALB; external-dns keeps Cloudflare provider, records point at ALB
- Neo4j + Qdrant StatefulSets unchanged on EBS gp3
- ArgoCD reinstalled on EKS, app-of-apps, new `overlays/aws-dev`; auto-sync develop→dev preserved
- external-secrets operator replaces current secrets flow
- kube-prometheus-stack + Grafana/Prometheus as today

## Section 3: Data Migration + Cutover Runbook

1. **Pre-stage (before freeze)**: S3 bucket + ECR repos; first `rclone sync` Spaces→S3; build+push images to ECR; `terraform apply`; ArgoCD synced with ingress suspended
2. **Freeze**: pause ArgoCD on DO, scale DO deployments to 0
3. **Postgres**: `pg_dump` DO → restore RDS
4. **Spaces final delta**: second `rclone sync`
5. **Neo4j**: `neo4j-admin database dump` → restore on EKS PVC
6. **Qdrant**: collection snapshots → restore
7. **Redis**: no migration (Celery broker/cache, cold start OK)
8. **DNS**: Cloudflare CNAMEs `dev-app`/`dev-api` → ALB; TLS via cert-manager
9. **Unfreeze**: scale up EKS, smoke test (auth, upload, chat, graph, search)
10. **Rollback**: DO kept alive 2 weeks; unfreeze DO + repoint DNS back

## Section 4: CI/CD + Decommission

- GitHub Actions: DO registry → `aws-actions/amazon-ecr-login` with OIDC role (no static keys); CD targets EKS/ArgoCD
- Backend env: remove `S3_ENDPOINT`, set AWS region; presigned URL logic unchanged
- Vercel frontend: unchanged
- **Decommission** (after 2-week soak): delete DO LBs, archive PG/Spaces/Neo4j snapshots to S3 Glacier, destroy DOKS, archive DO terraform state
- **Testing**: terraform validate/plan in CI; ported k8s manifest tests; post-cutover smoke suite; optional load test

## Open Items (deferred to implementation planning)

- Exact node sizing validation after load test
- Neo4j password rotation via external-secrets (existing P1 item)
- Snapshot schedule for EBS volumes (replaces DO weekly Qdrant backup cron)
