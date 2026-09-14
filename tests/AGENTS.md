# AGENTS.md

Test-specific guidance. The repository-root `AGENTS.md` still applies.

## Scope and sources of truth

Use [Testing standards](../docs/engineering/testing.md) as the primary
contract, with the [invalid-pattern audit](../docs/audits/2026-09-14-invalid-patterns-audit.md)
for the regression obligations it records. The root `tests/` tree contains
cross-system suites, Playwright E2E material, and load tests. It is distinct
from `backend/tests/` (backend unit/API/service tests) and from frontend
`__tests__/` colocated with the hook, component, or store under test.

## Invalid patterns

- Put a test in the layer that owns the behavior. Do not duplicate a backend
  route/service contract in the root cross-system suite or call a browser/E2E
  test a unit test.
- Preserve realistic fixtures, negative authorization cases, and failure
  assertions. Do not weaken assertions, rewrite fixtures to hide a failure,
  add retries that mask flakiness, or lower a quality/coverage floor to make a
  run pass.
- A race or idempotency test is incomplete until mutation verification proves
  the test fails when the guard is removed, the original guard is restored
  exactly, and the focused test passes again. Record the guard's file/line and
  focused command beside the test.
- Label integration, service, database, browser, and credential prerequisites
  honestly. A blocked environment is `NOT RUN`, not a passing test result.

The shared-dev maximum load runner is disruptive. Its README warning is:
**“Announce it before running `--full`.”** The full runner is
`tests/load/run-shared-dev-max.sh`; obtain explicit environment authorization
before invoking its `--full` mode. Do not run the full shared-dev stress
profile as routine validation.

## Required workflow

- Select the narrowest owning suite first, then add a cross-system or E2E
  check only when the behavior crosses that boundary. Keep root suites,
  `backend/tests/`, and frontend colocated tests clearly attributed in reports.
- For every new or changed race/idempotency guard, follow the four-step
  mutation proof in `docs/engineering/testing.md`: disable the guard and
  observe the focused failure, restore it exactly, verify a clean source diff,
  and rerun the focused test successfully.
- Keep fixtures and baseline/coverage ratchets under review. Never convert a
  service- or browser-dependent failure into a fixture or assertion change
  without proving the product behavior is the intended target.

## Verification

Routine matrix-backed checks are:

```sh
pytest -q backend/tests/unit/ci backend/tests/unit/architecture
pnpm --dir tests/e2e exec playwright test --project=chromium
```

The Playwright command requires the browser and any configured application
services; label it browser/service-dependent and report `NOT RUN` when those
prerequisites are absent.
