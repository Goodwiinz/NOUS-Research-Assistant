# Discovery verifier and source-integrity corrections

Source parent: `8c7aa67638e9292fc13bc2c873a34293f324e88f`. The final review
delta is bound by declared file hashes; its published head is recorded in the PR.
This is host calibration evidence. The corrected Harbor task and provider
diagnostic are **NOT RUN** as new scored experiments.

Four synthetic cases incorrectly exited 0 before the change: required searches
returning application errors, citations with no trial-returned IDs, a separate
wholly unsupported paper list, and a truncated answer. The corrected exits are
2 for service failure and 10 for each invalid answer. The original positive
fixture still exits 0. All eight original calibration files retain their bytes
and expected exits; five new fixtures exercise these boundaries and recovery
from an invalid query.

Successful calls require HTTP 200 and `is_error: false`. Citation aliases are
bound to papers actually returned in this trial, including versioned arXiv IDs
and a returned PubMed paper's DOI. Every detected top-level list/table paper row
is inspected. A service failure invalidates the trial even after a successful
retry; a rejected bad query followed by a valid result remains scoreable.
Truncation prevents an objective pass because the full answer was not audited.

The provider identity previously ignored an unlisted model-file change. An
isolated Git fixture reproduced identical identities despite that change.
The new guard requires a clean complete Git-visible checkout, records its HEAD
and tree, and checks identity after cleanup. Tests cover listed, unlisted,
staged and untracked changes; ignored raw artifacts; a different clean commit;
and mid-run source changes. Simulated database cleanup completes before a
changed-source measurement is classified as infrastructure. CLI source refusals
exit2 before writing a result; failed post-cleanup Git inspection preserves
the cleanup metadata and an infrastructure result. These tests do
not execute a real provider or PostgreSQL measurement.

Validation: the final declared working patch passed 36 distinct host cases with
zero failures or skips. All declared code/task inputs remained unchanged
across the run. Its JUnit SHA-256 is
`d995bf4e94eda65ae75027c9b45fedd604417fdb6881bc6569862d0870f89a89`.
The earlier overlapping 32-case matrix passed at clean source parent8c7aa676;
it remains separate evidence and is not added to the final case count. The
four added cases cover CLI declaration/execution refusal, a Git-inspection
failure after cleanup, and valid method notes in prose.
Pinned Ruff 0.15.15, Black 26.5.1, isort 5.13.2 and mypy 1.7.1 passed all four
changed/added Python files; the mypy run includes `--disallow-untyped-defs`.
The first Fable adjudication timed out without a verdict and is retained as
partial evidence. Final independent reviews are recorded in the PR handoff.

The pinned task preparation helper successfully archived the declared bridge
at `57fc21bc740e086c9e5880b1e1f26634c6a0f1c5` with the corrected current task.
Preparation is not an image build or a Harbor trial. The new dated records are
[task manifest](../../../../evals/source-manifests/agent-flow-2026-10-16-harness-mcp-discovery-verified-results.json),
[task baseline](../../../../evals/baselines/agent-flow-2026-10-16-harness-mcp-discovery-verified-results.json),
[provider manifest](../../../../evals/source-manifests/discovery-provider-2026-10-16-v4.json)
and [provider baseline](../../../../evals/baselines/discovery-provider-2026-10-16-v4.json).

The historical typed-HTTP 0/5 record and provider V3 metrics/manifests are
byte-identical to their prior versions. The task's whole-run invalidation rule
applies after a verifier defect: previous scores and identifier-audit passes
cannot establish acceptance for this revision. V3 records its fifteen declared
inputs; it does not establish the new complete-checkout source guard. New
authorized, predeclared canonical and near-boundary trials are required before
a current capability decision. Live composite/MCP quality remains a separate
acceptance requirement.

## 2026-10-16 22:18 UTC provider execution amendment

The separate [V5 diagnostic](../discovery-provider-v5-20261016/README.md)
completed one new provider/PostgreSQL attempt at clean source `6970a071`, with the
complete source identity matching after cleanup. The V4 NOT RUN preparation
and this earlier host-only proof retain their recorded scope. The corrected
Harbor task remains NOT RUN; V5 does not supply an agent capability decision.
