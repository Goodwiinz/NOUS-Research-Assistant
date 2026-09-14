# Invalid-pattern audit — 2026-09-14

## Executive summary

This is a point-in-time audit of repository state at Git SHA
`a269136add01f977c474e3550fc3069600fea8f0`. The working tree was clean when
the snapshot was recorded. The audit establishes the evidence vocabulary and
root-by-root baseline for the scoped `AGENTS.md` files planned by
`docs/superpowers/specs/2026-09-14-invalid-patterns-agent-guidance-design.md`.
It does not change production behavior.

Two confirmed findings were identified: one `medium`-severity finding and one
`low`-severity finding, with no confirmed `critical` or `high` findings. The
confirmed findings are raw internal exception details in the registered admin
worker-status endpoint (IP-001) and a document status read that violates the
organization/live-row query contract (IP-002). Three candidates remain review
risks because reachability or production impact was not proved: the
unregistered authentication helper's exception details (RR-001), the
feature-flag loader path (RR-002), and the legacy Sidebar accessibility gaps
(RR-003). The highest-priority work is to sanitize the registered admin error,
restore the status service's defense-in-depth query contract, and then prove
the three held candidates' consumers before changing production behavior.

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

Each required check was run independently. `PASS` means the check completed
successfully; `FAIL` means a completed check found a repository defect;
`BLOCKED` means the command or its preflight was invoked but could not
complete because a tool, dependency, or environment was unavailable; and
`NOT RUN` means the operation was intentionally withheld.

| Command | Result | Concise evidence |
| --- | --- | --- |
| `python3 scripts/docs/check_dir_docs.py` | `PASS` | `dir-docs: 46 directory doc(s) OK.` |
| `ruff check backend/src` | `PASS` | Ruff reported `All checks passed!` |
| `(cd backend && python ../scripts/ci/check_alembic.py)` | `BLOCKED` | The exact command returned 127 because the `python` executable is unavailable. The `python3` equivalent was attempted separately and could not import `alembic`; no network install was attempted. |
| `python scripts/ci/generate_openapi.py --check` | `BLOCKED` | The exact command returned 127 because the `python` executable is unavailable. The `python3` equivalent was attempted separately and could not import `langgraph`; no network install was attempted. |
| `pytest -q backend/tests/unit/architecture backend/tests/unit/ci` | `BLOCKED` | 291 collection/test errors were caused by missing `langgraph`; this is a dependency failure, not evidence that the guards pass or fail on repository behavior. |
| `pnpm --dir frontend lint:changed` | `BLOCKED` | Exit 2: `check_frontend_quality.mjs` requires its CI `--report` argument. pnpm also reported Node 22.22.0 while the repository declares Node 24, and frontend `node_modules` is absent. |
| `pnpm --dir frontend quality:exclusions` | `PASS` | `OK: 23 baselined production exclusions, no new type-check debt.` |

The supplemental `python3` attempts above were diagnostic only. They did not
install dependencies, modify tracked files, or read credential values.

### Final acceptance checks

Task 6 reran the safe acceptance matrix at implementation HEAD
`7707d514af9922acb4099fa252b2c4fef1c8bb7d`, before this audit reconciliation.
The status vocabulary above is applied uniformly here: missing-tool or
missing-dependency attempts are `BLOCKED`, while intentionally withheld live
operations are `NOT RUN`.

