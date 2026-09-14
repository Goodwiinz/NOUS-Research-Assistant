# Invalid-pattern audit — 2026-09-14

## Executive summary

This is a point-in-time audit of repository state at Git SHA
`a269136add01f977c474e3550fc3069600fea8f0`. The working tree was clean when
the snapshot was recorded. The audit establishes the evidence vocabulary and
root-by-root baseline for the scoped `AGENTS.md` files planned by
`docs/superpowers/specs/2026-09-14-invalid-patterns-agent-guidance-design.md`.
It does not change production behavior.

Five confirmed findings were identified: five `medium`-severity findings and
no confirmed `critical`, `high`, or `low` findings. They are raw internal
exception details in two HTTP paths (IP-001 and IP-002), a mock feature-flag
loader path that does not reach the tracked config (IP-003), a document status
read without an organization predicate (IP-004), and known accessibility
deficiencies hidden by narrowly disabled axe rules in the legacy Sidebar test
(IP-005). The highest-priority work is to sanitize public errors, restore
tenant-aware status reads, repair the feature-flag consumer path, and remove
the Sidebar accessibility suppressions after fixing the rendered controls.

The audit also records enforced invariants and review risks separately. In
particular, most SQL/process/suppression matches are safe or documented after
inspection; the Trigger.dev loader relationship, duplicate configuration, and
tracked artifact candidates remain review risks until their consumers are
proved. Counts and classifications below are observations at this SHA, not
permanent claims.

## Scope and method

### Identity and scope

- Audited SHA: `a269136add01f977c474e3550fc3069600fea8f0`.
- Snapshot state: clean (`git status --short` produced no paths).
- Included roots: `backend`, `brand`, `config`, `data`, `database`,
  `deployment`, `docs`, `evals`, `feature-flags`, `frontend`,
  `infrastructure`, `memory`, `monitoring`, `notebooks`, `scripts`, `specs`,
  `src`, `supabase`, `tests`, and `tools`.
- Excluded by the approved design: hidden tool/runtime directories and flat
  roots such as `daily-logs`, `nginx`, `projects`, and `security`. The root
  `AGENTS.md` remains the repository-wide entry point; the existing
  `frontend/AGENTS.md` is a child-guide concern, not a reason to add guides to
  excluded roots.
- The exact-root assertion passed: the tracked non-hidden roots with
  descendants match the 20 included roots.

### Evidence sources

The source set included the approved design, root instructions, current
engineering contracts (`docs/engineering/README.md`, `backend.md`,
`frontend.md`, `testing.md`, `api-contracts.md`, and `gotchas.md`), the four
named CI workflows, `.pre-commit-config.yaml`, `Makefile`, root and frontend
manifests, `backend/Makefile`, `evals/README.md`, `monitoring/README.md`,
`tools/nous-playwright/README.md`, the pre-public security runbook, the dated
UX audit, and `scripts/wcag-checklist.sh`. Source and consumer searches also
covered the implementation, tests, generated artifacts, migrations, Helm
render assertions, and workflow documentation named by those contracts.

### Classification vocabulary

- **Confirmed finding** — an `IP-NNN` with a severity, exact path/line range,
  observed behavior, impact, evidence, affected root, and next action. The
  severity scale is `critical`, `high`, `medium`, and `low`; `critical` is
  reserved for immediately reachable credential disclosure, arbitrary
  execution, destructive data loss, or cross-tenant compromise.
- **Enforced invariant** — a safe behavior backed by an inspected test, hook,
  script, or workflow. It is preventative coverage, not open debt; a local
  check that could not run is recorded as such rather than treated as proof of
  a passing invariant.
- **Review risk** — candidate evidence whose reachability, consumer, historical
  status, or service-dependent behavior remains unproven. A review risk names
  the exact follow-up evidence required.

A text match is never treated as an exploitability proof. Candidates were
reviewed in their enclosing function or configuration consumer, including
data origin, access predicate, parameterization, shell parsing, route/task
registration, or rendered control props. Tests, fixtures, examples,
migrations, generated files, archived material, allowlisted constants, and
intentionally advisory CI lanes were not promoted to findings merely because
they matched a search pattern.

### Deterministic checks

Each required check was run independently. `PASS`, `FAIL`, and `NOT RUN` mean
the following for this snapshot:

| Command | Result | Concise evidence |
| --- | --- | --- |
| `python3 scripts/docs/check_dir_docs.py` | `PASS` | `dir-docs: 46 directory doc(s) OK.` |
| `ruff check backend/src` | `PASS` | Ruff reported `All checks passed!` |
| `(cd backend && python ../scripts/ci/check_alembic.py)` | `NOT RUN` | The exact command returned 127 because the `python` executable is unavailable. The `python3` equivalent was attempted separately and could not import `alembic`; no network install was attempted. |
| `python scripts/ci/generate_openapi.py --check` | `NOT RUN` | The exact command returned 127 because the `python` executable is unavailable. The `python3` equivalent was attempted separately and could not import `langgraph`; no network install was attempted. |
| `pytest -q backend/tests/unit/architecture backend/tests/unit/ci` | `FAIL` | 291 collection/test errors were caused by missing `langgraph`; this is a dependency failure, not evidence that the guards pass or fail on repository behavior. |
| `pnpm --dir frontend lint:changed` | `FAIL` | Exit 2: `check_frontend_quality.mjs` requires its CI `--report` argument. pnpm also reported Node 22.22.0 while the repository declares Node 24, and frontend `node_modules` is absent. |
| `pnpm --dir frontend quality:exclusions` | `PASS` | `OK: 23 baselined production exclusions, no new type-check debt.` |

The supplemental `python3` attempts above were diagnostic only. They did not
install dependencies, modify tracked files, or read credential values.

### Candidate-review searches

- Contract/consumer searches found the directory-doc lint, changed-file
  frontend/backend ratchets, OpenAPI generation, Alembic guard, Helm render
  assertions, actionlint, and gitleaks hooks/workflows. The workflow and older
  README evidence disagree about retired staging/production deployment paths;
  this is recorded as a review risk below rather than silently resolved.
- Exception searches produced many matches. IP-001 and IP-002 are the two
  reachable HTTP paths whose response details interpolate arbitrary exception
  text. Expected domain/validation errors and log-only catches were not called
  findings.
- SQL searches found parameterized values and allowlisted identifier
  interpolation in the inspected setup/backup scripts. No confirmed dynamic
  SQL injection was established.
- Process searches found a test-only shell wrapper, sandbox-local subprocess
  construction, and ordinary argument-list subprocess calls. No production
  arbitrary-shell finding was established after tracing their inputs.
- Tenant searches found the organization-aware `workspace_access` funnel and
  organization predicates in the inspected document/dedup/search paths. The
  status broadcast query in IP-004 remains an unscoped exception.
- Suppression searches found documented typing/lint exceptions, generated
  compatibility imports, and intentionally advisory workflow lanes. The
  Sidebar's two disabled axe rules document real rendered deficiencies and are
  represented by IP-005; no separate undocumented suppression finding was
  added.
- Accessibility inspection found that the production `IconButton` component
  requires a `label` prop and sets `aria-label`; the inspected IconButton
  consumers supplied labels. The Sidebar is the confirmed exception because
  its `<nav>` elements have no distinct labels and collapsed links contain no
  text or `aria-label`.
- Artifact, duplicate-basename, and symlink searches were path-only. No
  environment-file, trace, credential, or other sensitive value was opened or
  copied into this report.

### Sensitive-value handling

Environment files, auth/session artifacts, logs, traces, credentials, tokens,
and exception payloads were not opened or quoted. The report names only safe
repository-relative paths, line ranges, config keys, and the kind of risk.

## Confirmed findings

### IP-001 — Authentication dependency exposes arbitrary exception text

| Field | Evidence |
| --- | --- |
| Severity / root | `medium` / `backend` |
| Location | `backend/src/auth/ab_testing_auth.py:164-178` |
| Observed behavior | `get_current_user_from_token` turns JWT and broad exception messages into `HTTPException.detail`, including `str(e)`/`str(e)`-derived text, instead of returning a stable public authentication message. |
| Impact | A reachable authentication failure can disclose parser, conversion, or backend implementation details to the caller and creates an unstable public contract. The match is not a credential disclosure by itself, so it is not critical. |
| Evidence and validation | The enclosing function decodes a caller-provided bearer token, parses UUIDs, queries the user, and catches both `JWTError` and `Exception`. The `ruff` run passed, but the backend guard suite was not executable because `langgraph` is missing. No test was found that asserts exception-detail redaction for this function. |
| Next action | Log the exception internally with structured context and return a fixed 401 detail for all unexpected authentication failures; add focused tests for malformed tokens, UUID conversion, and database exceptions that assert no internal text crosses the HTTP boundary. |

