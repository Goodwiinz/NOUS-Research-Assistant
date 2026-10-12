# alphaXiv `discover_papers`: observed behaviour (Plan 07 Slice 5 reference)

**Status:** reference record, 2026-10-12. Black-box evidence about a third-party service, not a NOUS contract. Amends the Slice 5 section of [the Plan 07 design](2026-10-05-nous-mcp-alphaxiv-parity-design.md). Checked against `origin/develop` `d8c5894d1`.

## Method

- Called alphaXiv's hosted MCP server (`https://api.alphaxiv.org/mcp/v1`, OAuth) from Claude Code twelve times, changing one input per call.
- Parsed the alphaXiv web bundle statically with REA (291 scripts, logged out) to list the routes the web client calls.
- Called the two public search routes the web client uses: `GET /search/v2/paper/fast` and `GET /v1/search/paper`.

- Downloaded the remaining 384 code-split chunks of the web app (public static assets) and parsed them, which surfaced the paper-filter controls, the agent-trace viewer and the components of alphaXiv's engineering post on its retrieval agent.
- Read that post and called the retriever sandbox API it documents (four GET requests).

The production pipeline is server-side. The sections "Code-level evidence" and "Inferred pipeline" separate what alphaXiv's own code and writing state from what was inferred from outputs.

## Code-level evidence