| Area | Command or review | Result and exact evidence |
| --- | --- | --- |
| Scope | Exact 20-root assertion; `git diff --name-only a269136add01f977c474e3550fc3069600fea8f..HEAD -- AGENTS.md` | `PASS` — exact set printed `exactly 20 selected top-level roots contain AGENTS.md`; root guide diff was empty. |
| Structure/content | Four required headings in every guide; frontend anchors; unfinished-marker scan | `PASS` — all 20 headings, six frontend anchors, and no banned marker/boilerplate matches. |
| Links/paths | Local Markdown-link resolver and explicit source-path ledger | `PASS` — `all local Markdown links resolve`; `all explicit source-path ledger entries exist`. |
| Provenance | Focused command-definition ledger from the brief | `PASS` — every named command had a matching engineering, manifest, workflow, or script definition. |
| Contract review | Side-by-side review of application/test guides against backend, frontend, testing, API, and gotchas contracts | `PASS` — organization/workspace scope, generated-artifact, ratchet, deployment-status, authorization, evidence, and verification gates remained consistent. |
| Documentation | `make docs-lint` | `PASS` — `dir-docs: 46 directory doc(s) OK.` |
| Python static lint | `ruff check backend/src` | `PASS` — `All checks passed!` |
| Script contracts | `python3 -m pytest tests/unit/scripts/ --confcutdir=tests/unit/scripts -q -p no:cacheprovider --no-cov` | `PASS` — `161 passed, 4 warnings in 22.76s`. |
| Backend architecture/CI | `pytest -q backend/tests/unit/architecture backend/tests/unit/ci` | `BLOCKED` (`exit 1`) — 291 collection errors, all reported as missing `langgraph`; not a behavioral pass/fail. |
| Backend API/threads | `pytest -q backend/tests/unit/api backend/tests/api/threads` | `BLOCKED` (`exit 2`) — 55 collection errors, including missing `spacy`; not a behavioral pass/fail. |
| Backend services/threads | `pytest -q backend/tests/unit/services/threads backend/tests/api/threads` | `BLOCKED` (`exit 2`) — 20 collection errors, including missing `spacy`; not a behavioral pass/fail. |
| OpenAPI | `python scripts/ci/generate_openapi.py --check` | `BLOCKED` (`exit 127`) — `python` executable is unavailable. The diagnostic `python3` equivalent was blocked by missing `langgraph`. |
| Alembic | `(cd backend && python ../scripts/ci/check_alembic.py)` | `BLOCKED` (`exit 127`) — `python` executable is unavailable. The diagnostic `python3` equivalent was blocked by missing `alembic`. |
| Frontend exclusion ratchet | `pnpm --dir frontend quality:exclusions` | `PASS` — `OK: 23 baselined production exclusions, no new type-check debt`; pnpm warned that the runtime is Node 22.22.0 rather than declared Node 24. |
| Frontend changed lint | `pnpm --dir frontend lint:changed` | `BLOCKED` (`exit 2`) — wrapper requires CI `--report`; Node 22.22.0 and absent `frontend/node_modules` were also reported. |
| Frontend type/test | `pnpm --dir frontend type-check`; `pnpm --dir frontend test` | `BLOCKED` — both reached package scripts but could not execute because `tsc`/`vitest` were unavailable with no local dependencies. |
| Compose checks | `docker compose -f config/docker-compose/docker-compose.development.yml config --quiet`; `bash infrastructure/tests/docker_compose_development_config_test.sh`; `bash scripts/validate_docker_compose.sh` | `BLOCKED` — Docker was present, but the Compose subcommand/plugin was unavailable (`unknown shorthand flag: 'f'`; monitoring script reported `Docker Compose is not installed`). |
| Helm checks | Legacy lint, three environment lint/template loops, and NetworkPolicy/image-digest/PDB render scripts | `BLOCKED` — invoked Helm checks reported `command not found`; the remaining loops were not entered after that preflight. No cluster operation was attempted. |
| Playwright listing | `pnpm test:list` from `tools/nous-playwright` | `BLOCKED` — package dependencies are absent (`playwright: not found`). Browser/service checks were not attempted. |
| Secret-scan preflight/scan | `command -v gitleaks`; actual `gitleaks git ...` scan | `BLOCKED` preflight — `gitleaks unavailable`; `NOT RUN` scan — intentionally withheld after the preflight failure. |
| Intentionally withheld live operations | Browser E2E/accessibility, hosted services, database/Redis, deployment/cluster, credentials, Harbor, and live webhooks | `NOT RUN` — intentionally withheld by the credential-free/safety scope; no command was invoked. |
| Diff/working tree | `git diff --check`; `git status --short`; `git diff --name-only a269136add01f977c474e3550fc3069600fea8f..HEAD` | `PASS` before reconciliation — whitespace check was silent, status was clean, and the base diff contained only the audit plus the 20 scoped guides. |

The acceptance matrix intentionally did not run browser, hosted service,
database, credential, deployment, cluster, destructive, or live webhook
operations. Those limits are validation gaps, not evidence that the related
production behavior is healthy.

### Final-fix provenance reconciliation (2026-09-14)

The original acceptance results above remain point-in-time evidence from the
implementation HEAD; this amendment records limitations proven while
reconciling the scoped guidance and does not convert any prior `BLOCKED` or
withheld operation into a pass. The frontend package's `lint:changed` script
delegates directly to `scripts/ci/check_frontend_quality.mjs`, whose parser
requires `--report` and whose `--base` option obtains changed frontend paths
through `scripts/ci/changed_source_files.py`. The CI workflow first generates
`/tmp/eslint-report.json` with `pnpm exec eslint app src --format json`, then
runs the comparator with `--report ... --base "$BASE"`; the local-CI wrapper
uses the same temporary-report and base-ref sequence. Consequently, a bare
`pnpm --dir frontend lint:changed` remains an invalid standalone ratchet
invocation even after dependencies are installed; the historical exit-2
result in the acceptance table is preserved as recorded.

