# Paper discovery

Research-engine search steps support `openalex`, `semantic_scholar`, `pubmed`,
`crossref`, `arxiv`, and organization-scoped `rag_store`. HTTP execution and
the worker-side factory share `connectors/registry.py`. The legacy `web` alias
still uses Semantic Scholar; source provenance records its actual name.

## Use

In the research blueprint editor, select OpenAlex in **Search sources**, then
save the blueprint. The selector updates the search steps as well as the global
parameters. Existing saved blueprints keep their configured providers.

Example search step:

```json
{
  "type": "search",
  "name": "Find papers",
  "parameters": {
    "sources": ["openalex", "semantic_scholar", "pubmed"],
    "query_template": "$query",
    "max_results": 50
  }
}
```

Set `query` in the blueprint parameters. `max_results` accepts 1–200 per
provider; results are bounded discovery, not exhaustive systematic-review
retrieval. With no step-specific providers, search uses the global source
selection. The selected provider names are preserved separately from results
so repeated searches and resume do not mistake paper titles for providers.

Optional server settings:

- `OPENALEX_API_KEY`: sent in the Authorization header, never in the URL.
- `SEMANTIC_SCHOLAR_API_KEY`: sent in the provider's `x-api-key` header.
- Existing `NCBI_API_KEY` and `CROSSREF_MAILTO` settings remain supported.
- `REDIS_URL`: coordinates request pacing across research workers. During
  outages the limiter logs a warning and falls back to per-process pacing.
  This fallback cannot enforce a deployment-wide quota.

OpenAlex API reference: [authentication](https://help.openalex.org/api/authentication/)
and [works](https://help.openalex.org/data/works/).

## Evidence and identity

The existing title list in `output.sources` remains compatible. Additional
`output.source_records` contain source IDs, abstracts, URLs, metadata,
content hashes, evidence level, and original provider snapshots with retrieval
timestamps. An open-access URL alone never sets a record's full-text evidence
level. Local RAG content is conservatively labelled `excerpt`, since retrieval
may supply only a snippet. No PDF downloads or embedding jobs run as part of discovery.

Deduplication applies within each search, using normalized DOI and exact
provider identifiers. Records connected by an identifier bridge can merge.
Conflicting shared identifiers prevent merging; arXiv revision suffixes are
preserved. Equal titles alone never establish identity. Local RAG documents
are kept separate from public metadata.

On streamed step completion, source rows and step output are committed
together using the existing `research_sources` and `research_steps` tables.
Source IDs link the table rows to the stored output. Resume restores the
stored source records, including abstracts and provenance, into later steps.
Source rows belong to the run; no global public catalog or cross-tenant
document cache is introduced. No database migration is required.

LLM prompts retain evidence content, content hashes, URLs, and provider metadata,
but omit per-retrieval row UUIDs and audit timestamps. This keeps identical
evidence reproducible without removing audit data from stored steps or sources.

Provider requests run concurrently with a 90-second deadline per provider.
OpenAlex and Semantic Scholar follow pagination up to the configured result
limit. Shared HTTP handling paces requests, retries transient network/server
errors, and respects bounded Retry-After cooldowns. Longer cooldowns fail
visibly rather than being retried prematurely. arXiv retains its existing
provider-specific gate and retry implementation.

`output.coverage` records provider successes/failures and retrieval time. A
partial failure retains successful results and displays a warning in step
progress. All providers failing fails the run. Error payloads include only
exception types, not exception messages that may contain credential URLs.

## Scope

This release supports on-demand discovery in research blueprints. Scheduled
alerts, a persistent global paper catalog, full-text import for new providers,
bulk snapshots, and a dedicated paper-result browsing screen are later phases.
Research step details expose structured output through the existing viewer.

## Verification

Focused backend tests:

```sh
pytest -q backend/tests/unit/services/test_paper_discovery.py \
  backend/tests/unit/services/test_paper_provider_http.py \
  backend/tests/unit/services/test_source_connectors.py \
  backend/tests/unit/services/test_workflow_engine.py \
  backend/tests/unit/api/test_research_engine_stream.py \
  backend/tests/unit/tasks/test_research_task_tenant_scope.py
```

Frontend: `pnpm --dir frontend test src/components/research-engine/__tests__/StepProgress.test.tsx`.
