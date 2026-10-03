# CI and release workflows

A successful Test Pipeline on `develop` starts `release-dev.yml`, which builds
that exact source SHA and opens a digest-pinned GitOps pull request. PR creation
uses `RELEASE_PR_TOKEN` when configured so the PR's workflow checks start
normally. PRs created with the built-in `GITHUB_TOKEN` require a human to approve
their workflow runs; manually dispatched workflows do not satisfy the PR's
checks. If the secret is absent, creation falls back to `GITHUB_TOKEN` and emits
a warning that the owner must approve the PR's workflow runs.

The workflow validates the AWS Helm candidate and enables squash auto-merge
for the proposal's exact head. GitHub waits for configured required checks and
an up-to-date branch, then AWS Argo CD deploys the merged image from `develop`.
Deployment-only commits are skipped to prevent a release loop.
The proposed file is `values-aws.yaml`; `values-dev.yaml` is historical DO
rollback material. All three AWS Argo CD definitions (`aws-dev`, `nous-dev-aws`,
and `nous-dev-aws-ingress`) track `develop` with automated sync.

After the source freshness check, promotion lists up to 100 open PRs based on
`develop` and closes superseded proposals whose head starts with
`codex/release-dev-`, deleting their branches and commenting with the new
source's short SHA. The current proposal branch is excluded.

Repository setup: enable **Allow auto-merge** and **Actions > General > Workflow permissions > Allow
GitHub Actions to create and approve pull requests**. Keep default workflow
permissions read-only. Only the promotion job requests `contents: write` and
`pull-requests: write`. It requests auto-merge only for its generated release
proposal, without bypassing branch rules or approving PRs.
AWS Helm lint/render runs before the request because Helm Validate is not
currently a required repository check.

Create the repository Actions secret **`RELEASE_PR_TOKEN`** using a fine-grained
personal access token restricted to **Goodwiinz/NOUS-Research-Assistant** with
**Pull requests: read and write**. Use a token whose owner has access to this
repository, with organization approval if required. The secret is used only
for `gh pr create`; Git pushes, PR lookups, superseded-proposal cleanup, and
auto-merge continue to use `GITHUB_TOKEN`. No GitHub App private key is needed.

`develop` requires **Release Gate** from GitHub Actions and **Require branches
to be up to date before merging** (restored 2026-10-01). Release preparation
and promotion each verify the effective branch rule with a read-only preflight;
missing protection fails before image construction or proposal creation.
The Lint Backend job checks each generated release branch against the current
`develop` SHA. Release Gate includes that job; these guards reject a proposal if
source advances during CI or before merge; updating the old branch does not
make its old image eligible again. Auto-merge only waits for checks that are
actually required by the repository.

## Workflow responsibilities

| Workflow | Trigger | Responsibility |
| --- | --- | --- |
| `test-pipeline.yml` | Push, pull request, or manual dispatch | Select affected PR checks; run full CI on protected-branch pushes and manual runs; publish the exact `Release Gate` result. |
| `release-dev.yml` | Completed successful Test Pipeline run on `develop` | Build the tested SHA, validate AWS values, retire superseded proposals, and request auto-merge of a checked `values-aws.yaml` promotion PR. |
| `docker-build.yml` | Reusable call or manual dispatch | Check out an explicit full SHA, assert `HEAD`, push the full-SHA trace tag, and return its digest. Called by `release-dev.yml`. |
| `helm-validate.yml` | Push and pull request | Validate the Helm chart and environment values. |
| `workflow-lint.yml` | Workflow changes | Run `actionlint` across all workflows. |
| `supabase-migrations.yml` | Push to `develop` touching `supabase/migrations/`, or manual dispatch | Apply pending Supabase migrations to the hosted project with `supabase db push` (repository secret `SUPABASE_DB_URL`; the job runs only on `develop`). |

The frontend deploys separately through Vercel. Its draft claims/release
controls discover available operations from the deployed backend schema and
stay unavailable until the required operations exist. The backend, AWS migration
Job, Celery worker, Celery beat, and synthetic workloads share the same backend
digest. AWS Argo waits for the wave-1 migration Job before wave-2 consumers;
see the [chart contract](../../infrastructure/helm/knowledge-graph-analytics/README.md#database-migrations).

The retired manual staging/production deploy paths and duplicate deployment
workflow tree have been removed. `trigger-deploy.yml` remains unchanged; its
Trigger.dev production deployment is not part of the AWS dev release path.

## Change-based PR checks

Test Pipeline always runs and publishes **Release Gate**, including for
documentation-only PRs. `CI Plan` reads the complete PR diff against the merge
base of the live target branch. It includes deleted paths and both sides of a
rename. Check selection depends on affected files, never the number of changed
lines: a one-line dependency or workflow change still gets full CI.

| PR profile | Changes | Blocking checks |
| --- | --- | --- |
| Documentation | Allowlisted root and directory docs, Markdown under `docs/`, and Claude command instructions | Directory-doc lint, script/NOUS contracts, CI selection and Release Gate regressions |
| Frontend | Frontend files without shared dependency/build changes | Lightweight checks, frontend lint/types, frontend/terminal/harness tests, E2E smoke |
| Backend | Backend files without shared dependency/build changes | Lightweight checks, backend lint, migration and OpenAPI contracts, security, unit/golden/integration/resilience tests, E2E smoke |
| Full | Mixed frontend/backend changes, generated API contract artifacts, CI/scripts, shared dependencies/build files, infrastructure, unknown paths, or an empty diff | Lightweight checks and every existing blocking job |

Markdown under `backend/src/` or `frontend/src/` is runtime content and stays in
its code profile. Release proposal branches (`codex/release-dev-<source SHA>`)
always use full CI so the existing stale-source guard runs.

Every push to `develop`/`main` and every manual dispatch also uses full CI.
This preserves the successful `develop` push as the release workflow's source
verification. The optimization currently applies to PRs only.

Release Gate requires exact success from CI Plan, Lightweight Checks, and all
selected jobs. Only explicitly unselected jobs may be skipped; their summary
rows say **not selected**. Missing results, failures, cancellations, unknown
profiles/dependencies, and inconsistent plan outputs block the gate. The branch
rule still requires Release Gate and an up-to-date branch.

Run the selection and gate regressions without backend services:

```bash
python3 -m pytest backend/tests/unit/ci/test_ci_scope.py \
  backend/tests/unit/ci/test_release_gate.py \
  --noconftest -c /dev/null -q -p no:cacheprovider --no-cov
```

## Manual Docker build

Manual image construction is available for diagnostics:

1. Open **Actions -> Docker Build -> Run workflow**.
2. Supply a full lowercase 40-character commit SHA.
3. Record the returned full-SHA trace tag and registry digest.

No branch tip or short SHA is inferred, and this does not promote anything to
dev or production.

## Local validation

Run the same workflow validator used in CI:

```bash
actionlint
```

For changes that also touch the release values/helper, lint and render each
real base-plus-environment combination. The base values file is intentionally
incomplete and does not lint on its own.

```bash
chart=infrastructure/helm/knowledge-graph-analytics
for env in dev staging production aws; do
  helm lint "$chart" -f "$chart/values.yaml" -f "$chart/values-$env.yaml"
  helm template rag "$chart" \
    -f "$chart/values.yaml" -f "$chart/values-$env.yaml" >/dev/null
done
bash "$chart/tests/backend_image_digest_render_test.sh"
python3 "$chart/tests/deployment_consistency_render_test.py"
```
