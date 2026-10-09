# Academic R0/R1 Closure Plan (GOO-290..295)

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Move GOO-290, 291, 292, 293, 294, 295 from In Review to Done by producing the missing acceptance evidence and the two small code gaps that remain.

**Architecture:** Every ticket already has merged implementation on `develop` (head `ad6f47b16`). No feature work is left. What remains is (a) two backend integration tests, (b) triage of 30 Codex review threads, (c) live/authenticated evidence that is currently blocked because no dev backend origin is reachable, and (d) Linear bookkeeping.

**Tech Stack:** FastAPI + SQLAlchemy async + PostgreSQL integration tests (`backend/tests/integration`, marker `integration`), Alembic, `evals/academic-writing-baseline-v1/collect.py`, kubectl against `rag-dev`, Linear MCP, `gh`.

---

## Status audit (2026-09-29)

| Ticket | Merged PRs | Code left | Evidence left |
|---|---|---|---|
| GOO-290 provenance | #1706 | none | PG proof run at head + CI job URL; authenticated export download + hash recompute |
| GOO-291 BibTeX/Markdown | #1706, #1721 | none | authenticated Markdown + LaTeX download pair with checksums |
| GOO-292 claim coverage | #1706, #1730 (GOO-328) | none | PG rejected-candidate proof; authenticated findings reload; 11-doc batch |
| GOO-293 writing baseline | #1721 | GOO-339 triage (30 threads) | 14-trial real-provider + judge run; per-trial artifacts |
| GOO-294 project bridge | #1708, #1710, #1714, #1731 (GOO-327) | none | backfill dry-run report on dev DB; zero unresolved; live journey + denial checks |
| GOO-295 decision ledger | #1710 | cleanup-retention test; blinded-read test | live authorization acceptance |

Related bug ticket GOO-321 (post-merge regressions): items 3 and 5 fixed by #1730 and #1731; items 1, 2, 4 were closed already-fixed as GOO-323/324/325. Nothing further from it blocks closure here.

**Hard blocker for every "authenticated"/"live" row:** `dev-api.gen-text.app` and `api.goodwiinz.tech` do not resolve, and `goodwiinz.tech/api/v1/health` returns 404. Task 3 must land before Tasks 4–8.

---

### Task 1: GOO-295 — prove decision rows survive agent-run cleanup

**Files:**
- Modify: `backend/tests/integration/test_research_decision_ledger.py` (append new test after line 241)

Existing `_seed()` and `_approved_kwargs()` helpers in that file already create a collection, user and stream. `agent_runs.project_id` has `ondelete="SET NULL"` to `collections.id` (`backend/src/models/agent_run.py:78`), and the ledger tables use `RESTRICT` (`backend/src/models/research_decision.py:30,58,62,68`). The test inserts an `agent_runs` row for the same collection, deletes it the way cleanup does, and asserts the ledger is untouched.

**Step 1: Write the failing test**

```python
@pytest.mark.asyncio
async def test_decisions_survive_agent_run_cleanup(
    decision_engine: AsyncEngine,
) -> None:
    ids = await _seed(decision_engine)
    kwargs = _approved_kwargs(ids, uuid4(), uuid4())
    async with AsyncSession(decision_engine, expire_on_commit=False) as db:
        appended = await append_decision(db, **kwargs)
        await db.commit()

    run_id = uuid4()
    async with decision_engine.begin() as connection:
        await connection.execute(
            text(
                """INSERT INTO agent_runs
                   (id, user_id, project_id, status, created_at, updated_at)
                   VALUES (:id, :user, :collection, 'completed', now(), now())"""
            ),
            {"id": run_id, **ids},
        )
    # disposable agent-run cleanup: hard delete
    async with decision_engine.begin() as connection:
        await connection.execute(
            text("DELETE FROM agent_runs WHERE id=:id"), {"id": run_id}
        )
    async with decision_engine.connect() as connection:
        surviving = await connection.scalar(
            text("SELECT count(*) FROM research_decision_events WHERE id=:id"),
            {"id": appended.event.id},
        )
        assert surviving == 1
        assert (
            await connection.scalar(
                text("SELECT count(*) FROM agent_runs WHERE id=:id"), {"id": run_id}
            )
        ) == 0
```

If the `agent_runs` INSERT fails on NOT NULL columns, read `backend/src/models/agent_run.py` and add the missing required columns to the VALUES clause; do not relax the model.

**Step 2: Run it**

```
cd backend && ../backend/.venv/bin/pytest -q tests/integration/test_research_decision_ledger.py -k survive_agent_run_cleanup
```
Expected: PASS if `_seed` keys match (`user`, `collection`). If it FAILS on a schema detail, fix the INSERT only. Requires local PostgreSQL per `backend/tests/integration/conftest.py`; if no DB, report `NOT RUN` and push to CI.

**Step 3: Commit**