### IP-002 — Admin worker-status endpoint exposes backend exception text

| Field | Evidence |
| --- | --- |
| Severity / root | `medium` / `backend` |
| Location | `backend/src/api/infrastructure/workers.py:132-141` |
| Observed behavior | The worker-status handler catches a broad exception and puts `str(e)` into a 500 response detail. The separate `ImportError` path has a stable message, but other broker/Celery failures are reflected to the caller. |
| Impact | An authenticated admin can receive internal broker, host, or implementation details; response wording also becomes coupled to third-party exception text. This is a bounded disclosure, not an immediately reachable compromise. |
| Evidence and validation | The enclosing route gathers Celery worker/task state and has an admin dependency; the broad catch is directly reachable when that inspection fails. No safe-error assertion was found for this route. The required backend test suite was blocked by missing `langgraph`. |
| Next action | Preserve the stable service-unavailable/server-error distinction, log the original exception internally, and test that the HTTP response contains only a stable public message. |

### IP-003 — Mock feature-flag loader resolves the wrong tracked path

| Field | Evidence |
| --- | --- |
| Severity / root | `medium` / `feature-flags` (consumer in `backend`) |
| Location | `backend/src/services/infrastructure/feature_flags.py:80-84` |
| Observed behavior | The loader joins the module directory with `../../feature-flags/launchdarkly-config.json`, which resolves to `backend/src/feature-flags/launchdarkly-config.json`. The tracked config is at `feature-flags/launchdarkly-config.json`; the resolved path does not exist at the audited SHA. |
| Impact | In mock/development mode the tracked flag config is skipped and hardcoded fallbacks are used. This can silently ignore a config change or make the mock behavior differ from the intended consumer. LaunchDarkly-backed mode is a separate path, so this is not classified as a live provider-key failure. |
| Evidence and validation | The constructor calls `_load_mock_flags` when the SDK/key path is unavailable, catches file-load errors, and then fills defaults. Existing unit tests patch `os.path.exists`/`open` and therefore exercise parsing but do not prove the real repository-relative path. No root-local feature-flag validator exists. |
| Next action | Resolve the path from the consuming file to the tracked root (or move the config under an explicitly owned path), then add a test that uses the real repository layout and verifies a known config entry is loaded without exposing provider keys. |

### IP-004 — Status broadcast reads a document without organization scope

| Field | Evidence |
| --- | --- |
| Severity / root | `medium` / `backend` |
| Location | `backend/src/services/infrastructure/status_update_service.py:178-195` |
| Observed behavior | `broadcast_document_update` accepts only `document_id` and queries `Document` with `Document.id == document_id`; it does not apply `organization_id` or `is_deleted` in the read predicate. The result is then shaped with title, filename, type, processing state, and owner/org-derived broadcast metadata. |
| Impact | A caller that supplies or obtains an ID outside its intended tenant boundary can make the status service load and broadcast another tenant's document metadata. Current channel fan-out later derives `target_organization` and skips an org-less shared broadcast, which reduces but does not replace an organization-aware read/access predicate. |
| Evidence and validation | The public realtime endpoints validate `Document.organization_id == organization.id` before calling this service, but internal processing/status callers also call it with only a document ID. The repository's backend contract explicitly requires organization predicates for every document, dedup, and search-suggestion query. Existing tests cover fail-closed channel fan-out for org-less rows, not this query predicate. |
| Next action | Thread the owning organization through every status producer, filter the document read by organization and live-row policy, and add a cross-tenant regression test covering both direct and queued broadcasts. |

### IP-005 — Sidebar accessibility rules are disabled for real rendered gaps