**alphaXiv's retrieval agent.** alphaXiv's engineering post [Training retrieval agents for arXiv search](https://www.alphaxiv.org/blog/training-retrieval-agents-for-arxiv-search) describes:

- **Model:** Qwen3-8B post-trained with GRPO on the SkyRL-Agent framework, with parallel tool calls.
- **Tools:** `keyword_search` (arXiv id, date, upvotes, page snippets), `semantic_search` (arXiv id, date, upvotes, abstract), `read_page` (page text) and `finish` (submit a list of document IDs).
- **Gates:** answers are capped at 10 documents, and a citation gate requires every submitted document to have come from a tool call.
- **Rewards:** RLVR on recall (duplication, submission, citation and limit gates; failing a gate scores 0), and rubric rewards from an LLM judge (Gemini 3 Flash in the main run) scoring search diversity, thoroughness, thoughtfulness and relevance. The post reports that recall-only runs collapsed to a single search call, and that rubric rewards produced more thorough searches.
- **Results and scope:** the post places its model just above GPT 5.4 on recall and below Opus 4.6 (0.58). The training corpus was about 150k AI papers. The post does not say this agent serves `discover_papers`.

**Retriever sandbox.** The post's sample request names `https://retriever-sandbox-production.up.railway.app`. On 2026-10-12 it answered unauthenticated GETs:

| Endpoint | Parameters | Response | Observed behaviour |
| --- | --- | --- | --- |
| `/api/search/embedding` | `query`, `limit` | `{results:[{title, abstract, universalId, votes, publicationDate, similarityDistance}], totalResults}` | Distances ~0.27 for a good match (cosine distance). |
| `/api/search/keyword` | `keyword` (`limit` ignored) | `{results:[{universalId, paperTitle, votes, publicationDate, occurrences:[{pageNumber, snippet}]}]}` | Full-text match on page text, 50 rows, sorted strictly by votes descending. The QLoRA paper itself was not in the top 50 for `QLoRA`. |
| `/api/page` | `universalId`, `pageNumber` | page text | Validation error only; not read. |

**Assistant paper filter (web bundle).** The assistant request body carries `filterModel` and `customFilter {url, apiKey}`. An admin "Race Filter Models" lane sets `filterModel: "cohere"`, labelled "Cohere Rerank", and the stream has a `paper_filter_debug` event. So the assistant passes candidate papers through a swappable reranking filter, and Cohere Rerank is one of the options. Which filter `discover_papers` uses was not observed.

**Agent traces (web bundle).** `GET /assistant/v2/messages/{messageId}/trace` and `GET /assistant/v2/trace/{trace}` feed an admin trace viewer that shows a tree of spans with nested model calls, subtree cost and depth. It recognises OpenAI, Anthropic, Google, DeepSeek, Qwen, Kimi, Grok and MiniMax models. Its route sits under `/admin`, so ordinary accounts presumably can't read traces.

**Fit with the black-box results.** A small agent that calls keyword and semantic search, reads pages, and submits up to a capped list explains both the non-determinism and the result counts below. A keyword channel sorted by votes explains why popular papers win `historical` and why the 2020 RAG paper only appeared once its exact title terms were used. The production corpus is wider than the post's 150k AI papers: `discover_papers` returned 2014 mathematics papers. The production setup can therefore differ from the post.

The pipeline below is the black-box reading of `discover_papers` outputs.

## Tool surface on 2026-10-12

The server listed 23 tools, not the nineteen the design counted: the research, researcher and library groups, plus `get_paper_notes` and `list_annotated_papers` (the user's private highlights and notes).

`discover_papers` inputs: `keywords` (required), `question` (required), `difficulty` 1–10 (required), `prioritize` (`default`, `recency`, `historical`, `popular`), optional `published_after` / `published_before`. The tool description tells the caller to pass only terms the user stated, never expansions, and to make at most two searches per message.

Output: one line per paper with `[ID=…]`, title, link, first publication date, organizations, votes, views and an abstract snippet. "No papers found for the given query." when nothing survives.

## Inferred pipeline

| Step | Behaviour | Evidence |
| --- | --- | --- |
| 1. Collect | Two channels: lexical match on `keywords`, dense match on `question`. | "teaching robots to fold clothes" returned garment-folding papers whose titles never say "laundry" (dense). An origami paper titled "…Fold Architectures" appeared (lexical). `keywords=["Q2D-Web"]` returned exactly that paper. |
| 2. Re-rank and cut | Candidates are re-ranked against `question`; the cut is relative to the best score, not a fixed top-k. | `keywords=["GRPO"]` with the cloth-folding question returned zero GRPO papers. Result counts: 1 (exact name), 14–25 (broad topics), 2 unrelated papers for a nonsense query. A model judge would have dropped those 2; a relative score cut keeps them. |
| 3. Filter and order | Date bounds and `prioritize` act on the collected candidates only; they never widen the search. | A 2020 window with generic RAG terms returned "No papers found" although 2005.11401 (first published 2020-12-07, 218 votes) is in the corpus. With its title terms as keywords the same window found it, and `historical` then ranked it first. |
| 4. Vary | Some step is non-deterministic, probably a model rewriting the query or re-ranking. | Two identical calls kept the same top four and changed the tail. `difficulty` 1, 5 and 10 gave 20, 17 and 16 results from the same ~22-paper pool, at about 7.6 s for difficulty 1 and 11 s for difficulty 10, both including client turn overhead. |

Other observations:

- `prioritize=default` skews to roughly the last twelve months. `historical` weights lifetime votes and views. `recency` orders roughly newest first.
- The output date is `first_publication_date`; votes are `public_total_votes`; views are `visits_count.all` (from the hydrated `GET /v1/search/paper` record).
- The two-searches-per-message limit is guidance only: three parallel calls in one turn all succeeded.
- The web site's search box is a different system. `GET /search/v2/paper/fast` returns web-search-style snippets for arXiv pages, and `GET /v1/search/paper` hydrates those same IDs from alphaXiv's database. For the same query they returned eight papers, none of which `discover_papers` returned.

## Implications for Slice 5

The design keeps Slice 5 gated on a Harbor eval showing harnesses fumble the primitives. That gate is unchanged. If the eval opens the slice, these are the reference behaviours:

1. Take both `keywords` (lexical) and `question` (dense) and collect from both channels. NOUS live retrieval is PostgreSQL full-text, so the dense channel is a new dependency and needs its own decision.
2. Re-rank against `question` and cut relative to the best score, so an exact-name query returns one paper instead of padding.
3. Apply date bounds and recency inside retrieval, not after it. This avoids the empty-window failure observed in step 3.
4. Keep the one-line-per-paper output with date, organizations, votes or citation signal, and a snippet.
5. Copy the tool-description rule that keywords are the user's own terms only.
6. Treat it as a small agent loop, not a single query: keyword search, semantic search, page reads, then a capped submit list where every submitted paper must come from a tool result. NOUS already has the primitives (`search_arxiv`, `search_documents`, `retrieve_passages`), so the loop is the new part.
7. Don't sort the keyword channel by votes alone. alphaXiv's sandbox does, and that is why an exact-topic paper can lose to popular papers that only mention the term.

Not observed: the embedding model, which reranking filter `discover_papers` uses, whether production runs the fine-tuned Qwen3-8B agent, how `difficulty` maps to rounds, and per-call quota cost.