```
git add backend/tests/integration/test_research_decision_ledger.py
git commit -m "test(research): prove decision ledger survives agent-run cleanup (GOO-295)"
```

---

### Task 2: GOO-295 — blinded/public read test on protocol routes

**Files:**
- Read first: `backend/tests/integration/test_protocol_governance.py`, `backend/src/api/research_engine/protocols.py:292-330` (`_authorized_protocol`, `get_protocol`)
- Modify: `backend/tests/integration/test_protocol_governance.py`

**Step 1:** Grep the existing file for `public` and `viewer`. If a test already asserts that a public-workspace viewer and a foreign-org user cannot read pending approvals/decision events, skip this task and cite the test name in the Linear comment.

**Step 2:** Otherwise add one test using the file's existing fixtures: make the workspace public, call `GET /api/v1/research-engine/protocols/{id}` as (a) an anonymous-org viewer and (b) a foreign-org user. Assert 404 for the foreign org, and for the public viewer assert the response contains no `approvals`, `decision_events` or supervisor identity fields (check the response model in `protocols.py:93-170` for the exact field names).

**Step 3:** Run `pytest -q backend/tests/integration/test_protocol_governance.py -k public` → PASS. Commit:

```
git commit -m "test(research): public and foreign readers cannot see protocol approvals (GOO-295)"
```

**Step 4:** Open PR from a fresh branch off `origin/develop` (`test/goo-295-retention-blinding`). Wait for the Integration Tests job; record its job URL. That URL is closure evidence for GOO-295's two unchecked boxes.

---

### Task 3: Restore a reachable dev backend (gate for Tasks 4–8)

Ops, not code. Use the `claude-remote-workflow` skill discipline.

**Step 1:** `kubectl -n rag-dev get ingress,svc,pods` and `kubectl -n rag-dev describe ingress`. Find which hostname the ingress actually serves.

**Step 2:** `argocd app get rag-dev` (or ArgoCD UI, see memory `reference_argocd_access`). Check sync/health.

**Step 3:** If the ingress host is fine but DNS is gone, fix the DNS record in the DO web console (memory: `feedback_do_dns_doctl`). If the ingress is missing, it is a GitOps change to `deployment/` on `develop`, never `kubectl patch`.

**Step 4:** Confirm `curl -s https://<host>/health` → 200 and note the deployed image SHA from `values-dev.yaml`. Record host and SHA; every later evidence comment must cite this SHA.

**Step 5:** Confirm Alembic head on the deployed DB is ≥ `b4d6f8021a3c` (`kubectl exec` into backend pod, `alembic current`). If not, deployment is stale; open the release-dev GitOps PR first.

---

### Task 4: GOO-294 — backfill dry-run and resolution on dev DB

**Files:** `backend/scripts/maintenance/backfill_research_project_collections.py` (dry-run by default; links only when UUIDs are identical, never by name).

**Step 1:** From the backend pod:
```
python -m scripts.maintenance.backfill_research_project_collections > /tmp/backfill-dry-run.json
```
Copy out with `kubectl cp`. Save as `docs/audits/2026-09-29-goo-294-backfill-dry-run.json`.

**Step 2:** Read `unresolved` and `conflicting` categories. For each, decide by inspecting workspace/org rows; do NOT merge by name. If any is a real user project, resolve via the authorized mapping endpoint, not SQL.

**Step 3:** `... --apply` then `... --require-resolved` → exit 0. Save both outputs alongside the dry-run file. Commit the audit files.

**Step 4:** Authenticated journey (the dev owner account; login in memory `feedback_app_login`) through Chrome: open one project → sources → run → matrix → draft; then hit a foreign project id, an archived project and a soft-deleted one on `/research-engine/projects/{id}`, `/blueprints`, `/runs`, `/steps`. Record the four status codes. Screenshot the single-project view (no duplicate cards).

**Step 5:** Comment on GOO-294 with SHA, backfill counts, denial codes, screenshot; move to Done.

---

### Task 5: GOO-290 — provenance proof at head + authenticated export