The monitoring validator's source also proves two current limitations: it
requires the standalone `docker-compose` executable, and its required-file
check still names root-level `QUICK_START_MONITORING.sh` and
`START_MONITORED_SYSTEM.sh` even though the tracked files are now under
`scripts/utilities/` and `scripts/startup/`. The historical Compose `BLOCKED`
result and all intentionally withheld live operations remain unchanged; no
operational script fix or claim that this validator can pass was made here.

### Candidate-review searches

- Contract/consumer searches found the directory-doc lint, changed-file
  frontend/backend ratchets, OpenAPI generation, Alembic guard, Helm render
  assertions, actionlint, and gitleaks hooks/workflows. The workflow and older
  README evidence disagree about retired staging/production deployment paths;
  this is recorded as a review risk below rather than silently resolved.
- Exception searches produced many matches. IP-001 is the registered HTTP path
  whose response detail interpolates arbitrary exception text. RR-001 is an
  unregistered authentication helper with the same pattern; its module-local
  callers do not establish an active route or middleware consumer. Expected
  domain/validation errors and log-only catches were not called findings.
- SQL searches found parameterized values and allowlisted identifier
  interpolation in the inspected setup/backup scripts. No confirmed dynamic
  SQL injection was established.
- Process searches found a test-only shell wrapper, sandbox-local subprocess
  construction, and ordinary argument-list subprocess calls. No production
  arbitrary-shell finding was established after tracing their inputs.
- Tenant searches found the organization-aware `workspace_access` funnel and
  organization predicates in the inspected document/dedup/search paths. The
  status broadcast query in IP-002 remains an unscoped contract exception;
  public realtime consumers scope before calling it and delivery derives its
  target organization and owner from the loaded document.
- Suppression searches found documented typing/lint exceptions, generated
  compatibility imports, and intentionally advisory workflow lanes. The
  Sidebar's two disabled axe rules document real component-local deficiencies;
  because the active dashboard uses `SidebarLayout`/`AppRail` and the legacy
  `AppLayout` consumer was not found, they remain RR-003 rather than a current
  production finding. No separate undocumented suppression finding was
  added.
- Accessibility inspection found that the production `IconButton` component
  requires a `label` prop and sets `aria-label`; the inspected IconButton
  consumers supplied labels. The legacy Sidebar's `<nav>` elements have no
  distinct labels and collapsed links contain no text or `aria-label`, but its
  production reachability is unproven and is recorded as RR-003.
- Artifact, duplicate-basename, and symlink searches were path-only. No
  environment-file, trace, credential, or other sensitive value was opened or
  copied into this report.

### Sensitive-value handling

Environment files, auth/session artifacts, logs, traces, credentials, tokens,
and exception payloads were not opened or quoted. The report names only safe
repository-relative paths, line ranges, config keys, and the kind of risk.

## Confirmed findings

### IP-001 — Admin worker-status endpoint exposes backend exception text

| Field | Evidence |
| --- | --- |
| Severity / root | `medium` / `backend` |
| Location | `backend/src/api/infrastructure/workers.py:132-141` (stable `ImportError` detail at `:135`; interpolated broad-exception detail at `:140`) |
| Observed behavior | The worker-status handler catches a broad exception and puts `str(e)` into a 500 response detail at line 140. The separate `ImportError` path at lines 132-136 has a stable message, but other broker/Celery failures are reflected to the caller. |
| Impact | An authenticated admin can receive internal broker, host, or implementation details; response wording also becomes coupled to third-party exception text. This is a bounded disclosure, not an immediately reachable compromise. |
| Evidence and validation | The enclosing route gathers Celery worker/task state and has an admin dependency; the broad catch is directly reachable when that inspection fails. No safe-error assertion was found for this route. The required backend test suites were blocked by missing `langgraph`/`spacy` dependencies. |
| Next action | Preserve the stable service-unavailable/server-error distinction, log the original exception internally, and test that the HTTP response contains only a stable public message. |

### IP-002 — Status broadcast reads a document without organization scope

