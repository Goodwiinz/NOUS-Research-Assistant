# Complete-checkout provider diagnostic, 2026-10-16

Classification: **NOT GATED**, synthetic local service/provider diagnostic.
One newly declared V5 attempt completed at **22:18:29 UTC**, using clean source
`6970a07177573576e2dd22882ed30c1d5d097bc6` and Git tree
`21843ea00674916143bba9c05b8f2975a6003d3e`. Its complete source/runtime/provider
identity matched again after scratch cleanup. This exercises the new source
guard with real migrations, imports, Cohere requests and PostgreSQL; it does not
establish deployed agent/MCP quality or release acceptance.

- [Source manifest](../../../../evals/source-manifests/discovery-provider-2026-10-16-v5.json)
- [Result summary](../../../../evals/baselines/discovery-provider-2026-10-16-v5.json)
- [Unchanged fixture](../../../../evals/discovery-provider-smoke-v1/fixture.json)
- [Runner instructions](../../../../evals/discovery-provider-smoke-v1/README.md)
- [Earlier V3 evidence](../discovery-provider-20261016/README.md)
- [Verifier/source-integrity calibration](../discovery-verifier-20261016/README.md)

## Declaration and runtime

The pre-execution declaration and manifest were written before the runner's
first provider request. The retained manifest SHA-256 is
`39dcafd5c3f02674146846e414e12947748176a17a57df2c0bfb2f47c86ed526`.
The source manifest records that declaration, runner/fixture/source hashes,
Python/package versions, provider endpoint hashes, process-local flags and
the actual runtime image digest. Generated traces and credentials remain
outside tracked source in ignored, owner-only artifacts.

The owned loopback database ran PostgreSQL 15.19 and pgvector 0.8.7 on Linux
arm64, using the locally retained digest-pinned pgvector image. The complete
Alembic chain reached `jd01_document_embeddings`. Both application engines
resolved to the owned scratch database. Thirteen 1024-dimensional
`cohere:embed-v-4-0` rows were stored. Only the configured Cohere embedding and
rerank providers were called; Hugging Face and Transformers stayed offline.
No saved provider configuration, deployment flags or user corpus changed.

## Separate V5 measurements

The fixture, labels, models and metrics are unchanged; no release threshold
was introduced. Eight canonical queries and one date-window query completed.

| Canonical macro measure | Semantic | Composite |
| --- | --- | --- |
| Recall at 1, eight queries | 0.9375 | 0.9375 |
| Recall at 3, eight queries | 1.0 | 1.0 |

The date-window query retained its labelled target at one and three for both
channels. The organization-wide semantic control excluded the duplicate in
the other organization. The long document selected six of fourteen pages,
including both labelled relevant pages; gold-page recall at six was 1.0.
The three-page document kept all three pages and short-circuited the reranker.
Selection of six pages does not imply all six are relevant.

No infrastructure or objective failure was recorded. Source identity matched
after cleanup, the scratch database was removed, and a separate post-run
inventory found zero scratch databases. The owned container was stopped and
removed successfully.

V1–V3 results and the V4 NOT RUN preparation remain unchanged; results are not
pooled. V5 establishes the complete-checkout guard only at its declared source
and runtime. The corrected Harbor task remains NOT RUN. Its historical 0/5
result remains archival under the verifier-defect invalidation rule. Deployed
MCP/grant journeys, broader labelled evaluation and owner acceptance remain open.
