# Linear open issues: implementation and verification progress

Date: 2026-10-16. Status: active; this record is not a completion claim.

Scope: GOO-219, GOO-237, GOO-238, GOO-239, GOO-240, GOO-241, GOO-435,
GOO-436, GOO-419, GOO-337, GOO-319 and GOO-320. The user requested review,
implementation, double-checking with Claude Fable, and overnight work.

Worktree: `/Users/goodwiinz/.codex/worktrees/7b9e/RAG_system`.
Branch: `codex/goo-219-linear-open-issues`.
Freshly fetched base: `6928d3d90a8d7f9e419015c1511eae213ec968ad`.
The starting checkout was stale; the implementation branch starts at that
fetched base. No pre-existing source changes were overwritten.

## Current workstreams

| Issues | Current evidence | Remaining work |
| --- | --- | --- |
| GOO-219, GOO-237–241 | Endpoint implementation and regression tests written. Nine initial causal failures reproduced; focused routing/connectivity tests pass. Full-suite checking identified the existing local-proxy contract, which is retained inside the shared resolver. | Final full suite, changed-file gates, independent final-diff/security review, local browser login verification, reviewed delivery. |
| GOO-435 | Discovery, embeddings migration, semantic primitive, ingest/backfill and tool registration exist on the base. Initial Fable review located mutation-annotated unit and PostgreSQL tests. | Run owning suites; inspect and run the existing `eval/plan07-s5-harness-discovery` evaluation work. Provider, Docker and pgvector prerequisites must be verified. No live flags or backfill have been changed. |
| GOO-436 | Page splitter, short-document skip, bounded reranking, fallback, access recheck and additive result metadata exist. Current focused source tests: **40 passed**, 310 deselected. | Remaining owning tests and live Cohere/flag acceptance. Unit results do not prove provider or deployed behavior. |
| GOO-419 | Initial Fable review identifies DV-3, AD-2 and HO-4 as remaining source findings. Existing clean worktree `.worktrees/pr-i`, branch `fix/audit-lows-dv3-ho3-ad2`, has 11 unmerged commits ending at `f3349be91`. Its work must be reviewed and reused rather than overwritten. | Verify current ownership and issue criteria; assess existing implementation, tests and conflicts. HO-4 requires its recorded design decision to be resolved. |
| GOO-337 | Retained verification record exists; initial Fable review found its source-default and lifecycle-fix statements stale. | Verify merge/producer evidence, add a dated evidence amendment, run current lifecycle checks, and preserve owner/live/model/rollback gates as unverified until directly demonstrated. |
| GOO-319, GOO-320 | Services, APIs, models, UI and retained real-worker/provider evidence exist. | Current focused and PostgreSQL verification; deployed authenticated journey and independent methods-review requirements remain separate gates. |

## Execution and ownership

- Same-host shared board: `/Users/goodwiinz/.loop-bridge`, verified writable;
  its initial listing had no claims. The endpoint claim uses agent
  `codex-01a1435c`, area `frontend-endpoints-GOO-219-237-241`, and the branch
  above. Renew it before expiry; do not label it merged without a verified merge.
- Overnight hourly heartbeat: `nous-overnight-linear-issues`, through 08:00
  America/New_York on 2026-10-16, explicitly approved by the user. Stop it at
  the cutoff or verified completion; preserve the full goal if work remains.
- Temporary raw logs, briefs and Fable artifacts: `/tmp/nous-linear-review/`.
  Do not commit raw traces, credentials, session state or complete tool logs.
- Fable initial review: session `ce5453a1-12d1-4684-8e01-5928d0c88ab5`, model
  `claude-fable-5-1`. The report is extracted in
  `/tmp/nous-linear-review/fable-initial-report.md`. This is an initial source
  review, not approval of the final diff. Its read-only tripwire observed this
  orchestrator's concurrent changes; do not attribute those changes to Fable.
- Node 24.21.0 and pnpm 10.18.2 were used; frozen-lockfile install succeeded.
  TypeScript passed before the last source refinements and must be rerun.
- Backend focused verification currently uses the existing main-checkout
  `backend/.venv` (Python 3.11.13, FastAPI 0.138.1, Pydantic 2.13.4,
  SQLAlchemy 2.0.51). These versions differ from current CI requirements;
  report that boundary. Pinned Ruff 0.15.15, Black 26.5.1 and isort 5.13.2
  are available in `/tmp/nous-linear-review/venv` for static gates.

## Acceptance boundaries

