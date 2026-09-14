# Invalid-pattern Audit and Agent Guidance Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Produce a point-in-time, evidence-based invalid-pattern audit and concise directory-scoped coding guidance for exactly the 20 tracked top-level project roots selected by the design.

**Architecture:** Keep historical evidence in one central audit and durable editing rules in nested `AGENTS.md` files. Each child guide inherits the repository root policy, links to the closest source of truth, records only directory-specific hazards and workflows, and names only validation commands already backed by repository manifests, workflows, scripts, or engineering docs.

**Tech Stack:** Markdown, Git, POSIX shell, Python 3 standard library, ripgrep, existing repository linters and test/validation commands.

**Spec:** `docs/superpowers/specs/2026-09-14-invalid-patterns-agent-guidance-design.md`

## Global Constraints

- Create `docs/audits/2026-09-14-invalid-patterns-audit.md` and one top-level `AGENTS.md` in each of these exact roots: `backend`, `brand`, `config`, `data`, `database`, `deployment`, `docs`, `evals`, `feature-flags`, `frontend`, `infrastructure`, `memory`, `monitoring`, `notebooks`, `scripts`, `specs`, `src`, `supabase`, `tests`, and `tools`.
- Leave the repository-root `AGENTS.md` unchanged.
- Revise `frontend/AGENTS.md` in place and retain its Tambo marker, Tambo documentation and CLI notes, and Context7 instructions.
- Do not create scoped guides in hidden/runtime directories or in the flat roots `daily-logs`, `nginx`, `projects`, or `security`.
- Every guide is standalone Markdown with compact `Scope and sources of truth`, `Invalid patterns`, `Required workflow`, and `Verification` sections where applicable.
- Child guidance may narrow inherited rules but must not weaken security, authorization, evidence, or verification requirements.
- Do not duplicate the root policy verbatim; link to canonical engineering contracts instead of paraphrasing nuanced rules that could drift.
- Do not invent commands, ownership boundaries, deployment status, or guarantees. A command included in guidance must be traceable to a manifest, Makefile, workflow, script, or canonical engineering document.
- Findings must be based on inspected repository evidence. Text matches are candidates until their call path, consumer, and context have been checked.
- Classify audit entries as `confirmed finding`, `enforced invariant`, or `review risk`; counts are point-in-time observations tied to the audited commit.
- Do not call legacy debt newly introduced by the current branch.
- Do not expose environment-file contents, trace payloads, credentials, tokens, or other potentially sensitive data. It is sufficient to name a tracked path and the kind of risk.
- Do not change production behavior or run broad auto-fixes.
- Do not hand-edit `backend/openapi.json` or `frontend/src/types/generated/api.d.ts`.
- Keep historical specs, baselines, screenshots, datasets, migration files, and recorded evaluation evidence immutable unless their local guide names an explicit replacement or regeneration workflow.
- For service-, credential-, cluster-, or browser-dependent checks, label the dependency and report `NOT RUN` with the reason rather than implying success.

---

## File Structure

| File | Responsibility |
| --- | --- |
| `docs/audits/2026-09-14-invalid-patterns-audit.md` | Audit snapshot, method, evidence, classified findings/invariants/risks, root coverage, priorities, and limitations. |
| `backend/AGENTS.md` | Backend router/service/transaction, tenancy, exception, SQL, migration, compatibility, and generated-contract rules. |
| `brand/AGENTS.md` | Brand source assets, rendered screenshots, binary-document preservation, and consumer-aware asset updates. |
| `config/AGENTS.md` | Canonical Compose targets, root symlink relationship, layered config, and secret-safe environment-file handling. |
| `data/AGENTS.md` | Dataset/fixture provenance, immutability, schema stability, and sensitive-data constraints. |
| `database/AGENTS.md` | Standalone SQL asset status, migration history, schema-source boundaries, transactional SQL, and rollback expectations. |
| `deployment/AGENTS.md` | Legacy deployment tree boundaries, no unapproved deploy/apply actions, and chart/workflow validation. |
| `docs/AGENTS.md` | Canonical-vs-historical documentation hierarchy, link/path accuracy, immutable records, and directory-doc lint. |
| `evals/AGENTS.md` | Harbor task isolation, digest/source-manifest integrity, baseline immutability, judge separation, and honest scoring. |
| `feature-flags/AGENTS.md` | Flag-key/default consistency, consumer verification, no secrets, and prototype/live-boundary checks. |
| `frontend/AGENTS.md` | Existing Tambo/Context7 notes plus frontend state ownership, generated types, quality ratchets, accessibility, and verification. |
| `infrastructure/AGENTS.md` | Live ArgoCD/Helm ownership, environment overlays, secret indirection, immutable image identity, and render tests. |
| `memory/AGENTS.md` | Non-authoritative contextual notes, privacy, dated claims, and source-of-truth cross-checking. |
| `monitoring/AGENTS.md` | Local monitoring configuration ownership, duplicate-file disambiguation, secret handling, and config validation. |
| `notebooks/AGENTS.md` | Exploratory status, output/data preservation, external-service caution, and reproducibility notes. |
| `scripts/AGENTS.md` | Script safety, destructive/remote action boundaries, shell construction, temporary files, and script contract tests. |
| `specs/AGENTS.md` | Historical feature records and contracts, immutability, and separation from live OpenAPI behavior. |
| `src/AGENTS.md` | Root Trigger.dev task status, shared client/schema boundaries, token/logging safety, retry/idempotency, and consumer verification. |
| `supabase/AGENTS.md` | Ordered append-only migrations, RLS/tenant isolation, auth-template/config boundaries, and migration checks. |
| `tests/AGENTS.md` | Test-layer selection, fixture fidelity, mutation verification, service dependencies, and disruptive-load-test authorization. |
| `tools/AGENTS.md` | Standalone tool boundaries, authenticated Playwright artifacts, explicit user interaction, and discovery checks. |

## Audit Classification and Evidence Contract

Use the following schema consistently in the audit:

- A **confirmed finding** has an ID (`IP-001`, increasing sequentially), severity (`critical`, `high`, `medium`, or `low`), exact path and line range at the audited commit, observed behavior, impact, evidence/validation, affected root, and a practical next action. `Critical` means an immediately reachable credential disclosure, arbitrary execution, destructive data loss, or cross-tenant compromise; `high` means a reachable security/isolation failure or severe production correctness failure; `medium` means a concrete but narrower correctness, operability, or maintainability hazard; `low` means localized debt with bounded impact.
- An **enforced invariant** names the safe behavior, its enforcing test/hook/workflow, and the directory guidance that preserves it. An invariant is not counted as open debt.
- A **review risk** names the candidate evidence, what remains unproven, and the exact follow-up needed. Ambiguous duplicate configuration, unverified consumers, historical examples, and service-dependent checks belong here until reachability is proved.
- A text match alone is never evidence of exploitability or a missing tenant filter. Inspect the enclosing function, caller or route registration, data origin, parameterization, and existing tests before classifying it.
- For audit `rg` scans, exit status 0 means candidates were found, 1 means no candidates were found, and 2 or greater is a command error. A no-match result is evidence to record, not a failed check.
- Tests, fixtures, generated artifacts, archived docs, examples, migrations, allowlisted constants, and intentionally advisory CI lanes are not violations merely because they match a pattern.
- Record the audited Git SHA and working-tree state. If unrelated changes are present, identify their paths and do not attribute them to this work.

