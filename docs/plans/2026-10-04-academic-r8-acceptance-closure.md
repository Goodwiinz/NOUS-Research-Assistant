# Academic R8 Acceptance Closure Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Produce the live acceptance evidence that GOO-319 and GOO-320 still lack: a real Celery worker and beat, real Redis and PostgreSQL, live Crossref/PubMed/OpenAlex. Fix any defect that evidence exposes, and leave the dev rollout ready for the owner.

**Architecture:** One typed harness (`backend/scripts/validation/r8_live_proof.py`) seeds isolated worlds in a scratch database migrated with the real Alembic chain, creates schedules through the real service layer, then drives `celery worker`, `celery beat` and `redis-server` child processes it owns through outage, duplicate-beat, crash-restart and revocation scenarios. It records a timeline from the database. GOO-320's successor chain then runs against the real delta. Evidence lands in `docs/audits/2026-10-04-live-evidence-r8/`.

**Tech Stack:** Python 3.11 (`backend/.venv` of the main checkout), Celery 5 + Redis, PostgreSQL 14 (local Homebrew), SQLAlchemy async, `gh`, Linear MCP.

---

## Status record (2026-10-04, `origin/develop` at `755312ffc`)

This is a dated plan. The roadmap rationale stays in `docs/plans/2026-09-30-goo-319-scheduled-updates.md` and `docs/plans/2026-09-30-goo-320-superseding-reviews.md`; neither is edited.