| Field | Evidence |
| --- | --- |
| Severity / root | `low` / `backend` |
| Location | `backend/src/services/infrastructure/status_update_service.py:178-195` |
| Observed behavior | `broadcast_document_update` accepts only `document_id` and queries `Document` with `Document.id == document_id`; it does not apply `organization_id` or `is_deleted` in the read predicate. The result is then shaped with title, filename, type, processing state, and owner/org-derived broadcast metadata. |
| Impact | This is a confirmed defense-in-depth and live-row query-contract violation, but the inspected call paths do not demonstrate a requester injecting a foreign ID and receiving or misdirecting data. Public realtime consumers first enforce organization and live-row scope, and delivery is derived from the loaded document's own organization and owner. The remaining risk is stale/deleted metadata and future callers bypassing the established route checks, so no current cross-tenant disclosure is claimed. |
| Evidence and validation | The public realtime endpoints enforce `Document.organization_id == organization.id` and `Document.is_deleted == False` before calling the service. Internal processing/status callers call it with only a document ID, but no caller was found that accepts an untrusted foreign ID or bypasses the service's own delivery target. Existing tests cover fail-closed channel fan-out for org-less rows, not this query predicate. |
| Next action | Thread the owning organization through status producers or require a preloaded scoped document, filter the read by organization and live-row policy, and add direct/queued regression tests for cross-tenant IDs and deleted rows. |

No confirmed finding was assigned `critical` or `high` severity at this
point-in-time baseline. The absence of a finding in a severity is not a claim
that a future deeper review cannot discover one.

## Review-risk candidates formerly considered findings

#### RR-001 — Authentication helper exposes arbitrary exception text

| Field | Evidence |
| --- | --- |
| Classification / root | Review risk / `backend` |
| Location | `backend/src/auth/ab_testing_auth.py:117-190`, with module-local references at `:220`, `:546-611`; JWT/broad-exception details are emitted at `:181-190` |
| Candidate behavior | `get_current_user_from_token` places JWT and broad exception text in `HTTPException.detail`. |
| Why not confirmed | Repository references found for this helper are confined to the same unregistered module; no active route or middleware imports it. The pattern is therefore not a demonstrated reachable HTTP disclosure at this SHA. |
| Required follow-up | Prove route or middleware registration and then sanitize unexpected details, log structured internal context, and add malformed-token/UUID/database non-disclosure tests if an active consumer is found. |

#### RR-002 — Feature-flag loader resolves an unverified tracked path

| Field | Evidence |
| --- | --- |
| Classification / root | Review risk / `feature-flags` (candidate consumer in `backend`) |
| Location | `backend/src/services/infrastructure/feature_flags.py:80-84`; re-export at `backend/src/services/infrastructure/__init__.py:8,29-31` |
| Candidate behavior | The loader joins the module directory with `../../feature-flags/launchdarkly-config.json`, resolving to `backend/src/feature-flags/launchdarkly-config.json`, while the tracked config is at `feature-flags/launchdarkly-config.json`. |
| Why not confirmed | Production code only re-exports `FeatureFlagService` from `backend/src/services/infrastructure/__init__.py:8,29-31`; the sole concrete consumer found imports the module directly for isolated unit tests. No runtime registration or production consumer was verified, so mock/development impact cannot be claimed. |
| Required follow-up | Prove a runtime consumer/registration, resolve the relative path from that consumer, and add a real-layout test before calling the root config live or changing it. |

#### RR-003 — Legacy Sidebar accessibility suppressions hide component-local gaps

| Field | Evidence |
| --- | --- |
| Classification / root | Review risk / `frontend` |
| Location | `frontend/src/components/layout/Sidebar.tsx:60-75,135-161`; suppressions at `frontend/src/components/layout/__tests__/Sidebar.a11y.test.tsx:40-59` |
| Candidate behavior | The legacy Sidebar has unlabeled `<nav>` landmarks and icon-only collapsed links without text or `aria-label`; its a11y test disables `landmark-unique` and `link-name`. |
| Why not confirmed | The active dashboard imports `SidebarLayout` in `frontend/app/(dashboard)/dashboard-layout-client.tsx:4,31-38`, and `SidebarLayout` renders `AppRail` at `frontend/src/components/layout/SidebarLayout.tsx:75-78`. The defective Sidebar is reached only through the otherwise unreferenced legacy `AppLayout` and its tests. Current production user impact is therefore unproven. |
| Required follow-up | Prove whether `AppLayout` is loaded by any active route/build entry; if it is, add landmark/link names, remove the two suppressions, and run focused axe plus browser accessibility checks. |

## Enforced invariants

These are preventative contracts verified in repository sources. Where the
local dependency set prevented execution, the existence and intended scope of
the gate are recorded without claiming a passing run.

