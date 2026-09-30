# GOO-291 — draft downloads (Markdown/APA and LaTeX/BibTeX): evidence (2026-09-30)

- Backend host: `https://dev-api.goodwiinz.tech`
- Deployed image SHA: deployed SHA not readable from this session (`kubectl` failed: AWS session expired, `aws login` required; `/health` returns only `version: 1.0.0`). For reference, the GitOps-pinned image on `develop` (`infrastructure/helm/knowledge-graph-analytics/values-aws.yaml`) is `5ee337cfed3e2394c2246f5b7b35af6133c5793e`. That commit contains #1730 (`acb5edb58`) and not #1742, which fits `D1-03` still returning 500. GitOps PR #1757 (`e67eb5f`, JIT-provisioning fix) was still OPEN at the time of this run.
- Principal: owner only (`allocs16@gmail.com`, user `87d4b23a-dc89-4c9a-817e-e9ca9e42fd2a`, org `e050bd43-…`).
- Raw requests/responses: `2026-09-30-live-evidence/` (index in `journey.md`).

## Which draft

The eval project `2f7056ed-b0fb-488a-8290-8bf1c8502652` still has **no persisted draft** (`D2-05` total 0, `D2-04` 404). A fresh generation with 12 documents was blocked by citation review; see `2026-09-30-goo-292-review.md`. The downloads were therefore taken from the owner's existing draft with the most citations: project `a7460d29-feee-456b-abf2-441a8008921a` ("Attention Is All You Need"), draft `1f01336d-2c57-44cd-a4a5-2924533ab12d` (v4, current, created 2026-09-15, 6 distinct `[Doc N]` citations, `F1-01`/`F1-02`). Export output is rendered at download time from the saved citations and document metadata, so it exercises the deployed export code.

## Downloads

| request | status | bytes | sha256 |
|---|---|---|---|
| `POST …/drafts/1f01336d-…/export?format=markdown&bib_format=apa` (`F1-03`) | 200 `text/markdown` | 14722 | `8d34856e558ad6cf1a3dff25b3d971508f8312bdd8c1fbbefd221a94ff856708` |
| `POST …/drafts/1f01336d-…/export?format=latex` (`F1-04`) | 200 `application/zip` | 6057 | `37d92ec2e622d1497d17ae9287f7854223153cf7305ce28434135b92a7603aed` |

Note that the export route is `POST`, not `GET` (`backend/src/api/research/drafts.py`, `export_draft`). The ZIP contains `Literature_Review_-_Transformer_architecture_and_self-attention_as_introduced_in.tex` (14242 B) and `references.bib` (1413 B). Checksums come from `shasum -a 256` on the downloaded bodies; the same values are recorded as `response_sha256` in the JSON files.

## Parse assertions (43/49 pass, 6 fail)

The `.bib` was parsed with `bibtexparser` 1.4.4. Each `docN` entry was compared with the cited document's metadata from `GET /api/v1/documents/{id}` (`F1-05`…`F1-10`).