**Step 1:** Trigger CI on `develop` head (or use the Integration Tests job from Task 2's PR). Capture the job URL and confirm `backend/tests/integration/test_research_step_provenance.py` shows as passed, not skipped, in the log.

**Step 2:** Against the live backend: create an approved protocol (needs a second user as supervisor per `run_conformance.py:103-137`), start a run, let one step complete, download the JSON export. Save it under `docs/audits/2026-09-29-goo-290-export.json`.

**Step 3:** Recompute: `sha256(step.input)` and `sha256(step.output)` from the export must equal `input_hash`/`output_hash`; `effective_model`, `temperature` present; `seed` null for the Azure provider; `protocol_version_id` and `effective_plan_hash` present in the completion manifest. One short Python snippet in the audit file.

**Step 4:** Comment on GOO-290 with run id, step ids, artifact SHA-256, CI job URL; move to Done.

---

### Task 6: GOO-291 — authenticated Markdown + LaTeX download pair

**Step 1:** Live: create a draft in a project with ≥3 seeded citations including one with Unicode/braces in the title and one document with no DOI. Download `?format=markdown&bib_format=apa` and `?format=latex` (ZIP).

**Step 2:** `shasum -a 256` both. Parse the `.bib` with `bibtexparser` (already in backend deps) and assert title/authors/year/venue/DOI match the seeded rows; assert Markdown References contain no evidence snippet text and every `[Doc N]` marker has a References entry.

**Step 3:** Also download as a collaborator user (200) and as a foreign-org user (404). Save checksums and codes in `docs/audits/2026-09-29-goo-291-downloads.md`.

**Step 4:** Comment on GOO-291; move to Done.

---

### Task 7: GOO-292 — rejected-candidate proof and 11-document batch

**Step 1:** CI job URL from Task 5 covering `backend/tests/integration/test_draft_review*.py` (grep `git ls-tree origin/develop backend/tests/integration | grep -i review` for the exact name); confirm non-skipped.

**Step 2:** Live: project with 11 distinct cited documents; generate draft; fetch persisted review. Assert `coverage.requested == coverage.checked + unresolved`, batches listed, `factual_classification_complete=false`, uncited assertions present as observations (not vetoes, per #1730), publication fields `unknown`.

**Step 3:** Submit a revision with a fabricated DOI; confirm 409/blocked, prior current draft unchanged (compare content hash before/after), candidate findings retrievable.

**Step 4:** Record ids, hashes, coverage JSON in `docs/audits/2026-09-29-goo-292-review.md`; comment; move to Done.

---

### Task 8: GOO-293 — run the 14-trial baseline with real provider and judge

**Prereq:** GOO-339 triage (Task 9) done first, because the collector's redaction/binding findings decide whether the produced artifacts are trustworthy.

**Step 1:** Read `evals/academic-writing-baseline-v1/README.md` and `protocol.json`. Export provider/judge credentials from Infisical (`/do-kb` path per memory `project_infisical_paths`; judge = gpt-4.1 on Azure per memory `project_langsmith_evaluators`).

**Step 2:** Run `collect.py` per README against the Task 3 host. Results land in the gitignored output dir; copy the summary and per-trial evidence to `evals/baselines/academic-writing-2026-09-29.json`. Do not touch `evals/baselines/agent-flow-2026-08-08-writing-kb.json`.

**Step 3:** Independent adjudication: a second judge pass or a human pass on the 5 canonical trials; record adjudicator identity and disagreement count.

**Step 4:** Commit baseline + `docs/testing/2026-09-29-academic-writing-baseline.md` stating denominators, pass criteria, separated objective/semantic/infra/judge-unavailable outcomes. Comment on GOO-293; move to Done.

---

### Task 9: GOO-339 — triage the 30 open Codex threads on #1721

**Step 1:** `gh api repos/Goodwiinz/NOUS-Research-Assistant/pulls/1721/comments --paginate` → list unresolved thread ids and bodies.

**Step 2:** For each, check against `evals/academic-writing-baseline-v1/collect.py` at `origin/develop`. Classify: fixed / real / over-reach. Priority for "real": secret scanning of scalar and tool-version fields, redaction of free-form failure reasons, binding citations/revisions/LaTeX to the retained draft id+hash.

**Step 3:** One branch, one PR, all real fixes together with tests in `evals/academic-writing-baseline-v1/tests/test_collect.py`. Resolve fixed threads; reply "wontfix: <reason>" to over-reach ones. Do not push per-finding.

**Step 4:** Comment the classification table on GOO-339; move to Done.

---

### Task 10: Linear bookkeeping

For each ticket: attach the audit file path, CI job URL, deployed SHA; tick the remaining acceptance boxes in the description; set status Done. GOO-293 stays In Review until Task 8 finishes. Don't restate merged evidence already in the descriptions.

## Amendment (2026-10-09)

Added when this plan was committed (PR #1958). The tasks above are kept as
written; these notes correct them and point to the current code.

- Task 4, Step 4: the account's personal email address was replaced with a
  role label before publication.
- Task 1: `agent_runs` has no `id` column. Its primary key is the string
  `job_id` (`backend/src/models/agent_run.py:61`), so the test SQL must insert,
  delete and query `job_id`.
- Task 7, Step 1: `git ls-tree` lists only the top-level entry without `-r`.
  Use `git ls-tree -r --name-only origin/develop -- backend/tests/integration | grep -i review`.
- Task 9, Step 1: `GET pulls/{n}/comments` returns individual review comments,
  not threads or their resolved state. List unresolved threads through
  GraphQL (`reviewThreads { nodes { id isResolved } }`), as
  `docs/superpowers/plans/2026-08-27-nous-plan-2b-github-reconciliation.md`
  does for `unresolved_review_threads`.