## Per-root Content Matrix

The task steps below repeat the relevant requirements, but this matrix is the final cross-check for root coverage and command provenance.

| Root | Sources of truth and required invalid-pattern guidance | Verification text allowed in the guide |
| --- | --- | --- |
| `backend` | Link `docs/engineering/backend.md`, `docs/engineering/api-contracts.md`, `docs/engineering/testing.md`, and `docs/engineering/gotchas.md`. Preserve router -> service -> transaction ownership, the membership-based workspace model, organization-scoped document/dedup/suggestion queries, soft-delete ancestor checks, compatibility exports/flags, parameterized SQL and validated enums, safe public errors with internal logging, and generated OpenAPI/type workflow. | `ruff check backend/src`; `pytest -q backend/tests/unit/architecture backend/tests/unit/api`; `pytest -q backend/tests/unit/services/threads backend/tests/api/threads`; `python scripts/ci/generate_openapi.py --check`. |
| `brand` | Treat SVG/HTML sources, screenshots, and the DOCX as reviewed brand artifacts; do not bulk regenerate or overwrite binaries, strip SVG accessibility/identity data, or update screenshots without identifying their consumer and capture provenance. Root `README.md` is a confirmed screenshot consumer. | State that no root-local automated validator is defined; require targeted rendering/visual review plus `git diff --check` for text assets, without claiming binary review from a text diff. |
| `config` | Identify `config/docker-compose/*.yml` as targets of the root Compose symlinks. Do not edit both the target and symlink as separate copies, flatten layered environment overrides, expose tracked environment-file values, hard-code secrets, or make CI/prod defaults silently diverge. | `docker compose -f config/docker-compose/docker-compose.development.yml config --quiet`; `bash infrastructure/tests/docker_compose_development_config_test.sh`. Label Docker availability honestly. |
| `data` | Treat `data/datasets/` as sample/reference input with provenance and stable consumer-facing schemas. Do not rewrite datasets to make a test pass, commit sensitive/customer data, or bulk-normalize files without a named consumer and reproducible replacement. | State that no root-local validator exists; require the named consumer's test and review of size/schema/provenance before replacement. |
| `database` | Distinguish standalone/historical SQL assets from live `backend/alembic/` and `supabase/migrations/`. Do not infer deployment from filenames, reorder or rewrite migration history, interpolate untrusted SQL, omit tenant predicates where the schema is tenant-owned, or apply/drop against a real database during routine editing. | State that there is no `database/`-local gate; for related Alembic work use `(cd backend && python ../scripts/ci/check_alembic.py)`, and label database execution probes as service-dependent. |
| `deployment` | Link `docs/engineering/gotchas.md`; mark the retired staging/production and legacy non-ArgoCD material accurately, and require consumer/reachability checks before changes. Never run deploy, apply, rollback, or secret-bearing commands without explicit environment authorization. | `helm lint deployment/helm/rag-system/` is present in `deployment/github-actions/workflows/deploy.yml`; label Helm as required and do not imply this legacy chart is live. |
| `docs` | Link `docs/engineering/README.md` as the current engineering index. Treat `docs/archive/`, audits, dated reports, plans, baselines, and approved specs as historical records; do not silently rewrite history or present old commands/status as current. Require relative links and real paths. | `make docs-lint` or `python3 scripts/docs/check_dir_docs.py`; note that this existing lint covers tracked `README.md`/`doc.md`, not `AGENTS.md`. |
| `evals` | Link `evals/README.md` and `evals/AGENT_FLOW_BASELINE.md`. Preserve task/environment/verifier isolation, recorded baselines, calibration/truth fixtures, and `source-manifests` digests. Keep judge credentials only in verifier configuration; never score infrastructure failure as reward zero or reuse scores after a digest/revision change. | State that Harbor runs require pinned tooling, images, services, and credentials. Name `evals/<task>/tests/test.sh` only as an in-environment verifier, not a generic host-side check. |
| `feature-flags` | Require a verified runtime consumer before calling the root config live. Keep external keys, enum values, defaults, rollout rules, and frontend/backend expectations aligned; never commit provider SDK keys or treat an example as production policy. Explicitly investigate the relative path in `backend/src/services/infrastructure/feature_flags.py` rather than assuming it reaches this root. | State that no root-local validator exists; require focused consumer tests once the consumer is established. |
| `frontend` | Preserve all existing Tambo/Context7 material. Link `docs/engineering/frontend.md`, `docs/engineering/api-contracts.md`, and `docs/engineering/testing.md`; consult `docs/frontend/ux-design-audit.md` and `scripts/wcag-checklist.sh` as dated accessibility evidence, not proof of current compliance. Enforce one server-state owner, `@/store/chat-store` facade imports, backend-only persisted-chat writes, terminal reconciliation, no hand edits to generated API types, adopt-on-touch aliases, ratchets that only tighten, accessible names on icon-only controls, keyboard/focus behavior, and repository-pinned Node/pnpm. | `pnpm --dir frontend lint:changed`; `pnpm --dir frontend quality:exclusions`; `pnpm --dir frontend type-check`; `pnpm --dir frontend test`; label `pnpm --dir frontend test:e2e:accessibility` browser/service-dependent. |
| `infrastructure` | Link `.github/workflows/README.md` and `docs/engineering/gotchas.md`. Treat `infrastructure/argocd/applications/dev.yaml` plus the knowledge-graph Helm chart as the live dev path; staging/production values remain non-live unless repository evidence changes. Preserve base-plus-overlay rendering, secret indirection, full-SHA/digest image identity, NetworkPolicy scope, and PDB environment differences. Do not apply to a cluster without authorization. | The exact `helm lint`/`helm template` loop and three render scripts from `.github/workflows/helm-validate.yml`; `bash infrastructure/tests/docker_compose_development_config_test.sh`. Label Helm/Docker dependencies. |
| `memory` | Treat notes as contextual, dated, and potentially personal—not authoritative engineering contracts. Do not copy personal data or secrets into audits/guidance; cross-check technical claims against code and `docs/engineering/`, and date any new observation. | State that no automated validator exists; require source cross-check and sensitive-content review. |
| `monitoring` | Link `monitoring/README.md`; distinguish local Compose/Prometheus/Grafana/OTel files from deployment and infrastructure copies by consumer. Do not synchronize same-named configs blindly, commit `.env`, expose webhook/auth values, or change scrape/alert labels without checking dashboards and rules. | `bash scripts/validate_docker_compose.sh`; label Docker availability and note that configuration rendering does not prove a live scrape or alert. |
| `notebooks` | Treat notebooks, plots, and sample documents as exploratory/recorded artifacts. Do not bulk-reformat notebook JSON, erase outputs or overwrite plots without an explicit reproducible regeneration, embed credentials, or claim scripts are production paths. | State that no root-local deterministic validator exists; require notebook-specific rerun instructions and recorded environment/data dependencies when a notebook is intentionally changed. |
| `scripts` | Distinguish CI/static scripts from operational, deployment, backup, and data scripts. Read the whole script before execution; require explicit authorization for destructive, remote, secret, or shared-environment actions. Avoid unsafe shell construction, unvalidated broad paths, secret echoing, and unguarded partial failure; use safe temporary paths and cleanup. | `python3 -m pytest tests/unit/scripts/ --confcutdir=tests/unit/scripts -q -p no:cacheprovider --no-cov`; `make docs-lint` for documentation tooling. |
| `specs` | Treat numbered feature specs, plans, checklists, and contract snapshots as historical design records rather than the live API. Do not rewrite completed history, infer implementation from checked boxes, or edit snapshot OpenAPI to change production behavior; link forward to newer decisions when superseding. | State that no root-local automated validator exists; live API changes use `python scripts/ci/generate_openapi.py --check` against FastAPI instead. |
| `src` | Identify this root as Trigger.dev task code and verify how it is loaded before claiming it is deployed; current `frontend/trigger.config.ts` points at `./src/trigger` relative to `frontend`. Centralize outbound requests in `_lib/backend-client.ts` and validation in `_lib/schemas.ts`; do not log access/service-role tokens, trust caller-supplied user identity without backend authorization, duplicate retries/writes, or bypass task/HITL idempotency. | State that the root manifest exposes `pnpm trigger:dev`/`pnpm trigger:deploy` but no isolated offline validator for this tree; both Trigger commands are configured-service operations and must not be presented as routine checks. |
| `supabase` | Treat timestamped migrations as ordered, append-only history; preserve RLS, policy, function `search_path`, and tenant isolation. Do not edit an applied migration in place, weaken RLS for convenience, store credentials in `config.toml`, or treat auth templates as permission checks. | `scripts/ci/run_local_ci.sh` is the repository-backed aggregate gate that includes the static Alembic check and backend unit suite; require focused RLS tests when changing a policy, and label any Supabase CLI reset/push as service-dependent and potentially destructive rather than routine validation. |
| `tests` | Link `docs/engineering/testing.md`. Put tests in the correct layer, preserve realistic fixtures and negative authorization cases, and prove race/idempotency guards by mutation. Do not weaken assertions, rewrite fixtures, add retries, or lower floors merely to make a failure disappear. Require explicit announcement/authorization before `tests/load/run-shared-dev-max.sh --full`. | `pytest -q backend/tests/unit/ci backend/tests/unit/architecture`; `pnpm --dir tests/e2e exec playwright test --project=chromium`; label browser/service dependencies and distinguish legacy root suites from backend tests. |
| `tools` | Link `tools/nous-playwright/README.md`. Keep the standalone package separate from the root workspace, never commit `.auth/`, traces, videos, reports, or workspace contents, and do not launch interactive auth or mutate live projects without user authorization. | `(cd tools/nous-playwright && pnpm test:list)` for discovery only; label installation/toolchain requirements and do not claim it runs the authenticated flow. |

