# Local discovery provider diagnostic

This companion to GOO-435 S5d and GOO-436 uses real Cohere requests and a
disposable local PostgreSQL/pgvector database. It is **NOT GATED** and is not
an agent, deployed MCP, or publication-quality retrieval benchmark.

`fixture.json` fixes twelve synthetic documents, relevance labels, eight
canonical queries, one date-window case, and long/short page cases before
execution. The eligible top-k is three, below the corpus size. A duplicate in
another organization is an isolation control. Page documents are added after
recall. The runner records exact source hashes and refuses changed inputs,
existing output files, or a non-loopback database target.

Declare first, then run once with the existing provider credentials injected
through the approved local secret manager. `NOUS_PROVIDER_PROOF_DATABASE_URL`
must name an owned disposable server; the runner creates/drops only its new
`provider_probe_*` database. Never point it at shared data.

Use a prepared Python environment with the repository backend dependencies;
managed worktrees do not necessarily contain `backend/.venv`. Set
`NOUS_PROBE_PYTHON` to that interpreter. Inject the same provider configuration
for declaration and execution. Hugging Face and Transformers are forced
offline; only Cohere requests may use the network in this diagnostic.

```sh
PYTHONPATH=backend "$NOUS_PROBE_PYTHON" \
  evals/discovery-provider-smoke-v1/probe.py --declare /tmp/discovery-declared.json
PYTHONPATH=backend "$NOUS_PROBE_PYTHON" \
  evals/discovery-provider-smoke-v1/probe.py --run /tmp/discovery-declared.json \
  --output /tmp/discovery-result.json
```

Report measured recall and execution failures separately. There is no release
threshold. A provider/migration error is infrastructure, not a failed label.
Only process-local settings enable embedding, semantic search and page
filtering. The runner never changes a deployment or runs a corpus backfill.

## 2026-10-16 collector amendment

The first declared runner reached the long-page case, where the configured
rerank endpoint returned HTTP 404. Its exception discarded the preceding
query measurements before writing the result. That attempt is retained as
infrastructure with recall unrecorded, not as a zero or passing score.

The revised runner preserves query-phase results before page cases and records
page availability independently, including the short-document skip. This
collector change requires a new declaration and a new attempt; it cannot reuse
the first attempt's unrecorded metrics. Labels, provider models and corpus are
unchanged, with no release threshold. Each declared runner baseline has one
attempt; the campaign retains all declared versions. Raw source snapshots, results
and logs stay under ignored `evals/jobs/` with owner-only permissions.

Commands initially write to the chosen local output directory; completed
attempts are copied into ignored `evals/jobs/` for retention. The second runner
completed successfully but failed the subsequent standalone typing gate on its
JSON row containers. Its exact inputs and positive result remain historical.
Explicit row annotations and independently retained page outcomes require a
third declaration before execution. All versions remain counted separately;
no labels, corpus, provider models or release thresholds are changed.