| check | result |
|---|---|
| bib entry exists for every citation (doc1–doc6) | PASS ×6 |
| title == `metadata.title` | PASS ×6 |
| authors == `metadata.authors` (order preserved) | PASS ×6 |
| **year == year of `metadata.publication_date`** | **FAIL ×6**: the bib has no `year`; metadata years are 2017, 2020, 2017, 2020, 2025, 2018 |
| venue == `metadata.venue` / `journal_reference` | PASS ×6 (both null; no venue emitted) |
| DOI == `metadata.doi` | PASS ×6 (both null; no DOI emitted) |
| eprint == `metadata.arxiv_id` | PASS ×6 |
| References section has no abstract/snippet text (40-char windows of each document's description) | PASS ×6 |
| every `[Doc N]` marker in the body has a `[Doc N]` References entry | PASS (markers 1–6, entries 1–6) |

The Markdown APA References section also omits the year. Entries read `Vaswani, A., … & Polosukhin, I. Attention Is All You Need. arXiv:1706.03762v7`, with no `(2017)` and no `(n.d.)`.

### Defect: year dropped for arXiv documents

`DraftGenerationService._canonical_citation_records` (document fallback branch, `backend/src/services/research/draft_generation_service.py` on `develop` `c16b201e0`) reads `year=metadata.get("year")`. arXiv-ingested documents store only `metadata.publication_date` (for example `2017-06-12T17:57:34+00:00`), so every such citation exports without a year in both BibTeX and APA. This is present on current `develop`, not only on the deployed image.

### Script used

```python
import json, glob, re, zipfile, sys
import bibtexparser
EV = "/Users/goodwiinz/development/RAG_system-wt-evidence3/docs/audits/2026-09-30-live-evidence"
DL = sys.argv[1]
cites = json.load(open(f"{EV}/F1-02-draft-citations.json"))["response"]["citations"]
docs = {}
for f in glob.glob(f"{EV}/F1-*-get-document-doc*.json"):
    r = json.load(open(f))["response"]; docs[r["id"]] = r
bib_txt = zipfile.ZipFile(f"{DL}/draft.zip").read("references.bib").decode()
entries = {e["ID"]: e for e in bibtexparser.loads(bib_txt).entries}
md = open(f"{DL}/draft.md", encoding="utf-8").read()
body, refs = md.split("## References", 1)
results = []
def check(name, ok, detail=""):
    results.append((name, ok, detail)); print(("PASS" if ok else "FAIL"), name, detail)
for c in sorted(cites, key=lambda c: c["citation_index"]):
    n = c["citation_index"]; m = docs[c["document_id"]]["metadata"]; e = entries.get(f"doc{n}", {})
    check(f"doc{n} bib entry exists", bool(e))
    check(f"doc{n} title", e.get("title") == m.get("title"), e.get("title", ""))
    check(f"doc{n} authors", [a.strip() for a in e.get("author", "").split(" and ")] == m.get("authors"))
    yr = (m.get("year") or (m.get("publication_date") or "")[:4]) or None
    check(f"doc{n} year", e.get("year") == (str(yr) if yr else None), f"bib={e.get('year')} metadata={yr}")
    venue = m.get("venue") or m.get("journal_reference")
    check(f"doc{n} venue", (e.get("journal") or e.get("booktitle") or e.get("howpublished")) == venue, f"bib={e.get('journal') or e.get('booktitle')} metadata={venue}")
    check(f"doc{n} doi", e.get("doi") == m.get("doi"), f"bib={e.get('doi')} metadata={m.get('doi')}")
    check(f"doc{n} arxiv eprint", e.get("eprint") == m.get("arxiv_id"), e.get("eprint", ""))
    # evidence/abstract text must not leak into the References section
    abstract = m.get("description") or m.get("abstract") or ""
    leak = [abstract[i:i + 40] for i in range(0, max(0, len(abstract) - 40), 20) if abstract[i:i + 40] in refs]
    check(f"doc{n} no abstract/snippet text in References", not leak)
markers = set(map(int, re.findall(r"\[Doc (\d+)\]", body)))
ref_ids = set(map(int, re.findall(r"^\[Doc (\d+)\]", refs, flags=re.M)))
check("every [Doc N] marker has a References entry", markers <= ref_ids, f"markers={sorted(markers)} refs={sorted(ref_ids)}")
print(f"\n{sum(ok for _, ok, _ in results)}/{len(results)} passed")
```

Run as `PYTHONPATH=<bibtexparser dir> backend/.venv/bin/python check291.py <download dir>`. `bibtexparser` is not installed in `backend/.venv`; it was installed into a scratch directory.

## NOT RUN

| step | reason |
|---|---|
| Collaborator download → 200 | Needs a second provisioned user. Non-owner principals get 500 until GitOps PR #1757 (JIT provisioning) deploys; it is OPEN. |
| Foreign-org download → 404 | Same #1757 dependency. |
| Download of a draft from the eval project `2f7056ed…` | No draft there can persist on the deployed backend: the grounded-evidence gate blocks every candidate (see GOO-292). |