| Field | Evidence |
| --- | --- |
| Severity / root | `medium` / `frontend` |
| Location | `frontend/src/components/layout/Sidebar.tsx:135-161`; `frontend/src/components/layout/Sidebar.tsx:60-75`; test suppressions at `frontend/src/components/layout/__tests__/Sidebar.a11y.test.tsx:40-59` |
| Observed behavior | The main and bottom navigation are two unlabeled `<nav>` elements. When collapsed, each navigation link renders only an icon and relies on a Tooltip, with no text or `aria-label` on the link. The a11y test disables `landmark-unique` and, for the collapsed state, `link-name`. |
| Impact | Screen-reader users cannot reliably distinguish the navigation landmarks or name collapsed links in the rendered DOM. The test can pass while those two rules remain unsatisfied. The issue is a known legacy accessibility gap, not a claim that every frontend control is inaccessible. |
| Evidence and validation | The test comments explicitly identify both issues and the rendered `Sidebar` source confirms the missing props. Other inspected `IconButton` uses supplied the required `label` prop, so they were rejected as false positives. Accessibility tests are present in frontend source, but no full accessibility workflow was run in this credential-free snapshot. |
| Next action | Add distinct `aria-label` values to both nav landmarks and accessible names to collapsed links, then remove the two rule disables and run the focused axe tests plus the browser accessibility project. |

No confirmed finding was assigned `critical`, `high`, or `low` severity at this
point-in-time baseline. The absence of a finding in a severity is not a claim
that a future deeper review cannot discover one.

## Enforced invariants

These are preventative contracts verified in repository sources. Where the
local dependency set prevented execution, the existence and intended scope of
the gate are recorded without claiming a passing run.

| Invariant | Enforcing evidence | Guidance that must preserve it |
| --- | --- | --- |
| Workspace routers remain transport-only; services own persistence boundaries; compatibility router exports resolve to the composed routers. | `backend/tests/unit/architecture/test_workspace_boundaries.py` and the backend engineering contract. The required suite was attempted but blocked by missing `langgraph`. | `backend`: do not add route-owned transaction calls or sideways resource imports; keep compatibility exports deliberate. |
| Access helpers fail closed on caller identity; workspace access is membership/public/owner-based while document access is organization-scoped; soft-deleted ancestors revoke child access. | `backend/tests/unit/architecture/test_maintenance_contracts.py`, `backend/src/services/threads/workspace_access.py`, and `docs/engineering/backend.md`. Execution was blocked by the same missing dependency. | `backend`, `database`, `supabase`, and `tests`: preserve identity/org predicates and negative authorization coverage. |
| Generated OpenAPI and frontend TypeScript artifacts are regenerated from the FastAPI app and diffed together. | `scripts/ci/generate_openapi.py`, frontend `generate:api-types`/`check:api-types`, `.github/workflows/test-pipeline.yml` `openapi-contract` job, and `backend/tests/unit/ci/test_generate_openapi.py`. The OpenAPI check could not import `langgraph`. | `backend`, `frontend`, and `specs`: never hand-edit generated API files; adopt wire types on touch. |
| Migration history has a blocking static single-head/revision-length guard, while execution probes are explicitly service-dependent/advisory where documented. | `scripts/ci/check_alembic.py`, the `migration-check` job in `.github/workflows/test-pipeline.yml`, and migration contract tests. The static command was not runnable because `python`/`alembic` prerequisites were unavailable. | `database`, `supabase`, `backend`, and `tests`: append migrations, preserve order, and label DB probes honestly. |
| Backend and frontend quality ratchets do not accept new changed-file debt; tsconfig production exclusions require a reviewed baseline. | CI changed-file steps, `.pre-commit-config.yaml`, `scripts/ci/check_frontend_quality.mjs`, `scripts/ci/check_tsconfig_exclusions.py`, and `frontend/quality-baseline.json`. The exclusion ratchet passed; changed-lint was invoked without its CI report argument and failed. | `backend`, `frontend`, `scripts`, and `tests`: do not lower floors or bypass changed-file gates. |
| Secret scanning is present before commit and in protected-branch CI, with repository-specific rules and security-control tests. | `.pre-commit-config.yaml`, `.github/workflows/secret-scan.yml`, `.gitleaks.toml`, and `backend/tests/security/test_pre_public_security_audit_guards.py`. No gitleaks binary was installed or scan run in this audit. | All roots: keep secrets indirect, do not echo values, and preserve the scan hooks/workflow. |
| Accessibility checks exist for selected rendered components, and `IconButton` requires/propagates an accessible label. | `frontend/src/test/a11y.ts`, selected component tests, `frontend/package.json` accessibility script, and `frontend/src/components/ui/icon-button.tsx`. The helper's no-op fallback and Sidebar suppressions are limitations/risk; there is no claim of global compliance. | `frontend`, `tools`, and `tests`: preserve accessible names, keyboard/focus behavior, and real axe/browser validation. |
| Helm environments are linted/rendered as base-plus-overlay combinations with NetworkPolicy, immutable-image, and PDB assertions. | `.github/workflows/helm-validate.yml` and the three tracked chart test scripts. Helm/Docker/cluster execution was not attempted in this credential-free audit. | `infrastructure`, `deployment`, `config`, and `monitoring`: preserve consumer-aware overlays and render checks; do not apply to a cluster without authorization. |