### Task 1: Establish the evidence baseline and write the central audit

**Files:**
- Create: `docs/audits/2026-09-14-invalid-patterns-audit.md`
- Read: `AGENTS.md`
- Read: `docs/superpowers/specs/2026-09-14-invalid-patterns-agent-guidance-design.md`
- Read: `docs/engineering/{README,backend,frontend,testing,api-contracts,gotchas}.md`
- Read: `.github/workflows/{test-pipeline,helm-validate,workflow-lint,secret-scan}.yml`
- Read: `.pre-commit-config.yaml`
- Read: `Makefile`, `package.json`, `frontend/package.json`, `backend/Makefile`, `evals/README.md`, `monitoring/README.md`, `tools/nous-playwright/README.md`
- Read: `docs/security/PRE_PUBLIC_SECURITY_AUDIT_REMEDIATION.md`, `docs/frontend/ux-design-audit.md`, `scripts/wcag-checklist.sh`

**Interfaces:**
- Consumes: approved design and current repository state at one recorded Git SHA.
- Produces: the classification vocabulary, evidence IDs, root-by-root coverage record, and remediation priorities that every child guide must map back to.

- [ ] **Step 1: Record a safe repository snapshot**

Run from the repository root:

```bash
AUDIT_BASE_SHA="$(git rev-parse HEAD)"
printf 'audited_sha=%s\n' "$AUDIT_BASE_SHA"
git status --short
git ls-files | awk -F/ 'NF>2 && $1 !~ /^\./ {print $1}' | sort -u
```

Expected: the SHA is recorded; working-tree changes are listed by path only; the final command includes the 20 selected roots plus any flat roots that the design intentionally excludes. Do not print file contents from environment, trace, auth, or credential paths.

- [ ] **Step 2: Prove the exact scoped-root set before drafting guidance**

Run:

```bash
python3 - <<'PY'
import subprocess

expected = {
    "backend", "brand", "config", "data", "database", "deployment",
    "docs", "evals", "feature-flags", "frontend", "infrastructure",
    "memory", "monitoring", "notebooks", "scripts", "specs", "src",
    "supabase", "tests", "tools",
}
tracked = subprocess.check_output(["git", "ls-files"], text=True).splitlines()
actual = {
    path.split("/", 1)[0]
    for path in tracked
    if "/" in path and not path.startswith(".") and path.count("/") >= 2
}
assert expected == actual, (sorted(expected - actual), sorted(actual - expected))
print("scoped roots match the 20 tracked non-hidden roots with descendants")
PY
```

Expected: the success sentence. If the repository topology changed after this plan was written, stop and reconcile the design rather than silently adding or dropping a guide.

- [ ] **Step 3: Inventory contracts, consumers, generated files, and existing gates**

Run these read-only searches:

```bash
rg -n "AGENTS\.md|docs-lint|lint:changed|quality:exclusions|generate:api-types|check:api-types|check_alembic|helm lint|actionlint|gitleaks" \
  AGENTS.md Makefile package.json frontend/package.json .pre-commit-config.yaml \
  .github scripts docs/engineering

rg -n "config/docker-compose|infrastructure/argocd|deployment/helm|monitoring/|feature-flags/|launchdarkly-config|source-manifests" \
  .github scripts backend frontend README.md docs/engineering evals/README.md \
  --glob '!frontend/lint_output.txt'

rg -n "gitleaks|credential|tenant|organization_id|WCAG|aria-label|keyboard|focus" \
  docs/security/PRE_PUBLIC_SECURITY_AUDIT_REMEDIATION.md \
  docs/frontend/ux-design-audit.md scripts/wcag-checklist.sh \
  .github/workflows/secret-scan.yml frontend/app frontend/src

git ls-files backend/openapi.json frontend/src/types/generated evals/source-manifests \
  'backend/alembic/versions/**' 'supabase/migrations/**'
```

Expected: enough evidence to name sources and consumers without guessing. Record contradictions between current workflows/code and older READMEs as review risks or confirmed documentation/configuration findings; do not silently choose the older prose.

- [ ] **Step 4: Run deterministic, credential-free repository checks**

Run each command independently so one failure does not hide later evidence:

```bash
python3 scripts/docs/check_dir_docs.py
ruff check backend/src
(cd backend && python ../scripts/ci/check_alembic.py)
python scripts/ci/generate_openapi.py --check
pytest -q backend/tests/unit/architecture backend/tests/unit/ci
pnpm --dir frontend lint:changed
pnpm --dir frontend quality:exclusions
```

Expected: record command, exit status, and concise result in the audit. These commands need no production services or credentials, but local tool/dependency absence is possible; record that as `NOT RUN` with the missing prerequisite and do not install from the network merely to make this documentation audit look complete.

