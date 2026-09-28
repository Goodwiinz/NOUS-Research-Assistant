# Daily Research Brief Task 8 follow-up evidence

Date: 2026-09-28

Source under test: `a92fcfa770f4fbe89d7154f1bb88b37f8cf5f408`

Source parent: `efd76079be75fdbba9eef4215c1c831d829cacb4`

The source commit contains every executable change made for the independent
review. This directory is committed in a documentation-only child commit. That
two-commit provenance lets every transcript identify the exact code it tested
without asking a commit to contain its own hash. The final handoff records both
commit IDs.

The committed transcripts are bounded copies of the relevant command output.
Each names the exact command, exit status, source SHA, and SHA-256 digest of the
local raw capture from which it was reduced. The raw `/tmp` captures are
mutable local scratch files and are not the evidence of record.

## Evidence index

| File | Classification | Scope |
| --- | --- | --- |
| `performance-run-1.txt` | PASS | Exact documented full performance pytest invocation; 6 tests. |
| `performance-run-2.txt` | PASS | Second separate invocation of the same command. |
| `performance-run-3.txt` | PASS | Third separate invocation of the same command. |
| `stream-claim-mutation-red.txt` | EXPECTED RED | Deliberately removed the row-lock guard; both real SSE consumers won and the regression failed. |
| `stream-claim-mutation-green.txt` | PASS | Restored the exact production file; one consumer won and the focused regression passed. |
| `focused-checks.txt` | PASS/BLOCKED | Eval, PostgreSQL, frontend, type-check, and scoped formatting checks. The live-model test is BLOCKED, not passed. |

The frozen numeric performance thresholds and the nearest-rank percentile
function did not change. The max-stage sample count is the baseline-equivalent
11 observations, so nearest-rank p95 is rank 11 (the maximum). Each max-stage
measurement has one untimed warmup, leaves garbage collection enabled, and
clears fixture request recording outside timed stage execution.

The overall release decision remains negative. Dependency audits and the full
local-CI gate are FAILED; the configured-model and authenticated Safety gates
are BLOCKED; remote checks and every production/release action are NOT RUN.
The feature remains disabled.