| Fact | Evidence |
| --- | --- |
| GOO-319/320 code is on develop | `f59725d87` (#1815), `ebcf2ce0b` (#1816); `git merge-base --is-ancestor` both true |
| Linear: 29 of 31 Done; GOO-319/320 In Review | Linear, 2026-10-04 |
| Open items are evidence, not code | Linear comments on both tickets list **NOT RUN**: beat across a real worker restart, live providers, live rag-dev journey, methods-expert review |
| Dev cannot receive R8 | Release Dev fails every run at "Verify release branch protection" (`RELEASE_PR_TOKEN` lacks Administration: Read); latest failure run 37181835414 at 2026-10-04T06:06Z |
| Dev is pinned at `8ad2c82` | `infrastructure/helm/knowledge-graph-analytics/values-aws.yaml` lines 64-66; migrations `c0f2a4b6d8e9`..`d4a6c8e0f2b3` are not applied there |
| The update tick is dormant until enabled | `SEARCH_UPDATES_ENABLED` defaults to False (`backend/src/core/config.py:191`); not in `values-aws.yaml`; `celeryBeat.enabled: true` is |
| Fixture DOI with real notices | Crossref `filter=updates:` for `10.1016/S0140-6736(97)11096-0` returns a `correction` (`10.1016/s0140-6736(04)15715-2`) and a `retraction` (`10.1016/s0140-6736(10)60175-4`); alternate `10.1016/j.fct.2012.08.005`. `10.1126/science.1112539` returns none and must not be used |

## Non-goals

- No change to GOO-319/320 behavior unless the live run proves a defect.
- No Linear status change. Both tickets stay In Review until the owner's gates clear.
- No dev deploy, secret change or merge. Those are the owner's.
- The out-of-scope seams in the two R8 plans (notifications, sub-hourly schedules, missed-fire backfill, Zenodo new version for superseding releases) are not built.

## Guardrails

- Work only in `/Users/goodwiinz/development/RAG_system-wt-r8-live` on branch `docs/r8-acceptance-closure`. Never touch the main checkout. Unset the branch upstream (it tracks `origin/develop`) before any push.
- No `git pull`, `git checkout`, `git reset`; stage path-explicit, never `git add -A`; `git fetch` before each push.
- No `rm`. Scratch resources are left in place and listed in the evidence README.
- Process control is limited to processes the harness spawned itself (private `redis-server` on port 6391, one `celery worker`, up to two `celery beat`). The crash scenario hard-kills a harness-owned process group, which needs the owner's explicit OK (Task 5, Step 1).
- Evidence contains no environment values, tokens or personal data (`docs/AGENTS.md`). Seeded users use `@test.invalid` addresses. `CROSSREF_MAILTO` stays unset: the owner's address is not sent to a third party.
- Live provider volume stays small: one query per provider per fire and one Crossref notice batch.
- PRs reference tickets with a non-closing keyword (`Part of GOO-319`), so merging cannot move a ticket to Done.
- Commits end with the session attribution trailers; PR bodies end with the generated-by lines.

## Evidence map

| Acceptance box (Linear) | Experiment | Artifact |
| --- | --- | --- |
| GOO-319: schedules survive restart, retained evidence, visible failures | S-crash: hard-kill worker mid-run, restart, one execution, attempts `started`, `started(retry_of)`, `succeeded` | `scenario-crash.json` |
| GOO-319: missed/overlapping/duplicate ticks nonduplicating | S-main with two beats ticking, plus the queue backlog replayed after a worker restart | `scenario-main.json` |
| GOO-319: deltas resolve to strategy, execution, two corpus snapshots | S-main `delta.json` (sealed export) and the baseline snapshot | `delta-main.json`, `snapshots-main.json` |
| GOO-319: new/changed/corrected/retracted/unchanged/unknown without false merges | S-main against the seeded baseline (unchanged, changed, retracted DOI, absent fake) | `delta-main.json` |
| GOO-319: outage and absent publication checks stay unknown | S-outage: Crossref unreachable by proxy, other providers up | `scenario-outage.json` |
| GOO-319: archived/revoked project cannot start an update | S-revoke: workspace archived before the fire | `scenario-revoke.json` |
| GOO-320: counts reconcile, targeted queue, successor from a real delta | Chain on S-main's real delta: root, successor, accounting, ensure work, both exports | `review-chain.json` |
| Named producer CI jobs | Integration job on develop that ran the R8 PostgreSQL proofs | `ci-jobs.md` |
| Database evidence on today's develop | The two R8 PostgreSQL proofs rerun locally, junit retained | `junit-r8-proofs.xml` |

## Tasks

### Task 1: Harvest the named producer CI jobs

**Files:**
- Create: `docs/audits/2026-10-04-live-evidence-r8/ci-jobs.md`

**Step 1:** Find the latest green develop run that executed the integration suite.

Run: `gh run list --branch develop --limit 15 --json databaseId,name,conclusion,headSha,createdAt --jq '.[] | "\(.databaseId) \(.name) \(.conclusion) \(.headSha[0:9]) \(.createdAt)"'`
Expected: a run list; pick the newest `success` of the workflow that has an "Integration Tests" job.

**Step 2:** List its jobs and confirm the R8 proofs ran.

Run: `gh run view <run-id> --json jobs --jq '.jobs[] | select(.name|test("Integration")) | "\(.databaseId) \(.name) \(.conclusion) \(.url)"'`
Then: `gh run view <run-id> --job <job-id> --log | grep -E "test_search_updates_postgres|test_review_versions_postgres|passed|failed" | head`
Expected: both files named with passes. If the proofs are skipped (no PostgreSQL service), record that as the finding and rely on Task 2.

**Step 3:** Write `ci-jobs.md` (run id, job id, URL, head SHA, conclusion, the matching log lines). **Commit** path-explicit.

### Task 2: Rerun the two R8 PostgreSQL proofs on today's develop

**Files:**
- Create: `docs/audits/2026-10-04-live-evidence-r8/junit-r8-proofs.xml`

**Step 1:** Create a scratch database for the proofs.

Run: `psql -h 127.0.0.1 -U goodwiinz -d postgres -c 'CREATE DATABASE r8_proofs_20261004'`
Expected: `CREATE DATABASE`.

**Step 2:** Run the proofs from `backend/` using the main checkout's venv and this worktree's source.

```sh
cd /Users/goodwiinz/development/RAG_system-wt-r8-live/backend
RESEARCH_DECISION_DATABASE_URL=postgresql://goodwiinz@127.0.0.1:5432/r8_proofs_20261004 \
PYTHONPATH="$PWD" /Users/goodwiinz/development/RAG_system/backend/.venv/bin/python -m pytest -q \
  tests/integration/test_search_updates_postgres.py tests/integration/test_review_versions_postgres.py \
  -p no:cacheprovider --no-cov --junitxml=../docs/audits/2026-10-04-live-evidence-r8/junit-r8-proofs.xml
```
Expected: all pass. A failure is a regression since 10-02: stop and go to Task 7 before anything else.

**Step 3:** Commit the junit file path-explicit.

### Task 3: Live provider spike picks the baseline fixtures

**Files:**
- Create: `docs/audits/2026-10-04-live-evidence-r8/spike-providers.json`

**Step 1:** Run the pinned query once against the real connectors (`build_connectors`) for `crossref`, `pubmed`, `openalex` with 10 results each. Record, per provider, titles, DOIs and years, plus whether the provider reported more results than the cap.

**Step 2:** Choose fixtures from that output: `P1` (a result returned by Crossref, baseline copy exact so it classifies `unchanged`), `P2` (another returned result with the year raised by one, so it classifies `changed`), `R` = `10.1016/S0140-6736(97)11096-0` (not returned by the query, classifies `corrected_retracted` through a real notice), `Z` = a made-up DOI under `10.5555/` (never returned, classifies `unknown`). Re-probe `R` with `filter=updates:` the same day (command in the status record).

**Step 3:** Confirm the exact PubMed and OpenAlex hostnames from the connector source for the outage `NO_PROXY` list. Commit the spike file.

### Task 4: Build the harness and make it boot

**Files:**
- Create: `backend/scripts/validation/r8_live_proof.py`

The harness is fully typed (CI runs mypy `--disallow-untyped-defs` on added files) and uses argv-only `subprocess` (`scripts/AGENTS.md`). Components, in order:

1. **Config**: `R8_DATABASE_URL` (asyncpg URL of the scratch database), Redis port 6391, `--out DIR`, `--phase` selectors. Child environments are built from an allowlist (`PATH`, `HOME`, `LANG`, `TMPDIR`) plus `DATABASE_URL`, `REDIS_URL`, `SEARCH_UPDATES_ENABLED=true`, `PYTHONPATH`; never the caller's full environment.
2. **Seed** (`seed_world`), reusing the PG proof helpers rather than rewriting them:

```python
from tests.integration.research_engine_postgres_support import seed_approved_protocol_binding
from tests.integration.test_report_identity_postgres import _seed

async def seed_world(factory, providers, query, baseline) -> World:
    ids = await _seed(factory)  # org, users O R A V F, workspace, collection, run
    async with factory() as db:
        db.add(ResearchProjectRoleAssignment(collection_id=ids["collection"], user_id=ids["O"],
                                             role=ResearchProjectRole.SUPERVISOR, assigned_by_id=ids["O"]))
        blueprint_id = (await db.execute(text("SELECT blueprint_id FROM research_runs WHERE id = :r"),
                                         {"r": ids["run"]})).scalar_one()
        binding = await seed_approved_protocol_binding(
            db, blueprint_id=blueprint_id, collection_id=ids["collection"], author_id=ids["O"],
            steps=[{"id": "search", "type": "search"}], parameters={})
        strategy = pinned_strategy(str(binding.protocol_version_id), providers)  # copy of the proof's _strategy, providers as an argument
        # UPDATE research_runs SET status='completed', protocol_version_id, reproducibility_manifest
        #   = {"_search_receipts_v1": {schema_version:1, strategies:{<hash>: strategy}, executions:{}}}
        # + one ResearchStep(step_type="search", output={"query": query, "coverage": {"strategy_version": <hash>}})
        await db.commit()
    await import_baseline(factory, ids, baseline)  # one GOO-300 file-import receipt, as the proof's _seed_corpus
    return World(ids=ids, strategy=strategy)
```

3. **Schedule**: `resolve_project(db, collection, O, ResearchAction.SUPERVISE)` then `search_update_service.create_schedule(db, ctx, O, SearchScheduleCreate(source_run_id=..., step_id="search", strategy_version=..., cron=f"{minute} * * * *", timezone="Europe/London", idempotency_key=...))`. A cron has exactly one minute value (`parse_schedule`), so scenarios are staggered by minute offsets from the start time.
4. **Processes**: `redis-server --port 6391 --save "" --appendonly no --dir <scratch>`; `python -m celery -A src.tasks.celery_app worker -Q celery --concurrency=1`; beat through a launcher that keeps only the real `search-updates-tick` entry:

```python
BEAT = (
    "import sys\n"
    "from src.tasks.celery_app import celery_app\n"
    "tick = celery_app.conf.beat_schedule['search-updates-tick']\n"
    "celery_app.conf.beat_schedule = {'search-updates-tick': tick}\n"
    "celery_app.start(['beat', '--loglevel=INFO', '-s', sys.argv[1]])\n"
)
```
   Each child starts with `start_new_session=True` so a process-group signal reaches the prefork children. `stop()` sends SIGTERM to the group; `hard_kill()` sends SIGKILL (Task 5 only).
5. **Observer**: one SQL join of executions, attempts (ordered `created_at, id`) and results; `wait_for(predicate, timeout)` polls every 3 s and appends every state change, with a UTC timestamp, to `timeline.json`.
6. **Recorder**: `check(name, ok, detail)` accumulates PASS/FAIL; evidence files are written even on failure and the exit status is non-zero if any check failed.

**Step 1 (boot spike):** create the scratch database and migrate it with the real chain.

Run: `psql -h 127.0.0.1 -U goodwiinz -d postgres -c 'CREATE DATABASE r8_live_20261004'`, then
`cd backend && DATABASE_URL=postgresql+asyncpg://goodwiinz@127.0.0.1:5432/r8_live_20261004 PYTHONPATH="$PWD" <venv>/bin/python -m alembic upgrade head`
Expected: ends at the single head (`python ../scripts/ci/check_alembic.py` from `backend/` confirms the head). If a migration needs an extension the local server lacks, record the exact error and fall back to `Base.metadata.create_all` plus the R8 migrations (the proofs' own approach), and say so in the evidence README.

**Step 2:** `r8_live_proof.py --self-check` starts Redis and a worker, confirms the worker registers `src.tasks.search_update_tasks.tick`, then stops them. Expected: prints the registered task and exits 0.

**Step 3:** `ruff check`, `black --check`, `isort --check-only` on the file and `mypy --ignore-missing-imports --follow-imports=silent --no-site-packages` (CI parity; the local tools are pinned in `CLAUDE.md`). **Commit** path-explicit.

### Task 5: Live run (outage, duplicate beats, crash, revocation)

**Step 1 (owner OK required):** ask for one explicit approval covering only: SIGTERM and SIGKILL of the harness's own spawned processes and shutdown of its private Redis on port 6391. Do not run the crash scenario without it; the other scenarios need only SIGTERM, which the same approval covers.

**Step 2:** Run in the background and poll with a monitor; the crash retry waits out `STALE_EXECUTION` (30 minutes), so the run takes about 40 minutes. Schedule (T = harness start):

| T+ | Scenario | Worker env | Expected |
| --- | --- | --- | --- |
| 2 min | S-outage: Crossref blocked (`HTTPS_PROXY=http://127.0.0.1:9`, `NO_PROXY` = the PubMed and OpenAlex hosts and localhost) | outage | execution `succeeded`; Crossref failed in coverage; baseline DOI `R` is `unknown`, not `corrected_retracted` |
| 4 min | Stop the worker (SIGTERM), start a normal worker and a second beat | normal | queued ticks replay, no duplicate executions |
| 5 min | S-main with both beats ticking | normal | exactly one execution; attempts `started`,`succeeded`; delta has `new`, `changed`, `unchanged`, `unknown`, `corrected_retracted` (notices include `retraction`); export sealed |
| 7 min | S-crash: on the `started` attempt, SIGKILL the worker group, start a new worker | normal | attempts stay `[started]` while live (no second run); at +30 min `started(retry_of)`, `succeeded`; one import receipt, one result |
| 9 min | S-revoke: workspace archived before the fire | normal | `failed project_archived`; no `scheduled:<execution_id>` receipt |

**Step 3:** Export `scenario-*.json`, `delta-main.json` (via `export_delta`), `snapshots-main.json`, `timeline.json`, and trimmed worker and beat log excerpts. Scan every file for `key=`, `token`, `Authorization`, `@` before committing. **Commit** path-explicit.

### Task 6: GOO-320 chain on the real delta

**Step 1:** Against S-main's world, in the harness: `create_root(db, ctx, O, ReviewVersionCreate(rationale=..., idempotency_key=...))`, then `create_successor(..., ReviewVersionCreate(parent_review_version_id=root.id, execution_id=<S-main>, delta_hash=<its delta_hash>, reviewer_user_ids=[B], rationale=..., idempotency_key=...))`, then `ensure_work`, `accounting`, `export_version` for both versions.

**Step 2:** Assert: the successor accepts the real delta by `(execution_id, delta_hash)`; carried plus new counts match the delta classes; the baseline reports with no historical decision are labelled missing rather than invented; the targeted queue exists for reviewer B; `accounting` reconciles with no `error`; exporting each version twice returns the same body hash after a reload; a reviewer cannot create a version (403); a foreign user gets 404. Carry-forward of resolved decisions and release linking stay covered by the PostgreSQL proof in Task 2 (this run seeds no screening decisions). **Commit** `review-chain.json`.

### Task 7: Fix any defect found (conditional)

For each defect: reproduce in a focused test first (failing), fix at the shared function, rerun green, and for a race or idempotency guard follow `docs/engineering/testing.md` mutation verification and record file:line plus command in `docs/testing/agent-orchestration-mutation-checks.md`. Branch from `origin/develop`, one small PR per defect. Log new defects as comments on GOO-321 (Linear's free issue limit applies). If a defect invalidates part of the evidence, rerun that scenario; never edit a result to match.

### Task 8: Evidence pack and index

**Files:**
- Create: `docs/audits/2026-10-04-live-evidence-r8/README.md`, `docs/audits/2026-10-04-live-evidence-r8/journey.md`, `docs/audits/2026-10-04-goo-319-live.md`, `docs/audits/2026-10-04-goo-320-live.md`
- Modify: `docs/audits/README.md` (collection and document rows)

Each write-up states date, SHA, environment (local PostgreSQL 14, private Redis, live providers), the experiment, the observed result and a **NOT RUN** list: rag-dev journey (deploy blocked), methods-expert review against PRISMA 2020, Zenodo-related paths, Kubernetes pod restarts. The README lists the scratch databases and port left behind.

**Verify:** `make docs-lint`; `git diff --check -- docs/`; every relative link resolves. **Commit** path-explicit.

### Task 9: Pull request and Linear

**Step 1:** `git fetch origin`, `git branch --unset-upstream`, push `docs/r8-acceptance-closure` explicitly, open the PR with `Part of GOO-319` and `Part of GOO-320`, the evidence summary and the NOT RUN list. Do not arm auto-merge.

**Step 2:** Post one comment each on GOO-319 and GOO-320: SHA, PR, CI job links, the scenario results and the NOT RUN list. Add the PR as a link attachment. Leave both In Review.

### Task 10: Held dev-rollout PR

**Files:**
- Modify: `infrastructure/helm/knowledge-graph-analytics/values-aws.yaml` (add one `backend.env` entry beside `DAILY_RESEARCH_BRIEF_ENABLED`)

**Step 1:** Add `SEARCH_UPDATES_ENABLED: "true"` with a comment `# GOO-319: default-off in code; dev opts in explicitly.` Worker and beat read the merged `backend.env`.

**Step 2:** Verify the render: `helm template nous-dev-aws infrastructure/helm/knowledge-graph-analytics -f infrastructure/helm/knowledge-graph-analytics/values.yaml -f infrastructure/helm/knowledge-graph-analytics/values-aws.yaml | grep -n SEARCH_UPDATES_ENABLED` shows it in the worker and beat deployments.

**Step 3:** Open as a **draft** PR from `origin/develop`. Its body carries the owner runbook: regrant the PAT's Administration: Read, rerun Release Dev, merge the GitOps PR, confirm migrations through `d4a6c8e0f2b3`, merge this PR, then repeat the two journey lists in the R8 plans on rag-dev.

### Task 11: Close the loop

Update the project memory entry for the academic roadmap with the PR numbers and the remaining owner gates. Report to the owner in a short list: what ran, what failed, what is still NOT RUN, and the single action that unblocks dev.

## Owner-only gates (not cleared by this plan)

| Gate | Why only the owner |
| --- | --- |
| Regrant `RELEASE_PR_TOKEN` Administration: Read | PAT permissions are not editable through the API |
| Merge the GitOps image PR and the flag PR | Production-deploy class action |
| Methods-expert review of the GOO-320 accounting against PRISMA 2020 | Independent human judgment; no substitute identity |
| Move GOO-319/320 to Done | Acceptance includes the rag-dev journey and the review |
