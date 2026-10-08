# scripts/verify

Helpers for the user-level verification gate described in
[`docs/engineering/verification.md`](../../docs/engineering/verification.md).

## `boot_local.sh start|stop|status`

Starts a local target for `pnpm qa:nous --features ...`:

- the backend with `backend/.venv/bin/python -m uvicorn src.main:app` on
  `127.0.0.1:8000` (in a linked worktree without its own venv it uses the main
  checkout's `backend/.venv`; override with `PYTHON`);
- the frontend with `pnpm --dir frontend dev:offline` on `127.0.0.1:3000`.

`start` refuses (exit 2) when either port already answers, so the health
checks can only be satisfied by the processes it started. It then polls `/health`, `/health/readiness` and the frontend `/login`
page until they answer or `NOUS_VERIFY_BOOT_TIMEOUT` seconds (default 180)
pass. On a timeout it stops what it started and exits 1; read
`.verify-artifacts/boot/backend.log` or `frontend.log` for the cause.

The backend still needs its usual environment: `backend/.env` or an Infisical
shell with Postgres, Redis and Supabase settings. Local development does not
use Docker, so nothing here starts a database. `dev:offline` skips Infisical,
so `frontend/.env.local` must carry the `NEXT_PUBLIC_*` values.

`stop` reads the PID files under `.verify-artifacts/boot/` (gitignored) and
signals only those process groups. It never kills processes by name, so a
backend you started yourself is left alone. Ports, timeout and the PID/log
directory can be changed with `NOUS_VERIFY_BACKEND_PORT`,
`NOUS_VERIFY_FRONTEND_PORT`, `NOUS_VERIFY_BOOT_TIMEOUT` and
`NOUS_VERIFY_BOOT_DIR`.

The contract test is `backend/tests/unit/ci/test_boot_local.py`.
