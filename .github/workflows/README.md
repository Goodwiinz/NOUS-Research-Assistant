# CI and release workflows

A successful Test Pipeline on `develop` starts `release-dev.yml`, which builds
that exact source SHA and opens a digest-pinned GitOps pull request using the
built-in `GITHUB_TOKEN`. No GitHub App private key or personal token is needed.
The workflow dispatches Test Pipeline, Secret Scan, and Helm Validate at the
proposal branch because token-created PR events require workflow approval.
Merge the proposal after those checks pass; Argo CD then deploys the image.
Deployment-only commits are skipped to prevent a release loop.

Repository setup: enable **Actions > General > Workflow permissions > Allow
GitHub Actions to create and approve pull requests**. Keep default workflow
permissions read-only. Only the promotion job requests contents, pull-request,
and workflow-dispatch write permissions; it never approves or merges a PR,
changes branch protection, or pushes to `develop`.

## Workflow responsibilities

| Workflow | Trigger | Responsibility |
| --- | --- | --- |
| `test-pipeline.yml` | Push and pull request | Run all required checks and publish the exact `Release Gate` result. |
| `release-dev.yml` | Completed successful Test Pipeline run on `develop` | Build the tested SHA and open a checked `values-dev.yaml` promotion PR. |
| `docker-build.yml` | Reusable call or manual dispatch | Check out an explicit full SHA, assert `HEAD`, push the full-SHA trace tag, and return its digest. Called by `release-dev.yml`. |
| `gitops-image-update.yml` | Manual dispatch only | Retained legacy production image update; it has no dev role. |
| `deploy.yml` | Manual dispatch only | Retained legacy staging/production Helm path; it has no dev role and requires an explicit image tag. |
| `helm-validate.yml` | Push and pull request | Validate the Helm chart and environment values. |
| `workflow-lint.yml` | Workflow changes | Run `actionlint` across all workflows. |

The frontend is deployed separately through Vercel. The backend, migration init
container, Celery worker, Celery beat, and synthetic-traffic workloads share the
same backend digest through the Helm image helper.

## Removed: the original automatic dev release

`release-dev.yml` ("Release Dev") implemented an exact-SHA dev release: on
every successful `Test Pipeline` run on `develop` it would build a backend
image via `docker-build.yml` and promote the digest onto a `deploy/dev` branch
for Argo CD to pick up.

It was removed on 2026-08 because it never worked end to end and was costing
real runner time for no benefit:

- `deploy/dev` was never seeded — the branch never existed, so the final
  promotion step had nothing valid to push against.
- The required App credential (`DEV_RELEASE_APP_ID` /
  `DEV_RELEASE_APP_PRIVATE_KEY`) was never provisioned, so the "Promote
  Immutable Dev Digest" step always failed with
  `[@octokit/auth-app] appId option is required`.
- Because `develop` CI had been red for weeks, the workflow only recently
  started actually firing, and every green push was burning a full backend
  image build (~17 minutes of runner time) that produced an image nothing
  consumed, then failing.

Argo CD's `nous-dev` Application (`infrastructure/argocd/applications/dev.yaml`)
tracks `develop` directly and was never switched over to `deploy/dev`, so
removing this workflow does not orphan any live deployment path.

### Design notes preserved for a deliberate reintroduction

The original workflow enforced a real chain of guarantees that any
reintroduction should preserve:

```text
push to develop
      |
      v
Test Pipeline at source SHA
      |
      | completed/success + Release Gate completed/success
      v
Release Dev (single writer)
      |
      +-- re-fetch develop; stop if source SHA is stale
      |
      +-- call Docker Build with that exact source SHA
      |     `backend:<full-source-SHA>` is a trace tag
      |     `sha256:...` is the immutable deployment identity
      |
      +-- validate Helm in a read-only job; stop if source SHA is stale
      |
      `-- on a fresh runner, prepare a bounded two-file commit
            recheck develop, mint the App token only at the write boundary,
            then force-with-lease only deploy/dev
```

Release authorization relied on all of:

1. The event being a completed, successful Test Pipeline `push` run for
   `develop` in this repository.
2. The producer run returned by the Actions API matching event, repository,
   branch, `workflow_run.head_sha`, and `workflow_run.run_attempt`.
3. The attempt-specific jobs endpoint returning exactly one producer job named
   `Release Gate`, with status/conclusion exactly `completed`/`success` for
   that SHA. A rerun could never borrow the gate result from another attempt.
4. A fresh `develop` read still pointing to the tested SHA before build.
5. Read-only Helm validation succeeding for the built digest, followed by
   another `develop` check.
6. A fresh promotion runner checking `develop` before preparing the bounded
   commit and again immediately before minting a write token.

It used a single concurrency group with cancellation, `force-with-lease` to
protect `deploy/dev` from an undetected competing update, a short-lived
GitHub App installation token minted only at the write boundary, and recorded
per-build evidence (source SHA, producer run ID/attempt/URL, trace tag,
digest, `release-evidence.json`, `release-dev.json`) in the release commit.

**If you want to reintroduce this:** see the removed file's history —
`git log --diff-filter=D -- .github/workflows/release-dev.yml` — for the full
implementation, and re-provision the following before re-enabling it:

1. A first-stage `develop` ruleset requiring pull requests, approval, and
   resolved conversations, blocking direct pushes. Do not require `Release
   Gate` as a status check yet — it has not produced a proven check run.
2. Seed `deploy/dev` and add a ruleset that permits only a dedicated release
   GitHub App to perform the workflow's non-fast-forward update, with no
   bypass on `develop` or other protected source refs.
3. Provision `DEV_RELEASE_APP_ID` (Actions variable) and
   `DEV_RELEASE_APP_PRIVATE_KEY` (Actions secret) for that App, installed only
   where release commits are needed, granted repository Contents write, and
   run shadow releases.
4. After one same-SHA green shadow run, extend the `develop` ruleset with the
   required `Release Gate`, up-to-date branch, and secret-scanning
   requirements.
5. Only then consider cutting Argo CD's `nous-dev` Application over from
   `develop` to `deploy/dev` — a separate, explicitly authorized operation
   (pausing/re-enabling root auto-sync, manually syncing the cutover, and
   collecting Application/revision and live pod `imageID` evidence).

`docker-build.yml` and `gitops-image-update.yml` were deliberately left in
place (see their own top-of-file comments) since neither self-triggers and
both remain useful building blocks — the former as the reusable build step,
the latter as the retired manual production-promotion path.

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
for env in dev staging production; do
  helm lint "$chart" -f "$chart/values.yaml" -f "$chart/values-$env.yaml"
  helm template rag "$chart" \
    -f "$chart/values.yaml" -f "$chart/values-$env.yaml" >/dev/null
done
bash "$chart/tests/backend_image_digest_render_test.sh"
```