| Invariant | Enforcing evidence | Guidance that must preserve it |
| --- | --- | --- |
| Workspace routers remain transport-only; services own persistence boundaries; compatibility router exports resolve to the composed routers. | `backend/tests/unit/architecture/test_workspace_boundaries.py:204-408` and `docs/engineering/backend.md:3-31`. The required suite was attempted but blocked by missing `langgraph`. | `backend`: do not add route-owned transaction calls or sideways resource imports; keep compatibility exports deliberate. |
| Access helpers fail closed on caller identity; workspace access is membership/public/owner-based while document access is organization-scoped; soft-deleted ancestors revoke child access. | `backend/tests/unit/architecture/test_maintenance_contracts.py:81-120`, `backend/src/services/threads/workspace_access.py:320-385`, and `docs/engineering/backend.md:42-70`. Execution was blocked by the same missing dependency. | `backend`, `database`, `supabase`, and `tests`: preserve identity/org predicates and negative authorization coverage. |
| Generated OpenAPI and frontend TypeScript artifacts are regenerated from the FastAPI app and diffed together. | `scripts/ci/generate_openapi.py:1-142`, `.github/workflows/test-pipeline.yml:491-560`, and `backend/tests/unit/ci/test_generate_openapi.py:104-139`. The OpenAPI check could not import `langgraph`. | `backend`, `frontend`, and `specs`: never hand-edit generated API files; adopt wire types on touch. |
| Migration history has a blocking static single-head/revision-length guard, while execution probes are explicitly service-dependent/advisory where documented. | `scripts/ci/check_alembic.py:60-140`, `.github/workflows/test-pipeline.yml:219-268`, and migration contract tests. The static command was not runnable because `python`/`alembic` prerequisites were unavailable. | `database`, `supabase`, `backend`, and `tests`: append migrations, preserve order, and label DB probes honestly. |
| Backend and frontend quality ratchets do not accept new changed-file debt; tsconfig production exclusions require a reviewed baseline. | `.github/workflows/test-pipeline.yml:82-191`, `scripts/ci/check_frontend_quality.mjs:1-20`, `scripts/ci/check_tsconfig_exclusions.py:1-22`, and `frontend/quality-baseline.json:1-23`. The exclusion ratchet passed; changed-lint was blocked because its CI report argument and local frontend dependencies were unavailable. | `backend`, `frontend`, `scripts`, and `tests`: do not lower floors or bypass changed-file gates. |
| Secret scanning is present before commit and in protected-branch CI, with repository-specific rules and security-control tests. | `.pre-commit-config.yaml:40-47`, `.github/workflows/secret-scan.yml:18-55`, `.gitleaks.toml:1-20`, and `backend/tests/security/test_pre_public_security_audit_guards.py:92-124`. No gitleaks binary was installed or scan run in this audit. | All roots: keep secrets indirect, do not echo values, and preserve the scan hooks/workflow. |
| Accessibility checks exist for selected rendered components, and `IconButton` requires/propagates an accessible label. | `frontend/src/test/a11y.ts:1-37`, `frontend/package.json:150-158`, and `frontend/src/components/ui/icon-button.tsx:11-41`. The helper's no-op fallback and Sidebar suppressions are limitations/risk; there is no claim of global compliance. | `frontend`, `tools`, and `tests`: preserve accessible names, keyboard/focus behavior, and real axe/browser validation. |
| Helm environments are linted/rendered as base-plus-overlay combinations with NetworkPolicy, immutable-image, and PDB assertions. | `.github/workflows/helm-validate.yml:35-63`, `infrastructure/helm/knowledge-graph-analytics/tests/networkpolicy_render_test.sh:13-104`, `infrastructure/helm/knowledge-graph-analytics/tests/backend_image_digest_render_test.sh:1-66`, and `infrastructure/helm/knowledge-graph-analytics/tests/pdb_render_test.sh:1-46`. Helm/Docker tool and config-validation attempts were `BLOCKED` by unavailable tools; cluster execution/apply/deploy operations were intentionally `NOT RUN`. | `infrastructure`, `deployment`, `config`, and `monitoring`: preserve consumer-aware overlays and render checks; do not apply to a cluster without authorization. |

## Review risks

These candidates are intentionally not counted as additional confirmed
findings. Each has a concrete follow-up requirement.