- [ ] **Step 5: Search for exception disclosure, dynamic SQL, and unsafe process construction**

Run:

```bash
rg -n --glob '*.py' --glob '!**/tests/**' --glob '!**/examples/**' \
  "detail\\s*=\\s*str\\(|detail\\s*=\\s*f['\"].*\\{(e|exc|error)\\}" backend/src

rg -n --glob '*.py' --glob '!**/tests/**' --glob '!**/examples/**' \
  "(execute|text)\\s*\\(\\s*f['\"]|\\.execute\\([^\\n]*(%|\\.format\\()" backend/src scripts

rg -n --glob '*.py' --glob '*.ts' --glob '*.tsx' --glob '*.js' --glob '*.mjs' \
  --glob '!**/tests/**' --glob '!**/examples/**' \
  'shell\s*=\s*True|os\.system\(|subprocess\.(run|Popen|call|check_call|check_output)\(|execSync\(|child_process' \
  backend/src frontend/src src scripts feature-flags
```

Expected: candidate locations, not automatic findings. For exception strings, distinguish expected user/domain errors from internal exception disclosure. For SQL, prove whether values are bound or allowlisted. For processes, trace the argument source and whether shell parsing occurs.

- [ ] **Step 6: Search tenant scope, suppressions, and accessibility candidates**

Run:

```bash
rg -n --glob '*.py' --glob '!**/tests/**' \
  'select\(Document|query\(Document|Document\.(id|checksum_sha256|title)|content_hash|search_suggestion' \
  backend/src/api backend/src/services

rg -n --glob '*.py' --glob '*.ts' --glob '*.tsx' --glob '*.js' --glob '*.mjs' \
  --glob '*.yml' --glob '*.yaml' --glob '!**/tests/**' --glob '!**/generated/**' \
  '#\s*noqa|#\s*type:\s*ignore|@ts-ignore|@ts-nocheck|eslint-disable|continue-on-error:\s*true' \
  backend/src frontend/src src scripts .github infrastructure deployment config monitoring feature-flags

rg -n '<IconButton|aria-label|aria-labelledby' frontend/app frontend/src
```

Expected: manually inspect the full enclosing operation. A tenant finding requires proof that a tenant-owned operation lacks an equivalent organization/access predicate; a suppression finding requires proof that the directive hides a relevant problem rather than documented legacy debt; an accessibility finding requires inspection of the rendered control and props, not merely a missing string on one line.

- [ ] **Step 7: Search tracked artifacts and duplicated configuration without reading sensitive values**

Run:

```bash
git ls-files | rg '(^|/)(\.env($|\.)|.*\.(log|trace|sqlite3?|db|pyc)|coverage/|test-results/|playwright-report/|node_modules/|\.next/|__pycache__/|lint_output\.txt$|\.DS_Store$)'

git ls-files config database deployment infrastructure monitoring | awk -F/ '
  { base=$NF; count[base]++; paths[base]=paths[base] "\n    " $0 }
  END { for (base in count) if (count[base] > 1) print base paths[base] }
' | sort

find . -maxdepth 1 -type l -printf '%f -> %l\n' | sort
```

Expected: path-only candidate evidence. Treat example/test environment files, intentional datasets, migration history, and separate environment overlays according to their consumers. Do not open or quote potentially sensitive values in the audit. Duplicate basenames are review prompts, not proof of drift.

- [ ] **Step 8: Inspect high-signal candidate anchors and classify them**

At minimum, inspect the enclosing functions and consumers for these already-observed anchors:

```bash
sed -n '160,205p' backend/src/auth/ab_testing_auth.py
sed -n '120,155p' backend/src/api/infrastructure/workers.py
sed -n '70,105p' backend/src/services/infrastructure/feature_flags.py
sed -n '175,215p' backend/src/services/infrastructure/status_update_service.py
sed -n '1,120p' frontend/trigger.config.ts
sed -n '1,180p' src/trigger/_lib/backend-client.ts
git ls-files frontend/lint_output.txt config/environments backend/.env.test tests/e2e/.env.test
```

Expected: every anchor is placed into exactly one audit category with supporting reasoning, or explicitly rejected as a false positive. For the feature-flag and Trigger.dev path relationships, resolve relative paths from the consuming file/config and verify registration before claiming reachability.

- [ ] **Step 9: Write the audit identity, summary, scope, and method**

Create `docs/audits/2026-09-14-invalid-patterns-audit.md` with the title `# Invalid-pattern audit — 2026-09-14`, then add `## Executive summary` and `## Scope and method`. Record the audited SHA, exact included/excluded roots, counts by classification/severity, highest-priority actions, evidence sources, candidate-review method, and every deterministic command with `PASS`, `FAIL`, or `NOT RUN`. State the sensitive-value handling rule explicitly.

- [ ] **Step 10: Add classified evidence sections**

Add these sections:

1. `## Confirmed findings` — one subsection/table row per `IP-NNN` using the evidence contract above; if a severity has no finding, say so plainly rather than creating an empty issue.
2. `## Enforced invariants` — backend boundaries/tenancy, frontend ownership/ratchets, generated API contracts, migration checks, secret scanning, accessibility checks, and infrastructure render assertions only where a real test/hook/workflow was verified.
3. `## Review risks` — ambiguous reachability, duplicate configs, tracked artifacts, service-dependent areas, and exact follow-up evidence required.

Do not copy secrets, full exception payloads, large source excerpts, or environment-file contents. Use repository-relative path/line citations and concise paraphrases.

- [ ] **Step 11: Add coverage, priorities, and limitations**

Add these sections:

1. `## Root coverage and guidance mapping` — all 20 roots, evidence reviewed, classification IDs/invariants that apply, and the intended `AGENTS.md` rule themes.
2. `## Remediation priorities` — ordered, practical next actions; this documentation change itself fixes only missing guidance, not production code.
3. `## Limitations` — textual-search blind spots, checks not run, no service/credential/cluster testing, historical/live ambiguity, and point-in-time count caveat.

- [ ] **Step 12: Verify and commit the evidence baseline**

Run:

```bash
test -s docs/audits/2026-09-14-invalid-patterns-audit.md
rg -n '^## (Executive summary|Scope and method|Confirmed findings|Enforced invariants|Review risks|Root coverage and guidance mapping|Remediation priorities|Limitations)$' \
  docs/audits/2026-09-14-invalid-patterns-audit.md
git diff --check -- docs/audits/2026-09-14-invalid-patterns-audit.md
git diff -- docs/audits/2026-09-14-invalid-patterns-audit.md
```

Expected: the file is non-empty, all eight sections are present, whitespace validation is silent, and the diff contains evidence-backed prose only.

Commit:

```bash
git add docs/audits/2026-09-14-invalid-patterns-audit.md
git commit -m "docs: record invalid-pattern audit"
```

### Task 2: Add application-code guidance and preserve the frontend integration notes

**Files:**
- Create: `backend/AGENTS.md`
- Modify: `frontend/AGENTS.md`
- Create: `src/AGENTS.md`
- Create: `tests/AGENTS.md`
- Read: `docs/audits/2026-09-14-invalid-patterns-audit.md`
- Read: `docs/engineering/{backend,frontend,testing,api-contracts,gotchas}.md`

**Interfaces:**
- Consumes: audit classifications/invariants plus canonical backend, frontend, testing, API-contract, and gotcha contracts.
- Produces: scoped application and test rules that later validation maps to the audit and checks for consistency.

