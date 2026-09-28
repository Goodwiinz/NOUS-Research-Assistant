# Daily Research Brief Task 8 follow-up evidence

Date: 2026-09-28

Independent-review source: `a92fcfa770f4fbe89d7154f1bb88b37f8cf5f408`

Final executable source: `b334ec79861b438e8646ff75325cf4f5bdd75a30`

Prior final re-review source: `ac17ca15a3a91af936851647bbde89d9860709ba`

Prior whole-feature source: `9ca9d6a2995ba8dd52328efd0e5cd93856138b4a`

Original certification source: `efd76079be75fdbba9eef4215c1c831d829cacb4`

The existing performance, eval, and simultaneous-stream-claim transcripts are
bound to the source where they ran. Runtime control, cancellation, PostgreSQL,
and approval attestation are frozen in the prior whole-feature source. Source
`ac17ca15` added deterministic bibliography fallback from retained provider
provenance. Final source `b334ec798` constrains reader identifiers to the seven
discovery identifier kinds, preserves populated canonical/direct identifiers,
and prevents opaque provider keys from reaching JSON, Markdown, or CSV. This
directory is committed in a documentation-only child of the final source, so
the transcripts can name the immutable executable commit without making a
commit contain its own hash.

Each bounded transcript records the command, exit status, exact source or
candidate state, and salient output. It also records a SHA-256 digest whenever
the local raw capture was retained. The identifier-safety expected RED was
recorded immediately as bounded terminal output but has no separate raw file.
The raw `/tmp` captures are mutable scratch files; these committed transcripts
are the evidence of record.

## Evidence index

| File                                           | Classification  | Scope                                                                                                       |
| ---------------------------------------------- | --------------- | ----------------------------------------------------------------------------------------------------------- |
| `performance-run-1.txt`                        | PASS            | First baseline-equivalent full performance invocation on `a92fcfa`.                                         |
| `performance-run-2.txt`                        | PASS            | Second separate invocation on `a92fcfa`.                                                                    |
| `performance-run-3.txt`                        | PASS            | Third separate invocation on `a92fcfa`.                                                                     |
| `final-source-performance.txt`                 | PASS            | Exact documented full performance invocation on prior source `9ca9d6a`.                                     |
| `stream-claim-mutation-red.txt`                | EXPECTED RED    | Removing the row-lock guard let both real SSE consumers win.                                                |
| `stream-claim-mutation-green.txt`              | PASS            | Restoring the exact production file admitted one consumer.                                                  |
| `terminal-sse-cancellation-mutation-red.txt`   | EXPECTED RED    | Removing the durable terminal guard rewrote a completed run to paused.                                      |
| `terminal-sse-cancellation-mutation-green.txt` | PASS            | Restoring the durable guard preserved terminal state.                                                       |
| `exact-source-browser.txt`                     | PASS            | All 10 real browser → Next → FastAPI → PostgreSQL scenarios on `9ca9d6a`.                                   |
| `exact-source-browser-ac17ca15.txt`            | PASS            | All 10 scenarios rerun after the rendering fix on exact source `ac17ca15`.                                  |
| `exact-source-browser-b334ec798.txt`            | PASS            | All 10 scenarios rerun after identifier filtering on exact source `b334ec798`.                             |
| `exact-source-browser-b334ec798-failures.txt`   | FAILED/EXCLUDED | Two stale local Next route-cache attempts retained and excluded from feature evidence.                      |
| `exact-source-browser-failures.txt`            | FAILED/EXCLUDED | Earlier executable failures and invalid environment/host attempts retained without being counted as passes. |
| `focused-checks.txt`                           | PASS/BLOCKED    | Eval, PostgreSQL, frontend, type-check, and formatting checks on `a92fcfa`; the live-model test is BLOCKED. |
| `final-source-checks.txt`                      | PASS            | Prior whole-feature backend, PostgreSQL, frontend, contracts, lint, Bandit, and Gitleaks checks.            |
| `final-re-review-checks.txt`                   | PASS            | Affected backend, API/type drift, type-check, static checks, Bandit, and Gitleaks on `ac17ca15`.            |
| `provenance-fallback-red-green.txt`            | EXPECTED RED/PASS | Missing secondary-provider bibliography regression before and after fix.                                  |
| `identifier-safety-red-green.txt`               | EXPECTED RED/PASS | Canonical DOI precedence and opaque-identifier leak regression before and after fix.                       |
| `identifier-safety-final-checks.txt`            | PASS            | Backend, API/type drift, type-check, static checks, Bandit, and Gitleaks on `b334ec798`.                    |

The certification uses distinct hashes. `report_hash` is the canonical JSON
report hash. The post-approval audit records a separate SHA-256 for the exact
persisted Markdown prefix. The export-stage `output_hash` binds the complete
persisted envelope, including `report_hash`, `verification_output_hash`, and
the exact Markdown content; changing that content changes `output_hash`.

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