| Candidate | Current classification | Required follow-up evidence |
| --- | --- | --- |
| `backend/src/auth/ab_testing_auth.py:117-190` (`RR-001`) catches JWT and broad exceptions and puts their text in `HTTPException.detail`. | Review risk: the helper's references are confined to this unregistered module; no active route or middleware consumer was found. | Prove route/middleware registration before claiming reachable disclosure; if registered, sanitize unexpected details, log internally, and add non-disclosure tests. |
| `backend/src/services/infrastructure/feature_flags.py:80-84` (`RR-002`) resolves `../../feature-flags/launchdarkly-config.json` to `backend/src/feature-flags/...`, not the tracked root config. | Review risk: production code only re-exports the class; the sole concrete consumer found is an isolated unit test, so mock/development impact is unproven. | Prove runtime registration/consumer, then resolve and test the real repository-relative path before calling the root config live. |
| `frontend/src/components/layout/Sidebar.tsx` and its a11y test (`RR-003`) contain unlabeled landmarks/icon-only collapsed links and disabled axe rules. | Review risk: the active dashboard uses `SidebarLayout`/`AppRail`; the defective Sidebar is reached only through otherwise unreferenced legacy `AppLayout` and tests. | Prove whether `AppLayout` is loaded by any active route/build entry; if so, fix names, remove suppressions, and run focused/browser accessibility checks. |
| `frontend/src/lib/thread-hooks.ts:1,149,210` and `frontend/src/lib/analytics.ts:269` (`RR-004`) retain Tambo-named/type references after the owner removed Tambo guidance. | Review risk: application code and dependencies were intentionally left unchanged; runtime reachability and dependency-removal impact were not established. | Trace imports and runtime consumers, inspect the dependency manifests, and remove or replace the references only in a separately authorized code/dependency cleanup. Do not claim these references are dead or safe to delete from this documentation pass. |
| `frontend/trigger.config.ts:21` declares `dirs: ["./src/trigger"]` relative to `frontend`, while the root `trigger.config.ts:21` and root `tsconfig.json:14` load `src/trigger/`; `frontend/src/trigger/example.ts:1-12` is the only tracked child example. | Ambiguous Trigger.dev reachability and likely stale/duplicate configuration. | Prove which config the deployed Trigger command consumes, then either align the loader path or document the intentionally separate package. Do not claim root `src/trigger` is deployed from the frontend config without that proof. |
| `src/trigger/_lib/backend-client.ts:1-120` centralizes bounded retries, optional service authorization, and response schemas for the root Trigger jobs; user-scoped agent jobs pass a user bearer token at `src/trigger/agent/execute-agent.ts:87-125` and the backend endpoints remain the authorization boundary. | Preventative pattern with deployment/reachability risk, not a confirmed vulnerability. | Verify each registered task is loaded by the root config, each write is idempotent/HITL-safe, and logs/metadata never contain access or service-role tokens. |
| `frontend/lint_output.txt` is tracked; `.env`-named example/test files are tracked under `backend`, `config/environments`, and `tests/e2e`. | Path-only tracked-artifact/config risk. | Identify whether the lint output is consumed; remove or replace it only through an approved history/ownership decision. Confirm each environment file contains sanitized test/example material without opening values in an audit. |
| Numerous duplicate basenames occur across `deployment`, `infrastructure`, `config`, and `monitoring` (charts, values, Compose/Prometheus/Grafana files). Root Compose symlinks point to `config/docker-compose/`; live dev Argo CD is documented at `infrastructure/argocd/applications/dev.yaml`, while staging/production material remains in the tree. | Duplicate configuration and historical/live-status risk; the symlinks themselves are intentional. | Trace every change to its workflow/consumer, render the relevant base-plus-overlay combination, and reconcile the older deployment README/workflows with `docs/engineering/gotchas.md:9-10` before changing status claims. |
| `scripts/test_arxiv_cli_neo4j.py:12-17` uses `subprocess.run(..., shell=True)` with hardcoded test commands and a fixed local path. | Rejected false positive for arbitrary execution: test-only, constant command strings, no user-controlled input, and not an application path. | If promoted to a reusable/production tool, replace it with an argument list and an explicit configurable working directory. |
| `scripts/backup/disaster_recovery.py:37,396-409,429-430` and `scripts/setup_databases.py:43,137-182,217-218` interpolate SQL identifiers into DDL. | Rejected false positive for SQL injection in inspected paths: identifiers pass `safe_identifier`; values are parameterized or quoted for the intended setup operation. | Keep the validator and parameterization when editing; add focused tests if new identifier inputs are introduced. |
| `backend/src/services/sandbox/e2b_sandbox_manager.py:125-225` constructs subprocess calls inside an E2B sandbox; `backend/src/services/processing/video_processing_service.py:105,334,410` uses argument-list `subprocess.run` without shell parsing. | Rejected false positive for host arbitrary execution. The sandbox is the explicit execution boundary and package names are filtered; video calls use argv lists. | Preserve the sandbox boundary, package allowlist, timeout, and no-shell argument form. |
| `frontend/src/test/a11y.ts:5-37` has a no-op fallback when `jest-axe` is unavailable; many typing/lint suppressions are documented, and workflow `continue-on-error` lanes are explicitly advisory at `.github/workflows/test-pipeline.yml:139,191,262-327,629,668`. | Review risk, not a new suppression finding at this SHA. | Make the accessibility helper fail closed in CI or assert dependency presence, and keep each suppression tied to a narrow rationale/removal condition. Do not call advisory lanes blocking. |
| Service-, credential-, browser-, Docker-, Helm-, cluster-, and Harbor-dependent paths were not exercised. | Service-dependent validation gap. | Run only in the authorized environment with the required pinned tooling and record the result separately from this credential-free baseline. |