- [ ] **Step 1: Capture and verify the existing frontend material before editing**

Run:

```bash
sed -n '1,220p' frontend/AGENTS.md
rg -n 'tambo-docs-v1\.0|Tambo AI Framework|https://docs\.tambo\.co/llms\.txt|npx tambo|## Context7|context7' frontend/AGENTS.md
```

Expected: the existing marker, Tambo framework description/link/CLI note, and Context7 section/instruction are all visible. Keep their meaning and operational instructions intact in the revised file.

- [ ] **Step 2: Create `backend/AGENTS.md`**

Write the four standard sections. Include every `backend` requirement from the per-root matrix, and keep details precise:

- routers under `backend/src/api/threads/workspace_routes/` own transport only; services under `backend/src/services/threads/` own persistence and one transaction boundary per method;
- `workspace_access.py` is the access funnel, workspace access is membership/public/owner based rather than organization based, while document/dedup/suggestion access is organization scoped;
- child access must re-check soft-deleted ancestors;
- public HTTP errors must not echo arbitrary internal exceptions, SQL must bind values or use validated enums, and compatibility exports/behavior flags are removed only deliberately;
- `backend/openapi.json` and the generated frontend type file are regenerated together, never edited by hand;
- include only the four commands allowed by the matrix and label broader integration checks honestly.

- [ ] **Step 3: Rewrite `frontend/AGENTS.md` around the preserved integration notes**

Retain the Tambo marker and Tambo/Context7 sections, then add the four standard sections with every `frontend` requirement from the matrix:

- one cache owner for each server entity; chat is the documented Zustand exception and external callers import only `@/store/chat-store`;
- the backend remains the persisted-chat writer and `refreshMessages` owns terminal reconciliation;
- presentation code uses domain adapters rather than generated types directly; `frontend/src/types/generated/api.d.ts` is never hand-edited and HTTP shapes migrate adopt-on-touch;
- quality baselines only move stricter in the same reviewed change that earns the move;
- icon-only controls require accessible names, keyboard/focus behavior must remain operable, and user-facing async errors must not disclose raw internals;
- Node 24, `pnpm@10.18.2`, and the root lockfile are authoritative;
- list the exact matrix commands and mark accessibility E2E as browser/service-dependent.

- [ ] **Step 4: Create `src/AGENTS.md`**

Write the four standard sections and every `src` requirement from the matrix. Explicitly state the evidence-backed ambiguity: this tree contains Trigger.dev jobs, while `frontend/trigger.config.ts` resolves `./src/trigger` beneath `frontend`; an editor must prove the actual loader/deployment path before treating root `src/trigger/` as live. Require shared backend-client/schema use, secret-safe logging, backend-side authorization, bounded retries, and idempotent/HITL-safe writes. Do not advertise `trigger:dev` or `trigger:deploy` as an offline verification command.

- [ ] **Step 5: Create `tests/AGENTS.md`**

Write the four standard sections and every `tests` requirement from the matrix. Distinguish root cross-system suites from `backend/tests/` and frontend colocated tests; forbid assertion/fixture/floor weakening; require mutation proof for race/idempotency guards; label integration/E2E prerequisites; and quote the shared-dev load runner's authorization/announcement boundary without suggesting it be run for routine validation.

- [ ] **Step 6: Validate and commit the application guides**

Run:

```bash
for path in backend frontend src tests; do
  test -s "$path/AGENTS.md"
  rg -q '^## Scope and sources of truth$' "$path/AGENTS.md"
  rg -q '^## Invalid patterns$' "$path/AGENTS.md"
  rg -q '^## Required workflow$' "$path/AGENTS.md"
  rg -q '^## Verification$' "$path/AGENTS.md"
done
rg -n 'tambo-docs-v1\.0|Tambo AI Framework|https://docs\.tambo\.co/llms\.txt|npx tambo|## Context7|context7' frontend/AGENTS.md
git diff --check -- backend/AGENTS.md frontend/AGENTS.md src/AGENTS.md tests/AGENTS.md
git diff -- backend/AGENTS.md frontend/AGENTS.md src/AGENTS.md tests/AGENTS.md
```

Expected: all guides and sections exist, all six frontend preservation anchors remain, whitespace validation is silent, and the diff contains no root-policy copy or unsupported command.

Commit:

```bash
git add backend/AGENTS.md frontend/AGENTS.md src/AGENTS.md tests/AGENTS.md
git commit -m "docs: add scoped application agent guidance"
```

### Task 3: Add configuration, data, database, and flag guidance

**Files:**
- Create: `brand/AGENTS.md`
- Create: `config/AGENTS.md`
- Create: `data/AGENTS.md`
- Create: `database/AGENTS.md`
- Create: `feature-flags/AGENTS.md`
- Create: `supabase/AGENTS.md`
- Read: `docs/audits/2026-09-14-invalid-patterns-audit.md`
- Read: `docs/engineering/{backend,testing,gotchas}.md`

**Interfaces:**
- Consumes: evidence about assets, config consumers/symlinks, persistence boundaries, flag reachability, tenancy, and migration gates.
- Produces: scoped rules for six roots whose files are easy to misclassify as live, generated, interchangeable, or safe to rewrite.

- [ ] **Step 1: Verify the ownership boundaries before writing**

Run:

```bash
find . -maxdepth 1 -type l -printf '%f -> %l\n' | sort
git ls-files brand data database feature-flags supabase | sed -n '1,220p'
rg -n 'backend/alembic|supabase/migrations|database/migrations|launchdarkly-config\.json|brand/screenshots|data/datasets' \
  .github scripts backend frontend README.md docs/engineering --glob '!frontend/lint_output.txt'
```

Expected: evidence of the Compose symlinks, separate persistence trees, root screenshot consumers, and any feature-flag consumer. Do not read environment-file values.

- [ ] **Step 2: Create `brand/AGENTS.md`**

Write the four standard sections and every `brand` matrix requirement. Name `README.md` as a screenshot consumer, distinguish source text/SVG from rendered PNG/DOCX artifacts, require provenance plus visual review for replacements, and state that no root-local validator exists.

- [ ] **Step 3: Create `data/AGENTS.md`**

Write the four standard sections and every `data` matrix requirement. Treat the tree as sample/reference data, require a named consumer and stable schema, prohibit sensitive/customer data, preserve fixtures rather than mutating them to hide application failures, and state that no root-local validator exists.

- [ ] **Step 4: Create `config/AGENTS.md`**

Write the four standard sections and every `config` matrix requirement. State that root Compose files are symlinks to `config/docker-compose/`, environment-specific layering must be preserved, environment values must not be copied to reports/logs, and secrets stay indirect. Include exactly the two matrix commands and state their Docker prerequisite.

- [ ] **Step 5: Create `database/AGENTS.md`**

Write the four standard sections and every `database` matrix requirement. Explicitly distinguish these standalone SQL/Cypher/JSON assets from the live Alembic and Supabase migration systems. Require consumer confirmation, parameterized/allowlisted SQL, tenant predicates, ordered immutable migrations, transactional/rollback reasoning, and explicit authorization before applying anything. Include the static Alembic command only as a related live-schema check, not as validation of every file under `database/`.

- [ ] **Step 6: Create `feature-flags/AGENTS.md`**

