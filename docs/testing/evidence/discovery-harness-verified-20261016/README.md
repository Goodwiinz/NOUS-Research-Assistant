# Corrected discovery harness decision, 2026-10-16–17 UTC

Classification: **NOT GATED**. The fresh registered result is **0/5 objective
passes**, semantic **N/A**, for **Codex 0.161.0 / Azure gpt-5.6-terra only**.
Under the unchanged four-of-five decision rule, this is **PRIMITIVES
INSUFFICIENT for Codex**. It supports retaining Slice 5 for this pair. It does
not evaluate the current composite tool, deployed MCP/grants or broader
retrieval quality.

- [New source manifest](../../../../evals/source-manifests/agent-flow-2026-10-16-harness-mcp-discovery-verified-http.json)
- [New baseline summary](../../../../evals/baselines/agent-flow-2026-10-16-harness-mcp-discovery-verified-http.json)
- [Registered protocol and amendments](../../../../evals/specs/harness-mcp-discovery-v1/harness.md)
- [Verifier corrections](../discovery-verifier-20261016/README.md)
- [Separate real-provider V5 diagnostic](../discovery-provider-v5-20261016/README.md)

## Frozen declaration and preflight

Execution source is clean commit `5f682a33412ed9eaaf816b9c42ea90784d62b89b`,
tree `79b73c7fe2779cff09cc52e9a5885714fe4e9598`. The real bridge, workspace
lockfile and shared helpers come from declared revision
`57fc21bc740e086c9e5880b1e1f26634c6a0f1c5`; the gateway is a deterministic
double. The current corrected task was copied into a new prepared context.
Its inputs matched after both jobs. No task, fixture, model, CLI version or
decision threshold was changed between attempts.

Before trial 1, the source/context, task/dataset digests, selected model,
Harbor 0.6.6 sources, pinned Codex version and attempt rules were declared.
The pre-registration SHA-256 is
`f508a05b22300d88691a07aa8878cc592cf9d5319e03d2ea6bf878aa81c9bd73`.
Both images built successfully. The pinned-source catalog check passed, all
13 calibrations returned their expected exits inside the main image, and an
unscored Azure reachability request returned HTTP 200. These checks are
separate from the scored agent trials.

The image selects the Azure Responses HTTP provider with WebSockets disabled;
the host did not forward `OPENAI_BASE_URL`. Hosted web search stayed disabled.
Only the needed provider credential was forwarded to the harness. No judge
credential or real NOUS account, database, grant or corpus was used.

## Attempt accounting

| Measure | Result |
| --- | --- |
| Attempts in this baseline | 7 of the declared cap of 10 |
| Scoreable trials | 5 |
| Objective passes | 0 |
| Infrastructure exclusions | 2 |
| Semantic | N/A; deterministic verifier, no judge |
| Earlier campaign attempts | 15, retained separately |
| Campaign attempts across versions | 22; scores are not pooled |

One attempt emitted reward 1 after a provider-stream disconnect and recovery.
The registered provider-error rule excludes it despite that raw reward. A
different attempt failed during agent installation before execution or reward.
Two unchanged replacement attempts completed the five scoreable trials.
All five failed solely because `search_documents` was not invoked through
the bridge with a non-empty query; the harness used project listing instead.
No other objective failure was reported. This is a tool-composition result,
not a semantic judgment that the literature answers were wrong.

The five scoreable agent executions had median duration **21.88 s** and p95
**264.17 s**, using linear interpolation. These are descriptive values for
five observations, with no statistical bound or performance gate. First-token
latency was not collected.

## Runtime evidence and limits

BuildKit export receipts retain OCI image digests for every attempt. Direct
container snapshots were captured for the two replacements; their image
filesystems match the preflight images. The first-series observer missed
snapshots because its `/tmp` path filter did not match Docker's `/private/tmp`
bind sources. Recovered build receipts remain distinct from direct container
observations. This gap is recorded rather than filled with an assumed image ID.

The verifier ran on Python 3.11.2 in the main image. The bridge uses Node
24.21.0; the mock image uses Python 3.11.17. Harbor's host interpreter was
Python 3.12.11. Agent results record the pinned Codex 0.161.0/model pair.
Harbor owns per-attempt teardown; raw traces stay in ignored owner-only
artifacts, outside the image build context.

The old verifier-defective scores and the NOT RUN preparation remain
unchanged historical records. The new result uses no old reward. The task's
five fixed-instruction decision trials are distinct from a full suite gate:
agent phrasing/order variants were not run, and the 13 verifier calibrations
do not substitute for those variants. No GATED promotion is claimed. Requested
Fable benchmark trials remain NOT RUN; OAuth Fable source reviews are separate
evidence. The gateway double, literal citation audit and open installation/model
egress retain the protocol's stated fidelity limits.
