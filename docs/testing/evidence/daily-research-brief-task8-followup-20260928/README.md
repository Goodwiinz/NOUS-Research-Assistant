# Daily Research Brief Task 8 follow-up evidence

Date: 2026-09-28

Independent-review source: `a92fcfa770f4fbe89d7154f1bb88b37f8cf5f408`

Final whole-feature source: `9ca9d6a2995ba8dd52328efd0e5cd93856138b4a`

Original certification source: `efd76079be75fdbba9eef4215c1c831d829cacb4`

The existing performance, eval, and simultaneous-stream-claim transcripts are
bound to the independent-review source. The final provenance, approval
attestation, runtime-control, cancellation, PostgreSQL, and browser fixes are
frozen in the final whole-feature source. This directory is committed in a
documentation-only child of that final source, so the transcripts can name the
immutable executable commit without making a commit contain its own hash.

Each bounded transcript records the command, exit status, exact source or
candidate state, salient output, and SHA-256 digest of its local raw capture.
The raw `/tmp` captures are mutable scratch files; these committed transcripts
are the evidence of record.

## Evidence index

| File                                           | Classification  | Scope                                                                                                       |
| ---------------------------------------------- | --------------- | ----------------------------------------------------------------------------------------------------------- |
| `performance-run-1.txt`                        | PASS            | First baseline-equivalent full performance invocation on `a92fcfa`.                                         |
| `performance-run-2.txt`                        | PASS            | Second separate invocation on `a92fcfa`.                                                                    |
| `performance-run-3.txt`                        | PASS            | Third separate invocation on `a92fcfa`.                                                                     |
| `final-source-performance.txt`                 | PASS            | Exact documented full performance invocation on final source `9ca9d6a`.                                     |
| `stream-claim-mutation-red.txt`                | EXPECTED RED    | Removing the row-lock guard let both real SSE consumers win.                                                |
| `stream-claim-mutation-green.txt`              | PASS            | Restoring the exact production file admitted one consumer.                                                  |
| `terminal-sse-cancellation-mutation-red.txt`   | EXPECTED RED    | Removing the durable terminal guard rewrote a completed run to paused.                                      |
| `terminal-sse-cancellation-mutation-green.txt` | PASS            | Restoring the durable guard preserved terminal state.                                                       |
| `exact-source-browser.txt`                     | PASS            | All 10 real browser → Next → FastAPI → PostgreSQL scenarios on `9ca9d6a`.                                   |
| `exact-source-browser-failures.txt`            | FAILED/EXCLUDED | Earlier executable failures and invalid environment/host attempts retained without being counted as passes. |
| `focused-checks.txt`                           | PASS/BLOCKED    | Eval, PostgreSQL, frontend, type-check, and formatting checks on `a92fcfa`; the live-model test is BLOCKED. |
| `final-source-checks.txt`                      | PASS            | Final-source affected backend, PostgreSQL, frontend, contracts, lint, Bandit, and staged Gitleaks checks.   |

The frozen numeric performance thresholds and nearest-rank percentile function
did not change. The baseline-equivalent max-stage sample count is 11, making
nearest-rank p95 rank 11 (the maximum). Each max-stage measurement uses one
untimed warmup, leaves garbage collection enabled, and clears fixture request
recording outside timed stage execution.

The release decision remains negative. Dependency audits, anonymous legacy
Safety, full frontend validation, and full local CI are FAILED. The
configured-model and authenticated Safety gates are BLOCKED. Remote checks,
production configuration inspection, deployment, and flag enablement are NOT
RUN. `DAILY_RESEARCH_BRIEF_ENABLED` remains default false.
