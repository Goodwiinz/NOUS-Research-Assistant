# Engineering standards

What this PR series (`docs/plans/2026-07-15-maintainability-foundation.md`)
actually established and enforces today — not aspirational guidance. Each doc
is tight on purpose: rules plus the commands that check them, linked to the
real module names and test files that own each guard. If a rule here isn't
backed by a test, a CI gate, or a pre-commit hook, it doesn't belong here —
file it as a proposal instead.

- **[backend.md](backend.md)** — router/service/transaction boundaries in
  `backend/src/api/threads/workspace_routes/` +
  `backend/src/services/threads/`, the tenant/access-scope funnel, and the
  backend quality ratchets.
- **[frontend.md](frontend.md)** — chat state ownership across
  TanStack Query / Zustand (`@/store/chat-store`) / component state,
  generated vs. hand-authored HTTP types, the frontend quality ratchets, and
  the Node/pnpm toolchain.
- **[testing.md](testing.md)** — test layout, how to move a quality floor,
  and the mutation-verification rule for race/idempotency tests.
- **[api-contracts.md](api-contracts.md)** — the OpenAPI → generated
  TypeScript pipeline and the adopt-on-touch migration rule.
- **[gotchas.md](gotchas.md)** — operational invariants preserved from the
  retired root `CLAUDE.md` (#1491). Exempt from the enforced-rule bar above:
  these are hard-won environment/API/tenancy facts, not CI-backed rules.
- **[typesafe-routing.md](typesafe-routing.md)** — opt-in routing configuration,
  offline replay, validation gates, and rollback; Azure remains the default.
- **[nous-loop.md](nous-loop.md)** — canonical, cross-runtime workflow for one
  evidence-driven self-improvement tick. Its static contract is enforced by
  `tests/unit/scripts/test_nous_loop_contract.py`; agent pressure tests verify
  the judgment-heavy behavior when the workflow changes.

Future internal workspace packages must be declared with the pnpm
`workspace:` protocol (`"pkg": "workspace:*"`) so installs can never fall
back to the public registry.

## Architecture guards

Static regression tests that fail loudly if a boundary above erodes:

- `backend/tests/unit/architecture/test_workspace_boundaries.py` — workspace
  route modules own no transaction/SQL, don't reach sideways into a sibling
  resource module, and the compatibility shim resolves to the composed
  routers.
- `backend/tests/unit/architecture/test_maintenance_contracts.py` — every
  scope/access getter in `workspace_access.py` requires the caller's
  identity (fails closed).
- `frontend/src/test/architecture/__tests__/maintenanceContracts.test.ts` —
  the chat route imports no API service directly, `chat-store.ts` stays a
  thin facade, and presentation components under `components/chat` don't
  import generated OpenAPI types directly when a domain adapter exists.

## Ownership

See [`.github/CODEOWNERS`](../../.github/CODEOWNERS).
