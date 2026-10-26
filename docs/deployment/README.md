# Deployment references

Deployment designs, Docker instructions, CI discussions and recorded release procedures. The live target is AWS EKS; most older guides here describe earlier generations and now carry a status banner. For the current target and rollout boundaries start with the AWS overview and the Helm chart contract.

[Documentation home](../README.md) · [Current engineering contracts](../engineering/README.md)

## Current

| File | Subject |
| --- | --- |
| [aws-overview.md](aws-overview.md) | Live AWS dev deployment: Argo CD applications, release flow, migration wave order, rollback |
| [Chart contract](../../infrastructure/helm/knowledge-graph-analytics/README.md) | Secrets, database migrations, values files |
| [CI and release workflows](../../.github/workflows/README.md) | Release Gate, `release-dev.yml`, workflow table |
| [AWS on-call commands](../operations/aws-on-call.md) | Read-only first-response commands |
| [Terraform](../../infrastructure/terraform/README.md) | AWS resources (EKS, RDS, ECR, ACM, OIDC, backup) |

## Dated records (not rewritten)

| File | Subject |
| --- | --- |
| [2026-09-17-chat-audit-release.md](2026-09-17-chat-audit-release.md) | Chat audit release preparation, 2026-09-17 |
| [2026-10-01-deployment-consistency-repair.md](2026-10-01-deployment-consistency-repair.md) | Deployment consistency repair, 2026-10-01 |
| [SENTRY_BACKEND_RUNBOOK.md](SENTRY_BACKEND_RUNBOOK.md) | Backend Sentry finish-up plan (dated status) |

## Historical guides (bannered, kept for reference)

These were written for earlier deployment generations. Each file starts with a status banner and the text is otherwise unedited. They stay in place because other documents link to them.

| File | Subject |
| --- | --- |
| [CI_CD_BEST_PRACTICES.md](CI_CD_BEST_PRACTICES.md) | CI/CD Best Practices for RAG System |
| [DEPLOYMENT.md](DEPLOYMENT.md) | Deployment Guide (Multimodal Enterprise RAG System) |
| [DEPLOYMENT_GUIDE.md](DEPLOYMENT_GUIDE.md) | Deployment Guide (second copy) |
| [DEPLOYMENT_TRADITIONAL.md](DEPLOYMENT_TRADITIONAL.md) | Traditional Deployment Guide (Without Docker) |
| [DOCKER_OR_NOT.md](DOCKER_OR_NOT.md) | Should You Use Docker? Decision Guide |
| [DOCKER_SETUP.md](DOCKER_SETUP.md) | Docker Setup |
| [FIX_STARTUP_ISSUES.md](FIX_STARTUP_ISSUES.md) | Fixing Startup Issues |
| [OPERATIONS_RUNBOOK.md](OPERATIONS_RUNBOOK.md) | Operations Runbook (duplicate of the one in `docs/operations/`) |
| [PACKAGE_MANAGEMENT_FIX.md](PACKAGE_MANAGEMENT_FIX.md) | Package Management Conflict Resolution |
| [PRODUCTION_DEPLOYMENT_GUIDE.md](PRODUCTION_DEPLOYMENT_GUIDE.md) | Production Deployment Guide |
| [RUNBOOKS.md](RUNBOOKS.md) | Runbooks |
| [TODO-CI-SETUP.md](TODO-CI-SETUP.md) | CI/CD Setup TODO |
| [document_upload_deployment.md](document_upload_deployment.md) | Document Upload Feature Deployment Guide |
