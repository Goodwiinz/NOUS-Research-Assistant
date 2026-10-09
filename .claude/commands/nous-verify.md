---
description: Prove a NOUS change works as a user would see it (nous-loop step 7b)
---

# /nous-verify

Read `docs/engineering/verification.md` completely first. That file is
canonical; this adapter only maps Claude Code capabilities onto it and
may not weaken its gates. Report each feature as `PASS`, `FAILED`, `BLOCKED` or
`NOT RUN`.

## Steps

1. Select: dry run with `pnpm qa:nous --changed-from origin/develop --list`
   (or `--features <ids>` from the request). Exit 2 with "No mapped feature
   changed" → report `NOT RUN (no mapped feature)` and stop.
2. Target and run. For the deployed lane, swap the URLs for
   `https://goodwiinz.tech` / `https://dev-api.goodwiinz.tech/api/v1` and add
   `--expected-backend-sha <sha>`. If nothing is reachable, report `BLOCKED`
   with the exact reason. The command is the one in nous-loop step 7b:

```bash
scripts/verify/boot_local.sh start
pnpm qa:nous --changed-from origin/develop --allow-writes \
  --base-url http://127.0.0.1:3000 --api-url http://127.0.0.1:8000/api/v1 \
  --evidence-record docs/testing/evidence/verify-<first-feature>-<YYYYMMDD>
scripts/verify/boot_local.sh stop
```

   Credentials come only from `NOUS_QA_EMAIL` / `NOUS_QA_PASSWORD` (or
   `--storage-state`); never pass them as flags or print them.
3. Inspect: open every PNG in `.verify-artifacts/<run-id>/checkpoints/` with
   the Read tool before claiming `PASS`, and compare each against the
   feature's `pass_criteria`. A screenshot that contradicts a criterion makes
   the result `FAILED` even if assertions passed.
4. Record: commit the evidence README, add it to
   `docs/testing/evidence/README.md`, and quote the exit code. Exit 2 is
   `BLOCKED` or `NOT RUN`, never a pass. Never attach, commit, or upload a
   `trace*.zip`; traces carry session cookies and tokens.
5. Report one line per feature:
   `<feature>: PASS|FAILED|BLOCKED|NOT RUN — <reason or evidence path>`.