## Review risks

These candidates are intentionally not counted as additional confirmed
findings. Each has a concrete follow-up requirement.

| Candidate | Current classification | Required follow-up evidence |
| --- | --- | --- |
| `frontend/trigger.config.ts` declares `dirs: ["./src/trigger"]` relative to `frontend`, while the root `trigger.config.ts` and root `tsconfig.json` load `src/trigger/`; `frontend/src/trigger/example.ts` is the only tracked child example. | Ambiguous Trigger.dev reachability and likely stale/duplicate configuration. | Prove which config the deployed Trigger command consumes, then either align the loader path or document the intentionally separate package. Do not claim root `src/trigger` is deployed from the frontend config without that proof. |
| `src/trigger/_lib/backend-client.ts` centralizes bounded retries, optional service authorization, and response schemas for the root Trigger jobs; user-scoped agent jobs pass a user bearer token and the backend endpoints remain the authorization boundary. | Preventative pattern with deployment/reachability risk, not a confirmed vulnerability. | Verify each registered task is loaded by the root config, each write is idempotent/HITL-safe, and logs/metadata never contain access or service-role tokens. |
| `frontend/lint_output.txt` is tracked; `.env`-named example/test files are tracked under `backend`, `config/environments`, and `tests/e2e`. | Path-only tracked-artifact/config risk. | Identify whether the lint output is consumed; remove or replace it only through an approved history/ownership decision. Confirm each environment file contains sanitized test/example material without opening values in an audit. |
| Numerous duplicate basenames occur across `deployment`, `infrastructure`, `config`, and `monitoring` (charts, values, Compose/Prometheus/Grafana files). Root Compose symlinks point to `config/docker-compose/`; live dev Argo CD points to the knowledge-graph Helm chart, while staging/production material remains in the tree. | Duplicate configuration and historical/live-status risk; the symlinks themselves are intentional. | Trace every change to its workflow/consumer, render the relevant base-plus-overlay combination, and reconcile the older deployment README/workflows with `docs/engineering/gotchas.md` before changing status claims. |
| `scripts/test_arxiv_cli_neo4j.py` uses `subprocess.run(..., shell=True)` with hardcoded test commands and a fixed local path. | Rejected false positive for arbitrary execution: test-only, constant command strings, no user-controlled input, and not an application path. | If promoted to a reusable/production tool, replace it with an argument list and an explicit configurable working directory. |
| `scripts/backup/disaster_recovery.py` and `scripts/setup_databases.py` interpolate SQL identifiers into DDL. | Rejected false positive for SQL injection in inspected paths: identifiers pass `safe_identifier`; values are parameterized or quoted for the intended setup operation. | Keep the validator and parameterization when editing; add focused tests if new identifier inputs are introduced. |
| `backend/src/services/sandbox/e2b_sandbox_manager.py` constructs subprocess calls inside an E2B sandbox; video processing uses argument-list `subprocess.run` without shell parsing. | Rejected false positive for host arbitrary execution. The sandbox is the explicit execution boundary and package names are filtered; video calls use argv lists. | Preserve the sandbox boundary, package allowlist, timeout, and no-shell argument form. |
| `frontend/src/test/a11y.ts` has a no-op fallback when `jest-axe` is unavailable; many typing/lint suppressions are documented, and workflow `continue-on-error` lanes are explicitly advisory. | Review risk, not a new suppression finding at this SHA. | Make the accessibility helper fail closed in CI or assert dependency presence, and keep each suppression tied to a narrow rationale/removal condition. Do not call advisory lanes blocking. |
| Service-, credential-, browser-, Docker-, Helm-, cluster-, and Harbor-dependent paths were not exercised. | Service-dependent validation gap. | Run only in the authorized environment with the required pinned tooling and record the result separately from this credential-free baseline. |

## Root coverage and guidance mapping