No deployment, live feature enablement, embedding backfill, merge, external
message or manual Linear status change has been performed. NOUS QA credential
variables are present; public Supabase and Azure/Cohere configuration is absent
from the shell. Check the established runtime configuration path without
printing secret values. Docker's daemon was not running at initial inspection.
These observations are prerequisites, not test outcomes. Local source, unit,
fixture, PostgreSQL, provider, browser, hosted CI and deployed acceptance must
remain separately attributed. Do not close the full goal from partial coverage.

## Endpoint delivery evidence update

The endpoint implementation is ready for reviewed delivery. Fresh base
`4c4600392` contains only an additional GitOps image pin relative to the first
base, with no overlapping source change. Both final Fable contexts found no
source defects and their read-only tripwires were false. The full frontend
suite passed 3,039 tests; every blocking local gate passed. Both WebSocket
guards were mutation-verified with byte-identical restoration. Three production
type exclusions were removed, analytics barrel exports repaired, and two
unreferenced legacy graph clients retired. The [endpoint evidence](../testing/evidence/linear-endpoints-20261016/README.md)
records commands and limits.

Browser login passed two assertions on the local frontend against the live dev
API. Both screenshots were opened and confirmed against the login criteria.
This remains mixed-target evidence with unknown backend SHA. The owned local
frontend server was stopped; its generated `next-env.d.ts` change was restored.

Other current checks: the complete read-tools unit module passed 350 tests
(including the earlier 40 page-filter cases); the discovery/search-update/
review-version/task unit matrix passed 180; and five PostgreSQL modules passed
all 30 tests on the isolated container `nous-linear-review-pg` at local port
55491, image `pgvector/pgvector:0.8.7-pg15`. These are separate from live providers
and deployed acceptance. The container remains available for subsequent work.

The established Infisical workflow is authenticated. A real Cohere embedding
call returned provider `cohere`, model `embed-v-4-0`, dimension 1024. The declared
Azure Codex evaluation deployment returned HTTP 200. The direct Anthropic key
is still a placeholder; AWS authentication is available, so a Fable Bedrock
route must be verified before any Claude benchmark trial. The existing
evaluation worktree is `.worktrees/plan07-s5-eval` on
`eval/plan07-s5-harness-discovery`, head `ed9ecd490`; bridge/backend/catalog
inputs are unchanged from its declared source `57fc21bc7`. Harbor 0.6.6 is
installed. All eight verifier calibration fixtures returned their expected
pass/objective-failure/infrastructure codes. No scored trial has run yet.

The full 12-issue goal remains active. Remaining work includes reviewed PR
delivery/hosted checks, the discovery decision evaluation and labelled recall
measurement, the existing PR I audit fixes and HO-4 proposal, and refreshed
Daily Brief/living-review acceptance evidence and their owner-dependent gates.

## Subsequent hosted and audit checks

Endpoint PR [#2009](https://github.com/Goodwiinz/NOUS-Research-Assistant/pull/2009)
is draft. Its first head passed all 3,039 hosted frontend tests but failed the
64.75% function coverage floor at 64.26%; Release Gate was a downstream failure.
Meaningful analytics caller tests now pass 3,056 cases with coverage and all
coverage floors unchanged (functions 64.80%). Fable reviewed that test delta
without findings or observed writes. The next head requires fresh hosted CI.

The existing PR I commits were reused on `codex/goo-430-audit-fixes` in managed
worktree `linear-audit-fixes/RAG_system`. Two cherry-pick conflicts retained
current CLI commands and discovery timeout documentation. The remaining
HO-3 route now uses the lean access check and returns 200 null for an authorized
chat without a handoff, preserving 404 for an inaccessible chat. Contracts were
regenerated together. Current checks: 209 backend tests, 20 affected frontend
tests, 314 bridge tests, all blocking local gates; the first-save retry was
mutation-verified with exact restoration. Separate Fable code/security reviews
both found a relinked-project handoff visibility gap; that finding remains to
be resolved and reviewed before delivery.

Five initial Codex discovery attempts encountered Azure WebSocket HTTP 302
errors and recovered over HTTPS. They are excluded under the registered
provider-error rule, with no decision claimed. Five replacement attempts use
an explicitly HTTP Azure provider and a new task digest. The preparation
command materializes the declared historical bridge revision; all 92 source
inputs were checked. Fable Bedrock invocation remains denied, so benchmark
evidence can only be Codex-only. Local OAuth Fable code reviews are separate.

The tenant-scoped handoff uniqueness proposal has its own managed worktree,
`linear-handoff-org/RAG_system`, branch `codex/goo-423-handoff-org-scope`.
Implementation, migration execution, review and owner acceptance are pending.
The full twelve-issue goal remains active.
