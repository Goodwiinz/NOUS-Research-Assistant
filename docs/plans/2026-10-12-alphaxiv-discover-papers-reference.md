# alphaXiv `discover_papers`: observed behaviour (Plan 07 Slice 5 reference)

**Status:** reference record, 2026-10-12. Black-box evidence about a third-party service, not a NOUS contract. Amends the Slice 5 section of [the Plan 07 design](2026-10-05-nous-mcp-alphaxiv-parity-design.md). Checked against `origin/develop` `d8c5894d1`.

## Method

- Called alphaXiv's hosted MCP server (`https://api.alphaxiv.org/mcp/v1`, OAuth) from Claude Code twelve times, changing one input per call.
- Parsed the alphaXiv web bundle statically with REA (291 scripts, logged out) to list the routes the web client calls.
- Called the two public search routes the web client uses: `GET /search/v2/paper/fast` and `GET /v1/search/paper`.

The pipeline below is an inference from outputs. The models, index and scoring code are server-side and were not observed.

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

Not observed: embedding and re-ranking models, index coverage, the number of follow-up rounds per difficulty level, and per-call quota cost.
