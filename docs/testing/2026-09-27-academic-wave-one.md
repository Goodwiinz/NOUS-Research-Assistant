# Academic workflow: first implementation wave

Recorded 2026-09-27. Status: locally validated implementation for review;
not deployed or accepted in a live environment.

Source: [checked roadmap](https://linear.app/goodwiinz/document/f53041f5-5409-447f-a3a9-8a82afdb88a7).
Branch: `codex/academic-wave-one`, based on `develop` at
`eada98ec97daeaec02121ab1ad913e421579e62b`. Three GPT-5.6 Sol workers
implemented and reviewed the four independent starters. GOO-293's writing
baseline and subsequent roadmap rounds are outside this wave.

## Changes and evidence

| Ticket | Implemented behavior | Durable evidence |
| --- | --- | --- |
| [GOO-290](https://linear.app/goodwiinz/issue/GOO-290) | Executor hashes, effective model/configuration identity and prompt inputs survive events, persistence and JSON export. Claude seed is unknown because its provider does not send a seed. | [Provenance integration test](../../backend/tests/integration/test_research_step_provenance.py) uses the real executor/event/row conversion, commits, reopens PostgreSQL, exports and recomputes hashes. |
| [GOO-291](https://linear.app/goodwiinz/issue/GOO-291) | Draft bibliographies reuse canonical citation/document metadata and stable `docN` keys. Missing metadata is not replaced with evidence quotes. | [Downloaded artifact integration test](../../backend/tests/integration/test_draft_bibliography_artifact.py) reads saved draft relationships through the download route, parses the ZIP's BibTeX and reconciles manuscript keys. |
| [GOO-292](https://linear.app/goodwiinz/issue/GOO-292) | Reviews expose potential uncited assertions, minor findings, independent check dimensions and limits. Rejected candidates retain review records without replacing the current draft. The draft UI retrieves and displays these findings after failure. | [Revision persistence integration test](../../backend/tests/integration/test_draft_review_persistence.py) reopens PostgreSQL to verify the rejected candidate's findings and unchanged current draft. [Verifier tests](../../backend/tests/unit/services/test_citation_verification_service.py) cover batching, invalid identifiers, source conflicts, late evidence, truncation and outages. |
| [GOO-294](https://linear.app/goodwiinz/issue/GOO-294) | Optional unique Collection mapping preserves engine UUIDs. Shared access checks cover projects, blueprints, runs and steps, including deleted ancestors. One-time linking serializes concurrent requests. | [Mapping integration test](../../backend/tests/integration/test_research_project_mapping.py) covers roles, organization boundaries, ownership transfer, public/deleted ancestry and concurrency. [Migration integration test](../../backend/tests/integration/test_academic_wave_migrations.py) covers constrained backfill, upgrade/downgrade and database constraints. |

Mapped research access requires explicit workspace membership within the
workspace organization, or current workspace ownership. Public visibility alone
does not grant research access. Review requires editor/admin/owner; adjudication
requires admin/owner; run supervision requires owner. Legacy unlinked projects
retain owner access. Source document organization boundaries remain in force.

The claim classifier is a conservative prose heuristic and explicitly reports
incomplete factual classification. No authoritative publication-status provider
is implemented: correction/retraction metadata is displayed as an observation,
while publication status stays unknown, unavailable and unperformed. Empty or
invalid-only citation coverage cannot become fully verified.

## Local validation

Environment: Python 3.11.13, Node 24.19.0, pnpm 10.18.2, isolated PostgreSQL 14.
The hosted integration environment's PostgreSQL 16 was not reproduced locally.
Quality checks used the CI pins: Ruff 0.15.15, Black 26.5.1, isort 5.13.2 and
mypy 1.7.1. Commands follow the [testing contract](../engineering/testing.md).

- Combined architecture/API/thread and changed-service pytest matrix:
  **1,148 passed**, no skips, plus three existing non-strict Ollama XPASSes.
- Five new PostgreSQL integration tests: **5 passed**. Each uses an explicitly
  supplied scratch database and isolated schema.
- Full frontend Vitest: **314 files, 2,348 tests passed**.
- Frontend production build: passed. Optional Codecov bundle analysis was
  skipped because its token was not configured.
- Frontend TypeScript: passed. Changed-file ESLint and type-exclusion ratchets:
  passed. Full-tree advisory ESLint still reports baseline debt (116 errors,
  2,003 warnings); this is not a clean full-tree lint claim.
- `scripts/ci/run_local_ci.sh --skip-tests`: passed, including changed-file
  Ruff/Black/isort, added-file mypy, directory-doc checks, regenerated OpenAPI
  and TypeScript consistency, Alembic graph validation, targeted evidence
  migration probe, and migration replay from an empty database.
- Full-tree backend Black and isort: passed (555 source files).
- After consolidating the shared project-access predicate, the PostgreSQL
  mapping test and five research-engine API tests passed again.
- API comparison adds two routes, removes none, and adds no required fields to
  existing project create/response contracts. The hosted compatibility checker
  remains a separate CI gate.
- Mapping guard mutation: temporarily removing the row lock caused the
  concurrent-link assertion to fail with two successful requests instead of
  one success and one conflict. Restoring the lock passed. The reproduction
  and assertion are recorded adjacent to the integration test.

The backend matrix was run with `PYTHONPATH=backend`, `--no-cov`, and a scratch
`TEST_PG_ADMIN_DSN`, against these paths:

```text
backend/tests/unit/architecture
backend/tests/unit/api
backend/tests/unit/services/threads
backend/tests/api/threads
backend/tests/unit/services/test_bibliography_service.py
backend/tests/unit/services/test_draft_bibliography_export.py
backend/tests/unit/services/test_citation_verification_service.py
backend/tests/unit/services/test_draft_citation_review_pass.py
backend/tests/unit/services/test_draft_revision_service.py
backend/tests/unit/services/test_workflow_engine.py
backend/tests/unit/services/test_export_service.py
backend/tests/unit/services/test_llm_providers.py
```

The PostgreSQL integration test files linked above use
`RESEARCH_PROVENANCE_DATABASE_URL`, `BIBLIOGRAPHY_DATABASE_URL`,
`DRAFT_REVIEW_DATABASE_URL`, `RESEARCH_PROJECT_DATABASE_URL` and
`ACADEMIC_MIGRATION_DATABASE_URL`. Use disposable databases only.

## Remaining acceptance boundaries

Authenticated browser acceptance, real provider/semantic quality evaluation,
independent methods review, hosted CI and deployment are not established by
these local tests. Linear tickets have not been closed. The first-wave code
provides the prerequisites for the separately scoped writing baseline; it does
not claim that baseline or later verified-release workflows are complete.

## CI follow-up: background-draft transaction assertions

The [Unit Tests producer job](https://github.com/Goodwiinz/NOUS-Research-Assistant/actions/runs/36342464599/job/108685329834)
at `797b458edf9876188d872446570283780292ea20` failed three tests in
`test_draft_bg_session.py`: they still expected one commit after the new durable
review introduced two. The Release Gate failed downstream; the run had 5,826
passing tests, 80 skips and three pre-existing non-strict XPASSes.

The corrected tests verify two awaited commits and the actual write order:
`DraftReview` is committed before adding `GeneratedDraft` and `DraftCitation`,
which share the second commit. Constructor-session isolation, the closed
document-fetch session during generation, and empty-input no-write assertions
remain intact. No production behavior or CI threshold changed.

Focused reproduction: three failures and four passes before the correction;
seven passes afterward. CI-pinned Ruff, Black, isort, directory-doc checks,
OpenAPI consistency and `git diff --check` passed. The workflow contract suite
also passed all 161 tests.

The full CI unit selection, invoked through `pytest` with two xdist workers,
passed locally: **5,829 passed, 80 skipped, three existing non-strict XPASSes**.
The 35% coverage threshold and all 150 measured per-file coverage floors passed.
Selection: `backend/tests/ -c backend/pytest.ini -m "unit or not (integration or
e2e or slow)"`. Hosted confirmation remains separate from this local result.