The following table is the handoff contract for the 20 child guides. `IP-*`
refers to confirmed findings above; invariant labels refer to the enforced
invariant table. The final column describes durable rule themes, not new
production guarantees.

| Root | Evidence reviewed | Findings/invariants | Intended guidance themes |
| --- | --- | --- | --- |
| `backend` | `docs/engineering/backend.md`, `api-contracts.md`, `testing.md`, `gotchas.md`; API/services/tests; CI and OpenAPI scripts. | IP-001, IP-002, IP-004; router/access/generated/migration/ratchet invariants. | Router → service → transaction boundaries; membership vs organization scope; ancestor soft-delete checks; safe public errors; bound SQL/validated enums; compatibility exports; generated contract workflow. |
| `brand` | Tracked SVG/HTML/source assets, screenshots, DOCX, and root `README.md` consumer. | No IP; artifact review risk. | Preserve source/rendered artifacts, provenance, accessibility/identity metadata, and consumer-aware visual review; no bulk binary overwrite. |
| `config` | `config/docker-compose/`, root Compose symlinks, manifests, workflow consumers, and environment-path inventory. | Duplicate-config risk; Compose/render invariant. | Edit canonical targets once; preserve layering and symlinks; keep environment values/secrets indirect; do not silently diverge CI/prod defaults. |
| `data` | `data/datasets/` inventory and referenced evaluation/test consumers. | No IP; artifact/provenance risk. | Preserve provenance and schemas; do not mutate fixtures to hide failures or commit customer/sensitive data; require named consumer for replacement. |
| `database` | Standalone SQL/Cypher/JSON assets plus Alembic/Supabase references and dynamic SQL candidates. | SQL false-positive review; migration/tenant invariants. | Distinguish standalone assets from live migration systems; parameterize/allowlist SQL; preserve tenant predicates, ordering, transactions, and rollback reasoning; authorize applies. |
| `deployment` | Legacy Helm/Kubernetes tree, deployment workflows, and retirement notes in `gotchas.md`. | Duplicate/live-status risk; Helm invariant. | Treat legacy paths as non-live until proven; require consumer/reachability checks; validate charts read-only; no deploy/apply/rollback without authorization. |
| `docs` | Engineering index, dated audits/reports/plans/specs, directory-doc tooling, and historical workflow docs. | Documentation contradiction risk; directory-doc invariant. | Keep canonical vs historical hierarchy clear, links/paths real, dated records immutable, and old status/commands labeled. |
| `evals` | `evals/README.md`, baseline/manifests, task specs/verifiers, and judge-separation text. | Artifact/provenance risk; isolation invariant. | Keep task digests/baselines immutable, isolate judge credentials, preserve fixtures, and never score infrastructure failure as reward zero. |
| `feature-flags` | `launchdarkly-config.json`, backend loader, enum/default tests, and frontend flag consumer. | IP-003. | Prove runtime consumer, align keys/enums/defaults/rollouts, keep configs secret-free, and test relative paths before calling config live. |
| `frontend` | `docs/engineering/frontend.md`, `api-contracts.md`, `testing.md`, UX audit, WCAG checklist, components/tests, and package scripts. | IP-005; frontend/ratchet/generated/accessibility invariants. | One cache owner; chat-store facade; backend-only writes/reconciliation; adapter/generated-type rules; ratchets only tighten; accessible names/focus; Node 24/pnpm/root lockfile. |
| `infrastructure` | Argo CD dev application, knowledge-graph Helm chart/overlays/tests, workflow README, and retirement notes. | Duplicate/live-status risk; Helm invariant. | Preserve live dev ownership, base-plus-overlay rendering, secret indirection, full-SHA/digest images, NetworkPolicy/PDB assertions; no cluster apply. |
| `memory` | Dated contextual notes and repository references; no authoritative implementation contract. | No IP; provenance/privacy risk. | Treat notes as contextual and potentially personal; date claims, cross-check sources, and keep secrets/personal data out of reports. |
| `monitoring` | `monitoring/README.md`, local Compose/Prometheus/Grafana/OTel files, deployment copies, and script consumers. | Duplicate-config risk; secret-scan invariant. | Identify canonical consumer, do not synchronize same-named configs blindly, keep `.env`/webhook/auth values private, and validate config without claiming live scrape/alert health. |
| `notebooks` | Notebook/output/data inventory and references to exploratory workflows. | No IP; artifact/provenance risk. | Preserve exploratory status and reproducibility notes; do not bulk-reformat/erase outputs or embed credentials; require explicit regeneration. |
| `scripts` | CI/static scripts, deployment/backup/data scripts, process/SQL candidates, and script tests. | SQL/process false positives; ratchet/secret invariants. | Read whole scripts before execution; authorize destructive/remote/secret actions; use safe argv/temp paths, validated inputs, cleanup, and honest partial-failure handling. |
| `specs` | Approved design/specs, plans, checklists, and contract snapshots. | No IP; historical-record risk. | Keep numbered records immutable, do not infer implementation from checkboxes, and point live API claims to FastAPI/generated contracts. |
| `src` | Root Trigger configs, `src/trigger` tasks, shared client/schemas, package scripts, and frontend duplicate config. | Trigger reachability review; client/idempotency invariant. | Prove loader/deployment path; centralize client/schema use; preserve backend authorization, bounded retries, secret-safe logs, and HITL/idempotent writes. |
| `supabase` | Timestamped migrations, RLS/security migrations, config boundaries, and backend migration contracts. | Tenant/migration invariant. | Keep migrations append-only/ordered; preserve RLS, tenant predicates, function `search_path`, and auth-template boundaries; label reset/push as destructive/service-dependent. |
| `tests` | Backend architecture/CI/security tests, frontend tests, E2E/load docs, and testing contract. | All findings' regression-test follow-ups; test invariant. | Select the right layer; preserve realistic fixtures/negative auth; prove race/idempotency guards by mutation; label services/browser; authorize disruptive shared-dev loads. |
| `tools` | `tools/nous-playwright/README.md`, standalone package and auth/artifact instructions. | Authenticated-artifact review risk. | Keep standalone install/lockfile boundary, never commit auth/traces/videos/reports, require explicit user interaction for live mutations, and use discovery-only checks by default. |

