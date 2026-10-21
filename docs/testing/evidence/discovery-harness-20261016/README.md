# Historical MCP discovery decision (2026-10-16)

Scope: GOO-435's registered primitive-composition decision. This is not a
completion claim for GOO-435 or the full twelve-issue goal.

| Boundary | Observed result |
| --- | --- |
| Real bridge and lockfile | Pinned `57fc21bc740e086c9e5880b1e1f26634c6a0f1c5` inputs; 92 prepared input files checked; a second preparation produced identical input hashes |
| Gateway, relevance and source identities | Deterministic doubles; no deployed backend |
| Verifier calibration | 8/8 expected exit codes, including objective failures and infrastructure exit 2 |
| Initial Codex series | Five attempts excluded for provider WebSocket HTTP302 errors, although HTTPS recovery completed them |
| Replacement HTTP series | Five scoreable trials; objective 1/5; semantic N/A; infrastructure 0 |
| Registered decision | PRIMITIVES INSUFFICIENT, Codex-only |
| Fable benchmark | NOT RUN: Bedrock Fable 5 and 5.1 invocation denied |
| Current composite, live ranking and labelled recall | Not measured by this task |

Harbor 0.6.6; Codex 0.161.0; Azure deployment `gpt-5.6-terra`; custom HTTP
Responses provider with `supports_websockets=false`. The replacement task digest
is `sha256:1c2006820db7721a59772133e531e92da650be95918ce5fcee2946fc612a4340`.
The fixture/verifier dataset is unchanged from pre-registration. Model
credentials were forwarded only to their provider; they are absent from task
configuration and committed records.

Three failed trials used `list_project_documents` and one used only
`retrieve_passages` instead of the required
`search_documents`. All five cited served identifiers without fabricated ids
or bridge bypasses. The objective result does not imply poor semantic answer
quality, and the verifier/threshold were not relaxed after observing it.
Agent elapsed p50 was 30.223 s and p95 160.247 s (linear interpolation, n=5).
First-token and terminalization latency were not captured. Final per-trial
image digests were not retained after Harbor cleanup; only the declared pinned
base-image digest is recorded. The baseline remains **NOT GATED**.

The machine-readable [baseline](../../../../evals/baselines/agent-flow-2026-10-16-harness-mcp-discovery-http.json)
and immutable [pre-run source manifest](../../../../evals/source-manifests/agent-flow-2026-10-16-harness-mcp-discovery-http.json)
record trial checksums, verdicts, source hashes, token counts and raw-artifact
references. Raw traces for both series remain under ignored `evals/jobs/`;
none are committed. The [harness specification](../../../../evals/specs/harness-mcp-discovery-v1/harness.md)
contains the commands, exclusions and ten-attempt cap.

## Corrected-task baseline (2026-10-16)

Delivery typing checks required explicit predicate/response/exit-code types in
the inherited task. Those edits changed task and verifier digests, so the prior
HTTP 1/5 record above remains historical and does not score this task. All eight
calibration exits were checked again before the fresh series. Five new trials
completed with **0/5 objective passes**, semantic **N/A**, and **zero provider or
infrastructure failures**. The registered Codex-only decision remains
**PRIMITIVES INSUFFICIENT**. No historical scores were combined or selected.

Every new trial called `list_project_documents` but skipped the required
`search_documents`; some also used `retrieve_passages`. Native Codex event logs
show the calls going through the `nous` MCP server. Every answer passed the
served-identifier audit. This measures the declared tool-use objective, without
a semantic judge. Current-task agent elapsed p50 was **49.040 s**, p95
**104.497 s** (linear interpolation, n=5). Fifteen attempts were made across the
three task versions; this corrected baseline used five of its ten-attempt cap.

Task digest:
`sha256:7a418755cd27c70b9e7c7619783e0bf77252c96a26b62e32ac813ea811c8ccf0`.
Dataset/verifier digest:
`sha256:87ecd715c341456756e68c27070fd943a9c209877ee1617ee243b9f77dad2b39`.
The [corrected baseline](../../../../evals/baselines/agent-flow-2026-10-16-harness-mcp-discovery-typed-http.json)
and [pre-run manifest](../../../../evals/source-manifests/agent-flow-2026-10-16-harness-mcp-discovery-typed-http.json)
are the current record. Status remains **NOT GATED**. The pinned historical
bridge, deterministic gateway and live/current-composite limits above apply.