Write the four standard sections and every `feature-flags` matrix requirement. Require runtime-consumer proof, key/enum/default alignment, and secret-free config. Cite the relative loader path in `backend/src/services/infrastructure/feature_flags.py` as something editors must resolve and test, without prematurely declaring the root config live or dead. State that no root-local validator exists.

- [ ] **Step 7: Create `supabase/AGENTS.md`**

Write the four standard sections and every `supabase` matrix requirement. Keep timestamped migration history append-only, require tenant-safe RLS/policies/functions and controlled `search_path`, prohibit credentials in `config.toml`, and distinguish email presentation templates from authorization. Use `scripts/ci/run_local_ci.sh` as the aggregate repository-backed check, require focused RLS tests for policy changes, and label reset/push operations as service-dependent and potentially destructive rather than routine validation.

- [ ] **Step 8: Validate and commit the configuration/data guides**

Run:

```bash
for path in brand config data database feature-flags supabase; do
  test -s "$path/AGENTS.md"
  rg -q '^## Scope and sources of truth$' "$path/AGENTS.md"
  rg -q '^## Invalid patterns$' "$path/AGENTS.md"
  rg -q '^## Required workflow$' "$path/AGENTS.md"
  rg -q '^## Verification$' "$path/AGENTS.md"
done
git diff --check -- brand/AGENTS.md config/AGENTS.md data/AGENTS.md database/AGENTS.md feature-flags/AGENTS.md supabase/AGENTS.md
git diff -- brand/AGENTS.md config/AGENTS.md data/AGENTS.md database/AGENTS.md feature-flags/AGENTS.md supabase/AGENTS.md
```

Expected: all six guides contain all four sections, whitespace validation is silent, and language distinguishes verified live sources from historical or unresolved consumers.

Commit:

```bash
git add brand/AGENTS.md config/AGENTS.md data/AGENTS.md database/AGENTS.md feature-flags/AGENTS.md supabase/AGENTS.md
git commit -m "docs: add scoped data and configuration guidance"
```

### Task 4: Add deployment, infrastructure, monitoring, script, and tool guidance

**Files:**
- Create: `deployment/AGENTS.md`
- Create: `infrastructure/AGENTS.md`
- Create: `monitoring/AGENTS.md`
- Create: `scripts/AGENTS.md`
- Create: `tools/AGENTS.md`
- Read: `.github/workflows/{helm-validate,workflow-lint}.yml`
- Read: `.github/workflows/README.md`
- Read: `docs/engineering/gotchas.md`
- Read: `monitoring/README.md`, `scripts/docs/README.md`, `tools/nous-playwright/README.md`

**Interfaces:**
- Consumes: audited live/legacy deployment evidence, existing render/config tests, operational safety boundaries, and standalone-tool privacy constraints.
- Produces: scoped operational guidance that prevents accidental deploys, config-copy drift, unsafe script execution, and leaked browser artifacts.

- [ ] **Step 1: Verify live/legacy and command provenance**

Run:

```bash
rg -n 'only `dev` is live|staging|production|ArgoCD|Qdrant|synthetic' docs/engineering/gotchas.md
rg -n 'helm lint|helm template|networkpolicy_render_test|backend_image_digest_render_test|pdb_render_test' \
  .github/workflows/helm-validate.yml .github/workflows/README.md deployment/github-actions
rg -n 'validate_docker_compose|tests/unit/scripts|test:list|\.auth/|test-results|playwright-report' \
  scripts .github tools/nous-playwright monitoring/README.md
```

Expected: each command and safety rule has a concrete workflow/script/doc source. Do not infer that same-named deployment or monitoring files share a consumer.

- [ ] **Step 2: Create `deployment/AGENTS.md`**

Write the four standard sections and every `deployment` matrix requirement. Mark legacy/retired material using the exact current gotcha evidence, require reachability checks before edits, prohibit unapproved deploy/apply/rollback or secret operations, and include the legacy chart lint command without claiming it validates the live ArgoCD path.

- [ ] **Step 3: Create `infrastructure/AGENTS.md`**

Write the four standard sections and every `infrastructure` matrix requirement. Name the live dev ArgoCD application and knowledge-graph chart, preserve base-plus-environment rendering, secret indirection, immutable image digest/full-SHA identity, backend-only NetworkPolicy selection, and environment-specific PDB behavior. Include the exact loop and all three render scripts from the workflow plus the Compose config assertion; state required local binaries and never suggest applying rendered output to a cluster as verification.

- [ ] **Step 4: Create `monitoring/AGENTS.md`**

Write the four standard sections and every `monitoring` matrix requirement. Require consumer-based disambiguation among repeated Prometheus/Grafana/Alertmanager/OTel names, preserve labels shared by alerts and dashboards, keep `.env` and webhooks private, and state that config rendering does not prove a live scrape. Include only `bash scripts/validate_docker_compose.sh` and its Docker prerequisite.

- [ ] **Step 5: Create `scripts/AGENTS.md`**

Write the four standard sections and every `scripts` matrix requirement. Split static/CI scripts from backup, deployment, maintenance, load, and shared-environment scripts; require a whole-file read and explicit authorization before side effects. Ban unsafe shell construction, broad unresolved destructive targets, secret output, and unhandled partial failure; require task-specific variables, safe temporary paths, and cleanup. Include the NOUS contract-test command and docs lint exactly as listed in the matrix.

- [ ] **Step 6: Create `tools/AGENTS.md`**

Write the four standard sections and every `tools` matrix requirement. State that `tools/nous-playwright` is standalone, its auth/session/trace/video/report outputs are private and untracked, and interactive authentication or live project creation requires user authorization. Include only the package's discovery command as an offline structural check; do not represent it as an authenticated end-to-end pass.

- [ ] **Step 7: Validate and commit the operational guides**

Run:

```bash
for path in deployment infrastructure monitoring scripts tools; do
  test -s "$path/AGENTS.md"
  rg -q '^## Scope and sources of truth$' "$path/AGENTS.md"
  rg -q '^## Invalid patterns$' "$path/AGENTS.md"
  rg -q '^## Required workflow$' "$path/AGENTS.md"
  rg -q '^## Verification$' "$path/AGENTS.md"
done
git diff --check -- deployment/AGENTS.md infrastructure/AGENTS.md monitoring/AGENTS.md scripts/AGENTS.md tools/AGENTS.md
git diff -- deployment/AGENTS.md infrastructure/AGENTS.md monitoring/AGENTS.md scripts/AGENTS.md tools/AGENTS.md
```

Expected: all five guides and sections exist, whitespace validation is silent, and no guide turns a validation command into an unauthorized deploy or shared-environment operation.

Commit:

```bash
git add deployment/AGENTS.md infrastructure/AGENTS.md monitoring/AGENTS.md scripts/AGENTS.md tools/AGENTS.md
git commit -m "docs: add scoped operations agent guidance"
```

### Task 5: Add historical, evaluation, notebook, and contextual guidance

**Files:**
- Create: `docs/AGENTS.md`
- Create: `evals/AGENTS.md`
- Create: `memory/AGENTS.md`
- Create: `notebooks/AGENTS.md`
- Create: `specs/AGENTS.md`
- Read: `docs/engineering/README.md`
- Read: `scripts/docs/README.md`
- Read: `evals/README.md`, `evals/AGENT_FLOW_BASELINE.md`