## Remediation priorities

1. Sanitize IP-001 and IP-002 at the HTTP boundary, retain internal
   structured exception logging, and add focused non-disclosure tests.
2. Fix IP-004 by carrying organization identity through status producers and
   enforcing organization/live-row predicates in the service read; add a
   cross-tenant regression test for direct and queued broadcasts.
3. Fix IP-003's repository-relative path and add a real-layout feature-flag
   test that prevents silent fallback when the tracked config changes.
4. Fix IP-005 in the rendered Sidebar, remove the disabled axe rules, and run
   focused component checks plus the browser accessibility project in an
   installed Node 24 environment.
5. Resolve the Trigger config relationship and duplicate/stale configuration
   risks with consumer evidence; separately decide the fate of tracked
   `frontend/lint_output.txt` and environment test/example artifacts. Do not
   delete or rewrite historical material as a side effect of this audit.
6. Restore the pinned local validation prerequisites (Python/Alembic/backend
   dependencies and frontend Node 24/frozen install) before treating the
   blocked checks as a usable baseline. This documentation change itself fixes
   only missing guidance; it does not remediate production code.

## Limitations

- Textual searches miss dynamically constructed paths, SQL, subprocess
  arguments, generated code, runtime registration, and browser-only behavior.
  They are candidate discovery, not complete static analysis.
- The required Alembic/OpenAPI checks were not runnable with the exact
  commands because the `python` alias is absent; their `python3` diagnostics
  were blocked by missing `alembic`/`langgraph`. The backend unit suite failed
  during collection for the same missing `langgraph` dependency.
- The frontend changed-lint wrapper was not run through the CI-provided report
  setup and the local runtime is Node 22 rather than the declared Node 24;
  only the tsconfig-exclusion ratchet passed directly.
- No production service, database, Redis, browser, Helm, Docker, Kubernetes,
  Supabase, LaunchDarkly, Harbor, credential, or cluster test was run. The
  report does not assert live deployment, scrape, auth, or alert behavior.
- Existing workflows and older READMEs contain historical/live-status tension,
  especially around staging/production deployment. This report preserves the
  contradiction as a review risk instead of choosing the older prose.
- Environment, auth, trace, log, and credential values were intentionally not
  read or quoted; some artifact/config conclusions therefore remain
  path-only review risks.
- Counts are tied to the audited SHA and the inspected candidate set. They may
  change after dependency installation, a broader runtime review, or later
  repository commits.
