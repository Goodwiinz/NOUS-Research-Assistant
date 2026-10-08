# User-level verification (nous-verify)

Canonical contract for proving a change works as a user would experience it.
Unit and CI gates stay as they are; this gate adds end-to-end proof selected
from the diff, with visual evidence a reviewer can inspect.

Rollout status: the feature map, the flow charts and
`scripts/ci/check_feature_map.py` are live and blocking. The runner flags
under [Running](#running) (`--features`, `--changed-from`, `--evidence-dir`,
`--evidence-record`), `scripts/verify/boot_local.sh`, video/trace capture and
checkpoint screenshots are available, and every v1 feature is `covered`.
`covered` means the mapped scenarios and their `checkpoint()` calls exist; it
is not a live `PASS`. The v1 live runs are `NOT RUN` until a run against a
target with QA credentials produces an evidence record. The
`nous-loop.md` step 7b stop gate is still planned in
[the implementation plan](../plans/2026-10-08-nous-verify.md).

## Vocabulary

- `PASS` — every selected scenario passed, cleanup completed, and the operator
  viewed every checkpoint screenshot and confirmed the feature's pass criteria.
- `FAILED` — at least one selected scenario failed an assertion, or a checkpoint
  contradicts a pass criterion even though assertions passed.
- `BLOCKED` — a required prerequisite was unavailable: credentials
  (`NOUS_QA_EMAIL`/`NOUS_QA_PASSWORD` or `--storage-state`), `--allow-writes`,
  a reachable target, or a deployment identity that did not match.
- `NOT RUN` — the gate was not executed for this change (no mapped feature, or
  the operator could not boot or reach any target).

A `PASS` never comes from assertions alone. Missing credentials, a skipped
case, or an exit code of `2` from `pnpm qa:nous` is `BLOCKED` or `NOT RUN`.

## Artifacts

| Artifact | Location | Committed |
| --- | --- | --- |
| Feature map | `docs/engineering/feature-map.yaml` | yes |
| Flow charts | `docs/engineering/flows/<feature>.md` | yes |
| Checkpoint PNGs, video, trace, JSON/HTML reports | `.verify-artifacts/<run-id>/` | no (gitignored; upload as CI/PR artifact) |
| Evidence record | `docs/testing/evidence/verify-<feature>-<YYYYMMDD>/README.md` | yes |

## Feature map

One entry per feature: `id`, `status` (`planned` or `covered`; `covered`
only says scenarios and checkpoint calls exist, never that they passed), `surfaces`
(`web` routes, `api` endpoints, `cli` commands), `states`, `pass_criteria`,
`scenarios` (ids from `tests/e2e/qa/scenarios.mjs`), `owns` (path globs,
`fnmatch` semantics: `*` also crosses `/`). `ignore.pages` and
`ignore.routers` list unclaimed `frontend/app/**/page.tsx` and
`backend/src/api/**/*.py` routers; the lists only shrink.

`scripts/ci/check_feature_map.py` fails when a mapped scenario id does not
exist, a `covered` feature's flow checkpoint has no `checkpoint('<name>')`
call, a page or router is neither claimed nor ignored, or an ignore entry is
stale, also claimed, or new relative to the map at merge-base(`--base`,
HEAD). It runs with `--base` in the hosted Lightweight Checks job and in
`scripts/ci/run_local_ci.sh`.

## Flow charts

Each `docs/engineering/flows/<feature>.md` holds one Mermaid chart. A node
whose label is `checkpoint: <name>` names a screenshot that the mapped
scenario must take with `evidence.checkpoint('<name>')`. Names are
`feature.step` in lower-case letters, digits, dots and hyphens.

## Running

```sh
scripts/verify/boot_local.sh start        # local backend + frontend, waits for health
pnpm qa:nous --features login,chat-send-stream-reload --allow-writes \
  --base-url http://127.0.0.1:3000 --api-url http://127.0.0.1:8000/api/v1 \
  --evidence-dir .verify-artifacts/<run-id> \
  --evidence-record docs/testing/evidence/verify-<feature>-<YYYYMMDD>
pnpm qa:nous --changed-from origin/develop ...   # select features from the diff
scripts/verify/boot_local.sh stop
```

Against the deployed lane, replace the URLs with `https://goodwiinz.tech` and
`https://dev-api.goodwiinz.tech/api/v1` and add `--expected-backend-sha <sha>`
(with `--deployment-evidence` when the server exposes no identity).

## Evidence record

The committed README lists the source SHA and dirty state, the target URLs,
the command, each scenario's result and reason, and every checkpoint name
with its artifact file name. It links no binary. Add the bundle to the table
in `docs/testing/evidence/README.md`.

## Stop gate

(planned) `docs/engineering/nous-loop.md` step 7b will require a local `PASS`
for every mapped feature the diff touches before an outcome of `merged`;
`BLOCKED` or `NOT RUN` will end the tick as `ready-for-human`. Step 8 will
rerun the same features against the deployed lane after deployment and file
a regression on failure. Neither step exists in `nous-loop.md` yet.