## Owner correction amendment (2026-09-14)

The original audit results, classifications, and point-in-time command outputs
above are preserved. The owner later confirmed that Tambo AI is no longer used
and authorized a bounded documentation correction: the stale marker and
completed Tambo section were removed from `frontend/AGENTS.md`, while its
Context7 section was preserved unchanged. This later decision supersedes the
historical Tambo-preservation requirement in the approved design and plan; it
does not claim that the remaining source references are dead or safe to delete.

The unresolved references at
`frontend/src/lib/thread-hooks.ts:1,149,210` and
`frontend/src/lib/analytics.ts:269` are recorded above as a review risk.
Application code and dependencies were intentionally not changed because
runtime reachability and dependency-removal impact were not established. A
separate authorized follow-up must trace consumers and manifests before
cleanup.

The corrected frontend verification guidance now prefers
`scripts/ci/run_local_ci.sh --base "$BASE" --frontend`. The wrapper creates a
fresh temporary ESLint JSON report with `mktemp`, invokes the comparators
directly without an extra pnpm `--` separator, and cleans the report afterward.
The original acceptance rows documenting the invalid bare
comparator invocation and its blocked result remain unchanged as historical
evidence; this amendment does not convert them into a pass or claim that the
frontend toolchain was exercised. The wrapper does not run frontend unit tests;
the separate `pnpm --dir frontend test` command remains required.

## Root coverage and guidance mapping

The following table is the handoff contract for the 20 child guides. `IP-*`
refers to confirmed findings above and `RR-*` to held review risks; invariant
labels refer to the enforced invariant table. The final column describes
durable rule themes, not new production guarantees.

Task 6's exact-set check confirmed that the mapping is implemented by these
20 paths, one per selected root: `backend/AGENTS.md`, `brand/AGENTS.md`,
`config/AGENTS.md`, `data/AGENTS.md`, `database/AGENTS.md`,
`deployment/AGENTS.md`, `docs/AGENTS.md`, `evals/AGENTS.md`,
`feature-flags/AGENTS.md`, `frontend/AGENTS.md`, `infrastructure/AGENTS.md`,
`memory/AGENTS.md`, `monitoring/AGENTS.md`, `notebooks/AGENTS.md`,
`scripts/AGENTS.md`, `specs/AGENTS.md`, `src/AGENTS.md`, `supabase/AGENTS.md`,
`tests/AGENTS.md`, and `tools/AGENTS.md`. The repository-root `AGENTS.md` is
unchanged and remains the parent policy.