**Interfaces:**
- Consumes: audit distinctions between current contracts, historical evidence, recorded benchmarks, exploratory artifacts, and contextual notes.
- Produces: scoped immutability/provenance guidance for the remaining five roots and completes the exact 20-root set.

- [ ] **Step 1: Verify historical and generated-evidence boundaries**

Run:

```bash
git ls-files docs/archive docs/audits docs/plans docs/superpowers/specs evals/baselines evals/source-manifests memory notebooks specs | sed -n '1,260p'
rg -n 'source-manifests|digest|baseline|NOT YET GATED|infrastructure.*reward|generated trial evidence' \
  evals/README.md evals/AGENT_FLOW_BASELINE.md
rg -n 'FastAPI/Pydantic is the HTTP source of truth|Adopt-on-touch|generated/api\.d\.ts' \
  docs/engineering/api-contracts.md
```

Expected: the guides can identify historical/recorded artifacts and current sources without inferring that checked boxes or old reports describe current runtime behavior.

- [ ] **Step 2: Create `docs/AGENTS.md`**

Write the four standard sections and every `docs` matrix requirement. Point current engineering claims to `docs/engineering/README.md`; preserve archives, dated audits/reports, approved specs, plans, and baselines as records; require forward links or new amendments instead of silent history rewrites; and require valid relative links/paths. Include directory-doc lint and accurately state its filename scope.

- [ ] **Step 3: Create `evals/AGENTS.md`**

Write the four standard sections and every `evals` matrix requirement. Preserve source-manifest hashes, task/environment/verifier isolation, truth/calibration fixtures, recorded baselines, and honest gating status. Keep judge credentials out of the agent environment, distinguish infra failure from reward zero, and require a new run after task/revision/harness digest changes. Label Harbor and task verifier commands as environment-dependent.

- [ ] **Step 4: Create `memory/AGENTS.md`**

Write the four standard sections and every `memory` matrix requirement. State that these are dated contextual notes, not canonical engineering rules; require verification against code/current contracts; prohibit copying personal or sensitive content into reports; and state that there is no automated validator.

- [ ] **Step 5: Create `notebooks/AGENTS.md`**

Write the four standard sections and every `notebooks` matrix requirement. Preserve notebook JSON/output/plot provenance, avoid bulk reformatting, require explicit reproducibility notes for intentional regeneration, keep credentials and customer data out, and state that scripts here are exploratory unless a current consumer proves otherwise. State that there is no root-local deterministic gate.

- [ ] **Step 6: Create `specs/AGENTS.md`**

Write the four standard sections and every `specs` matrix requirement. Keep numbered specs/contracts/checklists as historical records, distinguish planned/completed checkboxes from verified runtime behavior, and prohibit changing snapshot specs as a substitute for the FastAPI-to-generated-TypeScript pipeline. Name the OpenAPI freshness command only as the validation for a corresponding live API change.

- [ ] **Step 7: Validate and commit the evidence/history guides**

Run:

```bash
for path in docs evals memory notebooks specs; do
  test -s "$path/AGENTS.md"
  rg -q '^## Scope and sources of truth$' "$path/AGENTS.md"
  rg -q '^## Invalid patterns$' "$path/AGENTS.md"
  rg -q '^## Required workflow$' "$path/AGENTS.md"
  rg -q '^## Verification$' "$path/AGENTS.md"
done
python3 scripts/docs/check_dir_docs.py
git diff --check -- docs/AGENTS.md evals/AGENTS.md memory/AGENTS.md notebooks/AGENTS.md specs/AGENTS.md
git diff -- docs/AGENTS.md evals/AGENTS.md memory/AGENTS.md notebooks/AGENTS.md specs/AGENTS.md
```

Expected: all five guides and sections exist, the existing directory-doc lint passes, whitespace validation is silent, and the guides keep historical/contextual material separate from current contracts.

Commit:

```bash
git add docs/AGENTS.md evals/AGENTS.md memory/AGENTS.md notebooks/AGENTS.md specs/AGENTS.md
git commit -m "docs: add scoped evidence and history guidance"
```

### Task 6: Reconcile the audit and run full acceptance validation

**Files:**
- Modify: `docs/audits/2026-09-14-invalid-patterns-audit.md`
- Verify: `backend/AGENTS.md`, `brand/AGENTS.md`, `config/AGENTS.md`, `data/AGENTS.md`, `database/AGENTS.md`, `deployment/AGENTS.md`, `docs/AGENTS.md`, `evals/AGENTS.md`, `feature-flags/AGENTS.md`, `frontend/AGENTS.md`, `infrastructure/AGENTS.md`, `memory/AGENTS.md`, `monitoring/AGENTS.md`, `notebooks/AGENTS.md`, `scripts/AGENTS.md`, `specs/AGENTS.md`, `src/AGENTS.md`, `supabase/AGENTS.md`, `tests/AGENTS.md`, `tools/AGENTS.md`

**Interfaces:**
- Consumes: all 20 scoped guides and the evidence baseline.
- Produces: a final audit whose mappings and limitations match the implemented guidance, plus machine-checked acceptance evidence for scope, structure, paths, command provenance, placeholders, consistency, and clean diffs.

- [ ] **Step 1: Assert exactly the selected top-level roots contain scoped guides**

Run:

```bash
python3 - <<'PY'
from pathlib import Path

expected = {
    "backend", "brand", "config", "data", "database", "deployment",
    "docs", "evals", "feature-flags", "frontend", "infrastructure",
    "memory", "monitoring", "notebooks", "scripts", "specs", "src",
    "supabase", "tests", "tools",
}
actual = {path.parent.as_posix() for path in Path(".").glob("*/AGENTS.md")}
assert actual == expected, {
    "missing": sorted(expected - actual),
    "unexpected": sorted(actual - expected),
}
print("exactly 20 selected top-level roots contain AGENTS.md")
PY

test "$(git diff --name-only HEAD~5..HEAD -- AGENTS.md)" = ""
```

Expected: the exact-set assertion passes and the repository-root `AGENTS.md` was not changed by this implementation. If commit grouping differs from the plan, compare the root file to the recorded audit base SHA instead of relying on `HEAD~5`.

- [ ] **Step 2: Validate guide structure, frontend preservation, and substantive content**

Run:

```bash
for file in */AGENTS.md; do
  rg -q '^## Scope and sources of truth$' "$file"
  rg -q '^## Invalid patterns$' "$file"
  rg -q '^## Required workflow$' "$file"
  rg -q '^## Verification$' "$file"
done

rg -n 'tambo-docs-v1\.0|Tambo AI Framework|https://docs\.tambo\.co/llms\.txt|npx tambo|## Context7|context7' frontend/AGENTS.md

python3 - <<'PY'
from pathlib import Path

files = list(Path(".").glob("*/AGENTS.md")) + [
    Path("docs/audits/2026-09-14-invalid-patterns-audit.md")
]
markers = [
    "T" + "BD",
    "T" + "ODO",
    "implement" + " later",
    "fill in" + " details",
    "this directory is part of the project",
    "refer to the main project documentation for more information",
]
bad = []
for path in files:
    lowered = path.read_text(encoding="utf-8").lower()
    for marker in markers:
        if marker.lower() in lowered:
            bad.append((path.as_posix(), marker))
assert not bad, bad
print("no unfinished markers or banned boilerplate in scoped guidance/audit")
PY
```

