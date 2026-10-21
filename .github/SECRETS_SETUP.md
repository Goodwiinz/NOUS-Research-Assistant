# GitHub Actions secrets

Which repository secrets the workflows read, and which AWS identity they use.
Checked against `.github/workflows/*.yml` on 2026-10-17. There are no
`staging` or `production` GitHub Environments in use: no workflow declares an
`environment:`, and the earlier `STAGING_SSH_KEY`, `STAGING_HOST`,
`STAGING_USER`, `STAGING_DEPLOY_PATH`, `STAGING_API_URL` and Docker Hub secrets
are not referenced by any workflow. Only the AWS dev environment is live; see
[workflows/README.md](workflows/README.md).

Set secrets under **Settings -> Secrets and variables -> Actions**. Never paste
a secret value into an issue, PR, log or document. Which secrets are actually
set in the repository is **not verified** from files; only code that reads them
is.

## Secrets read by workflows

| Secret | Workflow | Purpose |
| --- | --- | --- |
| `RELEASE_PR_TOKEN` | `release-dev.yml` | Fine-grained PAT (Pull requests: read and write) used only to open the digest-pinned release PR so its checks start normally. Optional: without it the workflow falls back to the built-in token and warns that a human must approve the PR's workflow runs. Details in [workflows/README.md](workflows/README.md). |
| `SUPABASE_DB_URL` | `supabase-migrations.yml` | Connection string for `supabase db push` against the hosted Supabase project. |
| `INFISICAL_CLIENT_ID`, `INFISICAL_CLIENT_SECRET` | `trigger-deploy.yml` | Universal Auth machine identity used to read Trigger.dev secrets from Infisical (project `nous-platform`, env `dev`, path `/trigger`) before `trigger.dev deploy`. |
| `OPENCODE_API_KEY` | `opencode.yml` | Key for the `/oc` and `/opencode` comment bot, restricted to owner, member and collaborator comments. |
| `CODECOV_TOKEN` | `test-pipeline.yml` | Coverage upload. |
| `LANGSMITH_API_KEY`, `AZURE_OPENAI_*`, `EVAL_DATABASE_URL`, `EVAL_NEO4J_*`, `EVAL_REDIS_URL`, `EVAL_DO_API_TOKEN`, `EVAL_DO_KB_*` | `agent-eval.yml` | Weekly agent regression eval. It runs on a `[self-hosted, linux]` runner; see [../infrastructure/runner/README.md](../infrastructure/runner/README.md) for the unverified runner status. |

## AWS access (no static keys)

`docker-build.yml` (called by `release-dev.yml`) requests `id-token: write` and
assumes the IAM role `nous-github-ecr-push` in `us-east-1` through GitHub OIDC
to push images to ECR. No AWS access key is stored in GitHub. The role and its
trust policy are defined in
[`infrastructure/terraform/github-oidc.tf`](../infrastructure/terraform/github-oidc.tf);
see [../infrastructure/terraform/README.md](../infrastructure/terraform/README.md).
A second role in the same file, for EKS deploys, is trusted for `develop` only;
whether any workflow assumes it is not verified.

## Not secrets

The frontend deploys through Vercel; its environment is configured there, not
in GitHub. Application runtime secrets (database, Redis, Supabase, LangSmith)
live in Infisical and AWS Secrets Manager, not GitHub; see
[../docs/infrastructure/infisical-setup.md](../docs/infrastructure/infisical-setup.md).
The `secret-scan.yml` workflow (gitleaks) needs no secret.

## Repository settings the release flow needs

Enable **Allow auto-merge** and **Allow GitHub Actions to create and approve
pull requests**, and keep **Release Gate** required on `develop`. These are
described in [workflows/README.md](workflows/README.md).
