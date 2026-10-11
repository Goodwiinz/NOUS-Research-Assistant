# Artifact workspace completion verification — 2026-10-11

**Status:** implementation and local verification complete. Independent review
has no open consequential findings. Live acceptance remains **NOT RUN**. This
is a dated evidence record, not a deployment or live availability claim.

Source examined: `codex/linear-completion-20261011`, worktree
`/home/clawdbot/rag-clean/.worktrees/linear-completion-20261011`, based on
`ac693e63876b2b751712ae3f50926502e158e16c`. Frozen implementation revision:
`f6b1b45fa0f7b0eb0dacfb95d1163e6d1df1f6db`. Subsequent commits update evidence and the plan.
The review deliverable is [PR #1973](https://github.com/Goodwiinz/NOUS-Research-Assistant/pull/1973)
against `develop`; it remains unmerged.

The [October 11 implementation plan](../superpowers/plans/2026-10-11-artifact-workspace-completion.md)
defines this sharing/native-completion slice. The earlier
[October 9 artifact UI record](artifact-ui-plan03-verification-2026-10-09.md)
remains intact: its migration-free scope and recorded counts are historical
evidence, not proof of the new sharing route. Current rules remain in the
[frontend](../engineering/frontend.md), [API](../engineering/api-contracts.md)
and [testing](../engineering/testing.md) contracts.

## Environment and boundaries

Verified tools: Node **24.21.0**, pnpm **10.18.2**, test Python **3.13.12**;
the structural rerun and pinned lint environment use Python **3.11.14**.
Quality tools are Ruff **0.15.15**, Black **26.5.1**, isort **5.13.2** and
mypy **1.7.1**. Added-file typing uses repository-minimum SQLAlchemy **2.0.52**.
Node used the isolated official binary at `/tmp/rag-completion-node24/bin/node`;
Python used `/tmp/nous-qa-fix-venv/bin/python`. Dependencies were installed in
this worktree with `pnpm install --frozen-lockfile`; no dependency-tree symlink
or lockfile change was introduced by the frontend work.

Frontend tests use Vitest and the repository's React/Query test utilities.
Sharing service tests use the existing publication database fixture and memory
blob storage; route tests exercise FastAPI transport with test dependencies.
The Chromium proof uses production React components with explicit local auth,
context and HTTP transport fixtures. These checks are separate from a live
signed-in browser/backend journey.
Local `/tmp` log links below are ephemeral session artifacts, not committed
test fixtures.

## Sharing behavior covered

- An explicit 1–100-user audience and future timezone-aware expiry no later
  than seven days create a link for the selected immutable version. Editing
  later leaves the old share pinned. Invalid, foreign, inactive, deleted or
  duplicate recipients are rejected by the service.
- The creator is not silently added to the audience. A recipient can read the
  pinned metadata/bytes without obtaining project, source or provenance access.
  Shared metadata exposes only artifact/version IDs, title, MIME, size and hash.
- Browser-only writes and current owner authority are enforced. Creation needs
  both write flags; owner revocation, committed metadata, content and creator-list
  reads survive disabled write flags. Private
  no-store transport behavior, safe download names and generic denial errors
  are covered by the route tests.
- The [dialog](../../frontend/src/components/chat/artifact-panel/ArtifactShareDialog.tsx)
  validates UUIDs, deduplicates the submitted audience, labels the local expiry
  bounds, supports copy/revoke, uses stable errors, and restores trigger focus.
  Strict Mode cleanup, cancellation, stale list responses and account changes
  cannot repopulate another dialog or actor's cache.
- The [shared view](../../frontend/src/components/chat/artifact-panel/SharedArtifactView.tsx)
  reuses the existing bounded preview/sandbox renderers without source,
  provenance or editing controls. One actor/organization/share Query owns its
  metadata and bytes. Refetch unmounts private previews; denial replaces the
  cached data with `unavailable`; old account responses are aborted and ignored.
  The image teardown test observes object URL revocation.

The [sharing flow](../engineering/flows/artifact-version-sharing.md) documents
the actual UI states and registered live QA checkpoints. Feature-map `covered`
means the scenario is registered; live execution remains **NOT RUN**.

## Frontend commands and results

All commands ran from the worktree root with the isolated Node binary on PATH.

```sh
PATH=/tmp/rag-completion-node24/bin:$PATH pnpm --dir frontend test
PATH=/tmp/rag-completion-node24/bin:$PATH pnpm --dir frontend exec vitest run \
  src/components/chat/artifact-panel/__tests__/ArtifactShareDialog.test.tsx \
  src/components/chat/artifact-panel/__tests__/SharedArtifactView.test.tsx
PYTHON=/tmp/nous-qa-fix-venv/bin/python \
  PATH=/tmp/nous-qa-fix-venv/bin:/tmp/rag-completion-node24/bin:$PATH \
  scripts/ci/run_local_ci.sh --base origin/develop --skip-tests --frontend
git diff --check -- frontend
```

| Check | Observed result | Local evidence |
| --- | --- | --- |
| Final full frontend suite after frontend edits | **385 files / 2,997 tests passed**, exit 0 | [Final full suite](/tmp/rag-completion-frontend-full-final.log) |
| Focused dialog/shared-view suite | **2 files / 12 tests passed**, exit 0 | [Focused sharing suite](/tmp/rag-completion-sharing-final.log) |
| TypeScript in the joined wrapper | **PASS** (`tsc --noEmit`) | [Wrapper](/tmp/rag-completion-frontend-quality-venv.log) |
| Blocking frontend lint/exclusion ratchets | **PASS**, retained baselines; no new production exclusion | [Wrapper](/tmp/rag-completion-frontend-quality-venv.log) |
| Lint over all changed frontend source/test files | **PASS**, zero output | Session command output; [production-file lint log](/tmp/rag-completion-sharing-lint.log) |
| Frontend whitespace check | **PASS** | Session command output |

## Local Chromium sharing proof

The standalone [browser specification](../../frontend/e2e/artifacts/sharing.spec.ts)
bundles the actual `SharedArtifactView`, `ArtifactShareDialog`, sharing Query
hooks, artifact domain adapter and bounded preview components with esbuild's
production React build. It substitutes only auth/context stores and the HTTP
client boundary. Playwright routes return synthetic API-shaped metadata,
PDF/text bytes and create/revoke responses; a fixture header identifies the
synthetic actor. The fixture's fake audience/authorization decisions do not
verify the running backend's identity checks. Unrelated imported math CSS is
omitted; this proof exercises PDF and plain text, not math styling.

The [standalone configuration](../../frontend/playwright.artifact-sharing.config.ts)
uses **Chromium 149.0.7827.55**, one worker, zero retries and `bypassCSP: false`.
The fixture sends the complete production parent Content-Security-Policy and
serves the pinned PDF worker locally. No application server is started.

```sh
PATH=/tmp/rag-completion-node24/bin:$PATH pnpm --dir frontend exec playwright test \
  --config playwright.artifact-sharing.config.ts
PATH=/tmp/rag-completion-node24/bin:$PATH pnpm --dir frontend exec tsc --noEmit
PATH=/tmp/rag-completion-node24/bin:$PATH pnpm --dir frontend exec eslint \
  e2e/artifacts/sharing.spec.ts playwright.artifact-sharing.config.ts
```

Result: **4 passed** in 4.3 seconds, exit 0; [Chromium log](/tmp/rag-completion-sharing-browser.log).
Both new test/config files also passed TypeScript and ESLint with zero output:
[TypeScript](/tmp/rag-completion-sharing-browser-tsc.log),
[lint](/tmp/rag-completion-sharing-browser-lint.log).

| Browser behavior | Observed proof |
| --- | --- |
| Pinned preview and download | Real PDF canvas pixel is red; the browser download filename is `Saved v1.pdf` and its SHA-256 equals the fixture's immutable bytes. Both preview and download request the exact share content endpoint. |
| Revocation while open | During the pending metadata recheck, private text and title leave the DOM. A denied response shows the unavailable state and removes private metadata from Query data. |
| Audience, creation, copy and revocation | Invalid UUID input makes no creation request; duplicates deduplicate to the explicit recipient; the request targets the selected version with a future UTC expiry. The real clipboard receives the absolute link, and revocation removes it. |
| Keyboard and focus | Enter opens/submits/revokes; initial audience focus and Shift+Tab remain inside the dialog; Escape closes and restores trigger focus. |
| Account transition during byte loading | The old content request fails with `net::ERR_ABORTED`; a late release never displays the old title/bytes or repopulates private metadata in Query data. |

All four proofs observed zero page errors and zero unexpected network requests.
No screenshots or visual/product acceptance claim were made.

The intermediate frontend wrapper exited **1** overall. At that run its remaining blocking
failures were the then-unclaimed shared route, changed backend Black/isort, and
`pnpm check:api-types` detecting intended uncommitted generated files. Its
frontend gates, OpenAPI drift check, migration delta and Alembic empty-chain
measurement passed. These intermediate results must not be presented as a final
joined pass; the later joined run is recorded below. Full-tree
ESLint remains advisory and exited 1 with existing debt. No baseline was relaxed.

## Frontend mutation proof

Each guard below was temporarily removed during implementation. The named
tests failed for the described defect, then the original source was restored.
Mutation failures were observed in session output; no separate frontend mutant
log was retained. The focused green suite above and final full suite ran after
restoration. Line references below describe the reviewed implementation before
the final integration commit.

| Guard and mutation | Focused test | Observed failure |
| --- | --- | --- |
| [SharedArtifactView.tsx](../../frontend/src/components/chat/artifact-panel/SharedArtifactView.tsx):41; remove the `query.isFetching` gate | `drops private bytes and metadata while rechecking a revoked share` | Old private `<pre>` content remained visible during recheck. |
| [useArtifactSharing.ts](../../frontend/src/hooks/chat/useArtifactSharing.ts):55; throw on denied recheck instead of returning `unavailable` | Same test | Cached Query data still contained the private title after denial. |
| [ArtifactShareDialog.tsx](../../frontend/src/components/chat/artifact-panel/ArtifactShareDialog.tsx):141 and :143; remove the current actor/closed request checks before creating cache data | `does not show a cancelled request result in a reopened dialog` | Direct owner-list cache assertion received the late share instead of an empty list. |
| Same dialog :142; remove cancellation of an older list request before adding the new share | `keeps a newly created link when an older list request resolves` | The older list request's signal was not aborted. |

Exact focused reproduction commands for these guards (run each against a
temporary mutation, then restore source and run again):

```sh
PATH=/tmp/rag-completion-node24/bin:$PATH pnpm --dir frontend exec vitest run \
  src/components/chat/artifact-panel/__tests__/SharedArtifactView.test.tsx \
  -t 'drops private bytes'
PATH=/tmp/rag-completion-node24/bin:$PATH pnpm --dir frontend exec vitest run \
  src/components/chat/artifact-panel/__tests__/ArtifactShareDialog.test.tsx \
  -t 'does not show a cancelled request'
PATH=/tmp/rag-completion-node24/bin:$PATH pnpm --dir frontend exec vitest run \
  src/components/chat/artifact-panel/__tests__/ArtifactShareDialog.test.tsx \
  -t 'older list request'
```

All three exact commands were also rerun against the restored source while
writing this record: one selected test passed in each run. Retained green logs:
[viewer recheck](/tmp/rag-completion-sharing-mutation-restored-view.log),
[closed dialog](/tmp/rag-completion-sharing-mutation-restored-closed.log),
[older list](/tmp/rag-completion-sharing-mutation-restored-list.log).

The cancellation test was strengthened to inspect the persistent QueryClient
cache directly after the first mutation survived a UI-only assertion. Repeating
the mutation then failed that cache assertion; the restored implementation
passed.

## Backend sharing commands and results

The sharing implementer reported the command below; its log was checked for the
recorded result. This run preceded the controller's final native/integration
changes, so it does not replace the later joined artifact count.

```sh
/tmp/nous-qa-fix-venv/bin/python -m pytest -q \
  backend/tests/unit/services/artifacts/test_publication.py \
  backend/tests/unit/services/artifacts/test_editing.py \
  backend/tests/unit/services/artifacts/test_sharing.py \
  backend/tests/unit/api/test_artifact_sharing_api.py --tb=short
```

Result: **91 passed**, [sharing/publication/edit/API log](/tmp/artifact-sharing-final-tests.log).
An earlier focused new-sharing run recorded **50 passed** in
[the sharing log](/tmp/artifact-sharing-tests.log). Tests and fixtures are in
[test_sharing.py](../../backend/tests/unit/services/artifacts/test_sharing.py)
and [test_artifact_sharing_api.py](../../backend/tests/unit/api/test_artifact_sharing_api.py).

After the rollback correction, the final sharing/API command was:

```sh
/tmp/nous-qa-fix-venv/bin/python -m pytest -q \
  backend/tests/unit/services/artifacts/test_sharing.py \
  backend/tests/unit/api/test_artifact_sharing_api.py --tb=short
```

Result: **52 passed**, [rollback final log](/tmp/artifact-sharing-rollback-final.log).
Four new rollback checks first failed before the correction:
[initial rollback failures](/tmp/artifact-sharing-rollback-red.log).
Owner revocation reduces access with either write flag disabled; creation still
returns 503 and subsequent revoked metadata/content reads return 404. Browser
identity and current owner authorization remain required. Pinned mypy checks
for the service and both sharing test files passed:
[sharing typing log](/tmp/artifact-sharing-rollback-mypy.log).

Backend mutation proof removed the revocation and expiry clauses separately in
[sharing.py](../../backend/src/services/artifacts/sharing.py):168 and :169.
`test_revoked_share_denies_metadata_and_bytes` and
`test_expired_share_denies_metadata_and_bytes` each failed with
`DID NOT RAISE ArtifactNotFound`; exact source was restored. Retained logs:
[revocation mutant](/tmp/artifact-sharing-revocation-mutation.log),
[expiry mutant](/tmp/artifact-sharing-expiry-mutation.log).

```sh
/tmp/nous-qa-fix-venv/bin/python -m pytest -q \
  backend/tests/unit/services/artifacts/test_sharing.py::test_revoked_share_denies_metadata_and_bytes \
  --tb=short
/tmp/nous-qa-fix-venv/bin/python -m pytest -q \
  backend/tests/unit/services/artifacts/test_sharing.py::test_expired_share_denies_metadata_and_bytes \
  --tb=short
```

The post-storage authorization check in `sharing.py:216` was observed red/green
with `test_revocation_while_storage_loads_prevents_byte_release`:
[red](/tmp/artifact-sharing-storage-red.log),
[green](/tmp/artifact-sharing-storage-green.log). The source checks authorization
again after awaiting blob storage and validates byte size/hash before release.

```sh
/tmp/nous-qa-fix-venv/bin/python -m pytest -q \
  backend/tests/unit/services/artifacts/test_sharing.py \
  -k revocation_while_storage_loads_prevents_byte_release
```

The private/no-store denial-header test also failed before the route fix, then
passed in the 91-test run: [initial red](/tmp/artifact-sharing-error-header-red.log),
selected with `backend/tests/unit/api/test_artifact_sharing_api.py -k
inaccessible_share_errors_disclose_no_detail`. This is pre-fix behavioral proof,
not an additional mutation run.

## Final native, migration and joined evidence

| Check | Observed result | Evidence and boundary |
| --- | --- | --- |
| Native completion on final restored/formatted source | **17 passed** | Included in the final joined artifact command below. |
| Draft/tool regression suite | **81 passed** | [Regression log](/tmp/nous-completion-draft-regression.log) |
| Required backend matrix under Python 3.13 | **1,715 passed / 72 skipped / 1 failed** | [Matrix log](/tmp/nous-completion-backend-matrix.log); this run is not a complete green matrix. |
| Exact rerun of all 72 skipped thread cases | **72 passed**, zero skipped | [Thread PostgreSQL log](/tmp/nous-completion-thread-postgres.log); separate task-owned PostgreSQL 16 container, Python 3.13.12. |
| Python 3.11 structural rerun | **5 passed** | [Structural log](/tmp/nous-completion-structural-py311.log) |
| Initial isolated PostgreSQL completion/migration cases | **6 passed** | [Initial PostgreSQL log](/tmp/artifact-completion-postgres.log); superseded by the restored run below. |
| Native guard restoration run | **9 passed**, comprising **7 PostgreSQL + 2 SQLite unit cases**, after eight expected mutant failures | [Restored log](/tmp/artifact-completion-restored-final.log); mutation table below. |
| Final joined artifact suite with PostgreSQL configured | **159 passed**, no skips | [Joined artifact log](/tmp/nous-completion-artifact-joined-final.log); includes two existing terminal-ordering PostgreSQL cases. |
| Existing PostgreSQL retention cases | **5 passed** | [Retention log](/tmp/nous-native-retention.log); isolated-schema cleanup and refused-delete behavior. |
| Strict typing for every added Python file | **PASS**, nine files | [Typing log](/tmp/nous-completion-added-python-types-final.log); includes `--disallow-untyped-defs`. |
| Python workflow/script contracts | **170 passed** | [Workflow log](/tmp/nous-completion-workflow-contract.log); Python 3.11, no configured-provider replay. |
| Final Node QA/workflow contracts | **123 passed**, zero skipped | [Final controller log](/tmp/nous-completion-qa-contract-registered-final.log); includes the two final private-content/download checks added after the 121-case wrapper run. |
| Final joined local CI wrapper | **PASS**, exit 0 | [Post-registration wrapper](/tmp/nous-completion-local-ci-registered-final.log); tests run separately as listed here. Full-tree frontend lint remains advisory. |
| Independent branch and final rollback review | **No open consequential source findings** | Reviewer exercised 142 backend cases, repeated 17 native cases and ran ten final rollback/authority checks. |
| Directory documentation lint | **PASS**, 95 directory docs checked | `make docs-lint`; scope is README/doc.md, so new Markdown links were checked separately. |
| Feature map with the new flow | **PASS** | `python3 scripts/ci/check_feature_map.py --base origin/develop` |
| New-document relative/absolute links and whitespace | **PASS** | Every Markdown target resolved locally; `git diff --check -- docs/`. |

The sole Python 3.13 matrix failure was
`test_rerun_boundary.py::test_execute_code_tool_unchanged`. That structural test
records a Python 3.11 AST fingerprint. The original and
current `_tool_execute_code` AST matched; the isolated
required-version Python 3.11 rerun passed all five structural tests. The 72
initial skips came from the thread fixture's unavailable PostgreSQL endpoint;
all exact skipped node IDs were then rerun against an isolated PostgreSQL 16
container and passed, with no skipped cases. This closes that prerequisite
without changing the historical matrix result or claiming a single combined
matrix run. The existing lint/mypy environment was left intact.

The thread rerun used `postgres:16-alpine` in the task-owned
`nous-thread-verification-20261011` container, with a random port bound only to
loopback (observed `127.0.0.1:32779`). `TEST_PG_ASYNC_URL` bypassed the
[existing thread fixture](../../backend/tests/db/conftest.py)'s default
provisioning. Its `_bootstrap_schema_from_models` and
`_ensure_required_columns_and_indexes` helpers prepared only that disposable
database. Selection from the original matrix log produced the exact 54 service
and 18 API node IDs; it did not substitute a different test group.
The [exact provisioning/bootstrap/selection commands](/tmp/nous-completion-thread-postgres-evidence.md)
and [node list](/tmp/nous-completion-thread-skipped-nodeids.txt) are local session
evidence. The rerun examined `f54053bc21455ead8651172f9bb7d0e8b443c31d`, which
adds only documentation to the frozen implementation revision above. After
pytest exited, the container was stopped and removal was confirmed. Shared
`meridian_test` rows, public schemas, cluster roles and the pinned lint
environment were untouched. This ORM-schema fixture run does not establish
Alembic migration acceptance; the separate migration checks do that.

The new PostgreSQL cases are in
[test_artifact_completion_postgres.py](../../backend/tests/integration/test_artifact_completion_postgres.py).
They apply the actual `aw03` upgrade/downgrade/reupgrade on model-built
prerequisite tables in an isolated schema, together with concurrent enqueue,
worker claims, lease replacement and parent purge checks. This is the changed
migration proof; it is separate from the wrapper's empty historical-chain
measurement. Native unit tests are in
[test_native.py](../../backend/tests/unit/services/artifacts/test_native.py).

Commands below ran from the worktree root. PostgreSQL cases create and drop
only random test-owned schemas in the local `meridian_test` database.

```sh
ORCHESTRATION_TEST_DATABASE_URL=postgresql+asyncpg:///meridian_test \
  /tmp/nous-qa-fix-venv/bin/python -m pytest -q \
  backend/tests/unit/services/artifacts \
  backend/tests/unit/api/test_artifacts_api.py \
  backend/tests/unit/api/test_artifact_sharing_api.py --tb=short
ORCHESTRATION_TEST_DATABASE_URL=postgresql+asyncpg:///meridian_test \
  /tmp/nous-qa-fix-venv/bin/python -m pytest -q \
  backend/tests/integration/test_artifact_completion_postgres.py \
  backend/tests/unit/services/artifacts/test_native.py::test_lost_acknowledgement_replays_one_version \
  backend/tests/unit/services/artifacts/test_native.py::test_repeated_enqueue_creates_one_intent
ORCHESTRATION_TEST_DATABASE_URL=postgresql+asyncpg:///meridian_test \
  /tmp/nous-qa-fix-venv/bin/python -m pytest -q \
  backend/tests/integration/test_retention_thread_purge_postgres.py
/tmp/nous-qa-fix-venv/bin/python -m pytest -q \
  backend/tests/unit/services/test_agent_tools.py \
  backend/tests/unit/services/test_agent_create_draft_terminal.py \
  backend/tests/unit/services/test_draft_task_results.py \
  backend/tests/unit/services/test_draft_bg_session.py \
  backend/tests/unit/services/test_draft_docids_project_scope.py \
  backend/tests/unit/services/test_draft_status_store.py \
  backend/tests/unit/services/agent/test_tool_wrapper_contracts.py
/tmp/nous-qa-fix-venv/bin/python -m pytest -q \
  backend/tests/unit/architecture backend/tests/unit/api \
  backend/tests/unit/services/threads backend/tests/api/threads
/tmp/rag-remainder-lint-py311-20261009/bin/python -m pytest -q \
  backend/tests/unit/architecture/test_rerun_boundary.py \
  --confcutdir=backend/tests/unit/architecture --no-cov
/tmp/rag-remainder-lint-py311-20261009/bin/python -m pytest -q \
  tests/unit/scripts --confcutdir=tests/unit/scripts -p no:cacheprovider --no-cov
/tmp/rag-remainder-lint-py311-20261009/bin/mypy \
  --disallow-untyped-defs --ignore-missing-imports --follow-imports=silent \
  backend/alembic/versions/aw03_artifact_completion.py \
  backend/src/models/artifact_share.py \
  backend/src/models/native_artifact_intent.py \
  backend/src/services/artifacts/native.py \
  backend/src/services/artifacts/sharing.py \
  backend/tests/integration/test_artifact_completion_postgres.py \
  backend/tests/unit/api/test_artifact_sharing_api.py \
  backend/tests/unit/services/artifacts/test_native.py \
  backend/tests/unit/services/artifacts/test_sharing.py
```

### Native mutation proof

Eight deliberate guard mutations failed their targeted tests, each with exit 1.
Every guard was restored before the nine-case PostgreSQL/unit run and final
159-case artifact suite. The restored, formatted `native.py` has SHA-256
`f175129674ebf27a0a3bfcb4d5473ad7d19b46d88f14ab01109c3d398001e5b1`.
Source line references below describe that final file.

| Guard removed or changed in [native.py](../../backend/src/services/artifacts/native.py) | Selected test | Observed failure |
| --- | --- | --- |
| Draft `SELECT FOR UPDATE`, line 130 | `test_concurrent_enqueue_has_one_intent` | Unique draft constraint violation under concurrent enqueue. |
| `_eligible(now)` in conditional claim update, line 352 | `test_concurrent_drains_publish_once` | Duplicate workers incremented attempts to two. |
| Claim token in acknowledgement, line 397 | `test_expired_claim_cannot_acknowledge_new_worker` | Expired claimant overwrote replacement state. |
| Deterministic publication UUID, line 287 | `test_lost_acknowledgement_replays_one_version` | Replay created two versions. |
| Exhausted-intent row lock with `skip_locked`, line 201 | `test_final_attempt_publication_is_not_reconciled_while_locked` | Reconciliation read a receipt before final publication committed. |
| Existing-intent early return, line 139 | `test_repeated_enqueue_creates_one_intent` | Repeat enqueue violated draft uniqueness. |
| Expected token in publication guard, line 307 | `test_expired_claim_cannot_acknowledge_new_worker` | Expired claimant published under a replacement lease. |
| Local token/status check after claim reload, line 368 | `test_superseded_claim_cannot_adopt_reloaded_lease` | Superseded claimant adopted another worker's reloaded lease. |

For each PostgreSQL guard, the exact focused command is the same prefix below
with the corresponding table test name appended to the file as `::test_name`:

```sh
ORCHESTRATION_TEST_DATABASE_URL=postgresql+asyncpg:///meridian_test \
  /tmp/nous-qa-fix-venv/bin/python -m pytest -q \
  backend/tests/integration/test_artifact_completion_postgres.py::test_concurrent_enqueue_has_one_intent
```

Use the same invocation for `test_concurrent_drains_publish_once`,
`test_expired_claim_cannot_acknowledge_new_worker`,
`test_final_attempt_publication_is_not_reconciled_while_locked` and
`test_superseded_claim_cannot_adopt_reloaded_lease`. The two unit mutations use
the explicit unit node IDs in the nine-case restoration command above.
Temporary mutations are never retained in the feature branch.

Retained local mutant results: [first six guards](/tmp/artifact-native-completion-mutations.log),
[publication claim](/tmp/artifact-native-claim-mutation.log),
[reload lease](/tmp/artifact-native-reload-mutation.log).

The final wrapper command was:

```sh
PATH=/tmp/rag-remainder-lint-py311-20261009/bin:/tmp/rag-completion-node24/bin:$PATH \
  PYTHON=/tmp/nous-qa-fix-venv/bin/python \
  scripts/ci/run_local_ci.sh --base origin/develop --skip-tests --frontend
```

It exited **0**: full backend Ruff, changed-file Ruff/Black/isort, added-file
mypy, directory docs, feature map, OpenAPI drift, generated API types, the single
Alembic head, frontend TypeScript and blocking lint/exclusion ratchets passed.
The targeted evidence migration probe and advisory whole-chain upgrade also
passed against disposable PostgreSQL 16 containers. The historical-chain
measurement is separate from the actual new `aw03` round-trip tests above.
The wrapper ran 121 Node contracts; the final source adds two negative checks
and the separate final controller run passed all **123**. Full-tree frontend
ESLint exited 1 as an advisory with existing debt; no baseline was relaxed.

The first final-wrapper attempt had failed solely because the new feature
lacked a registered live scenario. The existing contract and coverage
requirement were preserved and the scenario below was implemented.

### Registered live sharing scenario

The [QA scenario](../../tests/e2e/qa/scenarios.mjs) registers
`workflow.artifact-version-sharing`, with authentication, authorized writes,
configured-model and browser prerequisites. Missing credentials or disabled
artifact/sharing capabilities block before fixture creation or model use.
It owns an isolated workspace, project and text document, attaches the document
through the v2 collection without starting a separate extraction job, and
submits one draft generation with a two-section bound. It waits for terminal
generation and native publication before checking exact saved bytes and a
self-audience share pinned to that version.

The five registered checkpoints are `artifact.fixture`,
`artifact.share-created`, `artifact.shared-ready`, `artifact.revoked` and
`artifact.no-private-content`. The scenario verifies the safe six-field
metadata contract, private/no-store responses, observed anonymous denial,
revoked metadata/content denial, and removal of private preview/title/body
text and download controls after browser refetch. Its content check is bounded
to 128 KiB. The one model-turn reservation counts a generation submission;
internal provider calls remain **unmeasured**.

The [session cleanup](../../tests/e2e/qa/session.mjs) retains known parent
fixtures when a child deletion fails, a mutation's accepted identity is missing,
or cancellation has not been proven terminal. Pending mutation risks are
registered before requests. It revokes only its exact registered share and
never reports complete cleanup while a known or unresolved task may still
write. Contract doubles cover accepted malformed/missing IDs, child-delete
conflicts, anonymous and revoked disclosure, byte corruption and stale DOM
content. Independent review passed 121 contracts and two additional cleanup
failure checks before the last two negative tests were added.

```sh
/tmp/rag-completion-node24/bin/node --test \
  tests/unit/scripts/nous-qa.test.mjs tests/unit/scripts/nous-verify.test.mjs
/tmp/nous-qa-fix-venv/bin/python scripts/ci/check_feature_map.py
```

The final controller run passed **123/123** contracts, zero skipped. These are
local doubles; the live browser/model scenario and screenshot checkpoints
remain **NOT RUN**.

## Checks not run and acceptance limits

| Gate | Status | Boundary |
| --- | --- | --- |
| Live authenticated sharing journey against running application services | **NOT RUN** | Component and FastAPI test-dependency results do not prove a deployed signed-in journey. |
| Registered sharing live QA scenario/screenshot checkpoints | **NOT RUN** | Local Chromium fixtures above are separate from the live QA harness. |
| `pnpm --dir frontend test:e2e:accessibility` | **NOT RUN** | Browser/running-service prerequisites were not exercised in this task. Accessible labels/focus unit tests do not establish global WCAG compliance. |
| Local production-component Chromium sharing/PDF proof | **PASS**, four tests | Explicit mocked auth/context/HTTP boundary; no running-backend or global sandbox-security acceptance claim. |
| Configured-provider/model baseline and live harness/golden replay | **NOT RUN** | Local fixture tests do not establish configured-provider behavior or performance. |
| Expert/product acceptance | **NOT RUN** | No expert acceptance session was recorded. |
| Hosted CI and automated PR review | **PENDING** | Started automatically on PR #1973; local verification does not establish the final head's hosted result. |
| Production deployment and release readiness | **NOT RUN** | No production deployment was requested or initiated by this task. |

Sequence after this completion slice merges remains the approved plan: fresh
develop with one migration head before the context migration; recall migration
after the context merge; bounded profile changes only after the configured-model
baseline. This record does not authorize or claim those later implementations.
