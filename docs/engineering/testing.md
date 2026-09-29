# Testing standards

## Layout

- `backend/tests/unit/` — fast, isolated unit tests.
- `backend/tests/unit/ci/` — contract tests for the CI toolchain/quality
  gates themselves (Node/pnpm versions, changed-file selection, tsconfig
  exclusion ratchet, OpenAPI generation).
- `backend/tests/unit/architecture/` — structural regression guards over
  the codebase's own module boundaries (`test_workspace_boundaries.py`,
  `test_maintenance_contracts.py`).
- `backend/tests/unit/services/threads/` — service-layer behavior
  (authorization, transaction order, response loading) for the workspace
  resource services.
- `backend/tests/api/threads/` — route-level behavior tests (permissions,
  cross-org denial, filter parity) reused rather than duplicated by the
  route-contract characterization tests.
- Frontend: `__tests__/` colocated next to the hook/component/store it
  covers; `frontend/src/test/quality/__tests__/` (ratchet comparator
  tests); `frontend/src/test/architecture/__tests__/` (structural guards);
  `tests/e2e/` (Playwright).

## Moving a quality floor

Ratchets (`frontend/quality-baseline.json`, Ruff `F821`/`F823`, the
tsconfig exclusion list) only ever move in the stricter direction, and only
as an explicit, reviewed edit landing in the same PR that earns it — never
as a side effect of a passing run.

1. Run the ratchet's own measuring command (see the Ratchets section of
   [backend.md](backend.md) or [frontend.md](frontend.md)).
2. Edit the committed baseline (`frontend/quality-baseline.json`, or the
   ratchet's stated location) to the new value — never to a number the
   current tree doesn't already satisfy.
3. Commit the baseline edit together with the change that earned it. Don't
   fold an unrelated floor-move into an unrelated PR.

A ratchet moving that you can't explain in the PR description is a
regression, not progress — treat it as a bug in the change, not a lucky
baseline update.

## Mutation verification (race / idempotency tests)

A test that asserts a race guard or an idempotency guard must be proven to
fail when the guard is removed — a test that can't be observed to fail
isn't exercising the guard, whatever it claims to assert. Procedure (see the
[chat store/hooks examples](../testing/chat-mutation-checks.md) and the
[durable operation examples](../testing/agent-orchestration-mutation-checks.md)):

1. Temporarily disable the guard in source (comment out the check, or
   neutralize the condition it depends on).
2. Run the named focused test and confirm it fails — and read the failure
   message; it should name the actual defect the guard prevents, not a
   generic mismatch.
3. Restore the guard exactly; confirm `git diff` on the source file is
   empty.
4. Rerun the focused test and confirm it passes again.

Do this once per guard whenever you add or change a race/idempotency test,
and record the guard's file:line plus the exact focused command — in the
test file itself, or in an adjacent doc like
`docs/testing/chat-mutation-checks.md` — so the next person doesn't have to
rediscover which line the test is actually protecting.

## Commands

```sh
pytest tests/ --cov=src            # full backend suite with coverage
pytest -q backend/tests/unit/ci backend/tests/unit/architecture
pytest -q backend/tests/unit/api backend/tests/api/threads
pytest -q backend/tests/unit/services/threads
pnpm --dir frontend test
pnpm --dir tests/e2e exec playwright test --project=chromium
```