| Root | Evidence reviewed | Findings/invariants | Intended guidance themes |
| --- | --- | --- | --- |
| `backend` | `docs/engineering/backend.md`, `api-contracts.md`, `testing.md`, `gotchas.md`; API/services/tests; CI and OpenAPI scripts. | IP-001, IP-002, RR-001; router/access/generated/migration/ratchet invariants. | Router → service → transaction boundaries; membership vs organization scope; ancestor soft-delete checks; safe public errors; bound SQL/validated enums; compatibility exports; generated contract workflow. |
| `brand` | Tracked SVG/HTML/source assets, screenshots, DOCX, and root `README.md` consumer. | No IP; artifact review risk. | Preserve source/rendered artifacts, provenance, accessibility/identity metadata, and consumer-aware visual review; no bulk binary overwrite. |
| `config` | `config/docker-compose/`, root Compose symlinks, manifests, workflow consumers, and environment-path inventory. | Duplicate-config risk; Compose/render invariant. | Edit canonical targets once; preserve layering and symlinks; keep environment values/secrets indirect; do not silently diverge CI/prod defaults. |
| `data` | `data/datasets/` inventory and referenced evaluation/test consumers. | No IP; artifact/provenance risk. | Preserve provenance and schemas; do not mutate fixtures to hide failures or commit customer/sensitive data; require named consumer for replacement. |
| `database` | Standalone SQL/Cypher/JSON assets plus Alembic/Supabase references and dynamic SQL candidates. | SQL false-positive review; migration/tenant invariants. | Distinguish standalone assets from live migration systems; parameterize/allowlist SQL; preserve tenant predicates, ordering, transactions, and rollback reasoning; authorize applies. |
| `deployment` | Legacy Helm/Kubernetes tree, deployment workflows, and retirement notes in `gotchas.md`. | Duplicate/live-status risk; Helm invariant. | Treat legacy paths as non-live until proven; require consumer/reachability checks; validate charts read-only; no deploy/apply/rollback without authorization. |
| `docs` | Engineering index, dated audits/reports/plans/specs, directory-doc tooling, and historical workflow docs. | Documentation contradiction risk; directory-doc invariant. | Keep canonical vs historical hierarchy clear, links/paths real, dated records immutable, and old status/commands labeled. |
| `evals` | `evals/README.md`, baseline/manifests, task specs/verifiers, and judge-separation text. | Artifact/provenance risk; isolation invariant. | Keep task digests/baselines immutable, isolate judge credentials, preserve fixtures, and never score infrastructure failure as reward zero. |
| `feature-flags` | `launchdarkly-config.json`, backend loader, enum/default tests, and frontend flag consumer. | RR-002. | Prove runtime consumer, align keys/enums/defaults/rollouts, keep configs secret-free, and test relative paths before calling config live. |
| `frontend` | `docs/engineering/frontend.md`, `api-contracts.md`, `testing.md`, UX audit, WCAG checklist, components/tests, and package scripts. | RR-003; frontend/ratchet/generated/accessibility invariants. | One cache owner; chat-store facade; backend-only writes/reconciliation; adapter/generated-type rules; ratchets only tighten; accessible names/focus; Node 24/pnpm/root lockfile. |
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

1. Sanitize IP-001 at the registered admin HTTP boundary, retain internal
   structured exception logging, and add a focused non-disclosure test.
2. Fix IP-002 by carrying organization identity through status producers or
   requiring a preloaded scoped document, enforcing organization/live-row
   predicates, and testing direct/queued foreign and deleted IDs.
3. Prove RR-001's route or middleware consumer before treating its auth helper
   exception text as reachable; sanitize and test it only if registration is
   established.
4. Prove RR-002's runtime feature-flag consumer and registration, then align
   the relative path and add a real-layout test before calling the config live.
5. Prove RR-003's legacy `AppLayout` reachability; only if active, fix the
   rendered Sidebar, remove the disabled axe rules, and run focused/browser
   accessibility checks in an installed Node 24 environment.
6. Resolve the Trigger config relationship and duplicate/stale configuration
   risks with consumer evidence; separately decide the fate of tracked
   `frontend/lint_output.txt` and environment test/example artifacts. Do not
   delete or rewrite historical material as a side effect of this audit.
7. Restore the pinned local validation prerequisites (Python/Alembic/backend
   dependencies and frontend Node 24/frozen install) before treating the
   blocked checks as a usable baseline. This documentation change itself fixes
   only missing guidance; it does not remediate production code.

## Limitations

- Textual searches miss dynamically constructed paths, SQL, subprocess
  arguments, generated code, runtime registration, and browser-only behavior.
  They are candidate discovery, not complete static analysis.
- The required Alembic/OpenAPI checks were not runnable with the exact
  commands because the `python` alias is absent; their `python3` diagnostics
  were blocked by missing `alembic`/`langgraph`. The final architecture/CI,
  API/thread, and service/thread suites were blocked during collection by
  missing `langgraph`/`spacy`; they are not behavioral passes or failures.
- The frontend changed-lint wrapper was not run through the CI-provided report
  setup and the local runtime is Node 22 rather than the declared Node 24;
  only the tsconfig-exclusion ratchet passed directly. Frontend type-check and
  unit tests could not find `tsc`/`vitest` because `node_modules` is absent.
- Docker was present but its Compose plugin was unavailable, and Helm,
  Playwright, and gitleaks were not installed. Their invoked preflights are
  `BLOCKED`; the actual gitleaks scan and all browser/hosted-service/
  deployment/cluster operations were intentionally `NOT RUN`. No service
  startup, database, Redis, browser, Kubernetes, Supabase, LaunchDarkly,
  Harbor, credential, cluster, or live webhook operation was attempted. The
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