Expected: all guides have the four sections, the six frontend anchors are present, and the content scan reports no unfinished marker or boilerplate.

- [ ] **Step 3: Validate all local Markdown links and the explicit source-path ledger**

Run:

```bash
python3 - <<'PY'
import re
from pathlib import Path

files = list(Path(".").glob("*/AGENTS.md")) + [
    Path("docs/audits/2026-09-14-invalid-patterns-audit.md")
]
missing = []
for source in files:
    text = source.read_text(encoding="utf-8")
    for target in re.findall(r"\[[^\]]+\]\(([^)]+)\)", text):
        target = target.strip().split("#", 1)[0]
        if not target or "://" in target or target.startswith("mailto:"):
            continue
        resolved = (source.parent / target).resolve()
        if not resolved.exists():
            missing.append((source.as_posix(), target))
assert not missing, missing
print("all local Markdown links resolve")
PY

for path in \
  docs/engineering/README.md docs/engineering/backend.md \
  docs/engineering/frontend.md docs/engineering/testing.md \
  docs/engineering/api-contracts.md docs/engineering/gotchas.md \
  docs/security/PRE_PUBLIC_SECURITY_AUDIT_REMEDIATION.md \
  docs/frontend/ux-design-audit.md scripts/wcag-checklist.sh \
  .github/workflows/README.md .github/workflows/helm-validate.yml \
  scripts/docs/check_dir_docs.py scripts/ci/check_alembic.py \
  scripts/ci/generate_openapi.py scripts/validate_docker_compose.sh \
  infrastructure/tests/docker_compose_development_config_test.sh \
  infrastructure/helm/knowledge-graph-analytics/tests/networkpolicy_render_test.sh \
  infrastructure/helm/knowledge-graph-analytics/tests/backend_image_digest_render_test.sh \
  infrastructure/helm/knowledge-graph-analytics/tests/pdb_render_test.sh \
  evals/README.md evals/AGENT_FLOW_BASELINE.md \
  tools/nous-playwright/README.md; do
  test -e "$path" || { printf 'missing referenced path: %s\n' "$path" >&2; exit 1; }
done
```

Expected: every local Markdown link and every canonical path named by the guides exists. Manually inspect any additional code-formatted repository path in the diff; commands and glob patterns are not filesystem links.

- [ ] **Step 4: Verify command provenance**

For every command printed in an `AGENTS.md`, locate its defining manifest/workflow/script or canonical engineering command block. Run this focused ledger check:

```bash
rg -n 'ruff check backend/src|pytest -q backend/tests/unit/architecture|generate_openapi.py --check' \
  docs/engineering/backend.md docs/engineering/api-contracts.md
rg -n 'lint:changed|quality:exclusions|type-check|test:e2e:accessibility' frontend/package.json
rg -n 'docker compose.*config --quiet' infrastructure/tests/docker_compose_development_config_test.sh
rg -n 'helm lint|helm template|networkpolicy_render_test|backend_image_digest_render_test|pdb_render_test' \
  .github/workflows/helm-validate.yml deployment/github-actions/workflows/deploy.yml
rg -n 'validate_docker_compose.sh|tests/unit/scripts/' scripts .github/workflows/test-pipeline.yml
rg -n 'test:list' tools/nous-playwright/package.json
rg -n 'playwright test --project=chromium' docs/engineering/testing.md
rg -n 'run_local_ci\.sh|check_alembic|Unit tests \(blocking\)' \
  docs/engineering/backend.md scripts/ci/run_local_ci.sh .github/workflows/test-pipeline.yml
```

Expected: every guide command has a matching repository definition/reference. Remove or correct any command that lacks provenance; do not substitute a plausible command.

- [ ] **Step 5: Perform the canonical-contract consistency review**

Read each child guide side by side with the canonical contracts:

```bash
git diff -- \
  backend/AGENTS.md frontend/AGENTS.md tests/AGENTS.md database/AGENTS.md \
  config/AGENTS.md infrastructure/AGENTS.md src/AGENTS.md supabase/AGENTS.md
sed -n '1,260p' docs/engineering/backend.md
sed -n '1,280p' docs/engineering/frontend.md
sed -n '1,180p' docs/engineering/testing.md
sed -n '1,180p' docs/engineering/api-contracts.md
sed -n '1,160p' docs/engineering/gotchas.md
```

Expected manual checklist:

- no guide changes workspace access from membership/public/owner semantics to organization filtering;
- every document/dedup/suggestion rule retains organization scope;
- frontend chat/cache ownership and generated-type adopt-on-touch rules agree with current engineering docs;
- no guide calls advisory full-tree debt checks blocking or allows a baseline to loosen silently;
- no guide suggests hand-editing generated OpenAPI/TypeScript artifacts;
- no guide calls retired staging/production applications live or treats legacy deployment material as deployed;
- no guide weakens secret, authorization, human-confirmation, evidence, or verification gates.

Fix any contradiction in the child guide and update its audit mapping before proceeding.

- [ ] **Step 6: Run final documentation and diff gates**

Run:

```bash
make docs-lint
git diff --check
git status --short
git diff --stat HEAD~5
git diff --name-only HEAD~5 | sort
```

Expected: directory-doc lint passes; `git diff --check` is silent; the base-to-worktree diff contains only the audit and the 20 scoped guides for this implementation, with no generated, runtime, environment, baseline, migration, dataset, screenshot, or root-policy edits. At this point `git status --short` should show only the audit reconciliation and any unrelated pre-existing work recorded in Task 1, because Tasks 1–5 were committed separately.

- [ ] **Step 7: Reconcile final validation evidence into the audit**

Update `docs/audits/2026-09-14-invalid-patterns-audit.md` so its root-coverage mapping points to every implemented guide, its command-results table reflects commands actually run, and its limitations plainly list unavailable tools or service-dependent checks. Keep findings separate from preventative rules. Do not turn newly written guidance into proof that the underlying production finding is fixed.

Run:

```bash
git diff --check -- docs/audits/2026-09-14-invalid-patterns-audit.md
git diff -- docs/audits/2026-09-14-invalid-patterns-audit.md
```

Expected: whitespace validation is silent and the audit accurately distinguishes current findings, enforced invariants, review risks, validation status, and limitations.

- [ ] **Step 8: Commit the final reconciliation**

```bash
git add docs/audits/2026-09-14-invalid-patterns-audit.md \
  backend/AGENTS.md brand/AGENTS.md config/AGENTS.md data/AGENTS.md \
  database/AGENTS.md deployment/AGENTS.md docs/AGENTS.md evals/AGENTS.md \
  feature-flags/AGENTS.md frontend/AGENTS.md infrastructure/AGENTS.md \
  memory/AGENTS.md monitoring/AGENTS.md notebooks/AGENTS.md scripts/AGENTS.md \
  specs/AGENTS.md src/AGENTS.md supabase/AGENTS.md tests/AGENTS.md tools/AGENTS.md
git commit -m "docs: reconcile invalid-pattern guidance validation"
```

Run one post-commit scope check:

```bash
git show --stat --oneline --summary HEAD
git status --short
```

Expected: the reconciliation commit contains only intended documentation changes and the working tree is clean apart from unrelated pre-existing work identified in Task 1.
