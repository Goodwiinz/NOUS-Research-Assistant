# Local discovery provider diagnostic, 2026-10-16

Classification: **NOT GATED**, synthetic local service/provider diagnostic.
This is separate from the historical Harbor primitive-composition experiment
in the same PR. Neither result establishes deployed agent quality or release
acceptance for GOO-435 or GOO-436.

## Frozen inputs and execution

The third declaration was written before execution after pinned Ruff, Black,
isort and standalone mypy passed. Four metric calibration cases passed.
The declaration records base Git revision
`28de2ffa5402e4ce0baf6759cf5ccfc93c640513`, the explicit dirty-source state,
and SHA-256 hashes of all fifteen executed source inputs. Those hashes matched
the retained source snapshot after execution. The final commit can be checked
against the same file hashes; the base revision alone is not the tested tree.

- [Declaration](../../../../evals/source-manifests/discovery-provider-2026-10-16-v3.json)
- [Result](../../../../evals/baselines/discovery-provider-2026-10-16-v3.json)
- [Runner and reproduction instructions](../../../../evals/discovery-provider-smoke-v1/README.md)

The fixture fixes twelve eligible synthetic documents, an identical-text
document in another organization, eight canonical queries, one date-window
case and labelled long/short page cases. Top-k is three, below corpus size.
Page documents are seeded after the query phase. Real Cohere embedding and
rerank requests were used with native PostgreSQL 15.19, pgvector 0.8.7 and
the complete Alembic chain through `jd01_document_embeddings`. Thirteen
1024-dimensional `cohere:embed-v-4-0` embedding rows were stored. The extra
organization-wide semantic query checks that the other organization's
duplicate is excluded. Other semantic queries and every composite query
also constrain a project; the explicit organization-wide control covers
the semantic channel.

Only an owned loopback PostgreSQL server was used. The runner created its own
unique database, bound both backend database settings before imports, verified
the actual engines and removed the scratch database afterwards. Feature flags
were process-local. Hugging Face and Transformers were offline. No deployment,
live flag, user corpus, arXiv call, grant-HTTP journey or backfill was used.

## Third attempt result

Completed at **2026-10-16 11:12:38 UTC**, with no recorded infrastructure or
objective failure and confirmed scratch-database cleanup.

| Measure | Semantic | Composite |
| --- | --- | --- |
| Canonical macro recall at 1, eight queries | 0.9375 | 0.9375 |
| Canonical macro recall at 3, eight queries | 1.0 | 1.0 |
| Macro recall at 3 including date-window case | 1.0 | 1.0 |

One canonical query has two relevant targets, so its maximum recall at one is
0.5; the resulting canonical macro ceiling is 0.9375. These descriptive scores
have no release threshold and do not generalize beyond this fixed fixture.

The fourteen-page document selected pages **1, 3, 9, 11, 12 and 14**, including
both labelled relevant pages (3 and 11). Selection of six pages does not imply
all six are relevant. The three-page document kept pages **1, 2 and 3**, recorded
`short_circuited=true` and did not invoke the reranker.

## Endpoint defect and causal checks

The configured Azure Cohere rerank URL ended at the SDK base `/openai/v1/`.
The existing direct HTTP adapter posted there verbatim and received 404.
A same-origin request to `/providers/cohere/v2/rerank`, with the same model and
Bearer credential, returned 200. The source now normalizes only the exact SDK
base path on recognized Azure host suffixes; complete, custom and native Cohere
URLs retain their existing behavior. Host, query, payload and authentication
are preserved. No saved provider configuration was changed.

Fourteen HTTP transport cases cover synchronous/asynchronous routing, full
endpoints and lookalike hosts. Restoring the original adapter caused six
relevant failures; restoration of the fix passed the combined rerank/read-tool
suite (**367 tests**). Independent read-only Fable correctness and security
reviews found no blocking adapter defect, with false read-only tripwires.

## Attempt accounting and limitations

All three declarations and their raw sources/results/logs are retained under
ignored, owner-only `evals/jobs/discovery-provider-20261016-v*/` directories.
Each changed runner baseline has one attempt; scores are not pooled.

1. V1: provider 404 during long-page selection. The original collector lost
   earlier query measurements. Recall is unrecorded, not zero or passing.
2. V2: completed with positive measurements, but the subsequent standalone
   typing gate found missing JSON-container annotations. Its exact inputs and
   results are historical and are not attributed to the corrected runner.
3. V3: annotations and independent page-result retention were checked before
   declaration. Frozen labels, corpus and provider models were unchanged.

The earlier Harbor current-task baseline remains **0/5 objective success,
zero infrastructure exclusions**, classified Codex-only primitives
insufficient and **NOT GATED**. A successful synthetic provider diagnostic
does not replace that agent result. Deployed MCP/grant execution, broader
labelled retrieval evaluation, owner acceptance and live rollout remain open.
## Forward source guard

The historical V3 declaration covered fifteen explicit files. A later isolated
Git reproduction showed that modifying an unlisted imported model left that
identity unchanged. The forward runner now requires a complete clean checkout
and verifies its HEAD/tree again after cleanup. The new V4 record is NOT RUN;
the V3 measurements are preserved without being reused for the changed guard.
See [verifier/source-integrity evidence](../discovery-verifier-20261016/README.md).

## 2026-10-16 22:18 UTC execution amendment

A separate [V5 diagnostic](../discovery-provider-v5-20261016/README.md) now
exercises the complete-checkout guard at clean source `6970a071`. Its full identity
matched after real provider/PostgreSQL execution and cleanup. This V3 record
and the V4 NOT RUN preparation are unchanged; no measurements are pooled.
