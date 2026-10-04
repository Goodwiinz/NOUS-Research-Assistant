# GOO-291 — collaborator draft downloads and BibTeX years: evidence (2026-09-30b)

- Backend host: `https://dev-api.goodwiinz.tech`, image `8ad2c82` (includes #1768/#1772 per the dispatch; pod SHA not read, AWS session expired).
- Raw requests/responses: `2026-09-30b-live-evidence/` (`D*` steps; index in `journey.md`).

## Draft `1f01336d-2c57-44cd-a4a5-2924533ab12d` (project `a7460d29-…`, workspace `d7612a30-…`)

| check | evidence | result |
|---|---|---|
| Same-org user, not a member | `D1-01` | 404 |
| Foreign org, Markdown and LaTeX | `D2-01`, `D2-02` | 404, 404 |
| Owner temporarily adds `approver` (same org) and `supervisor` (other org) as **viewer** | `D2-03`, `D2-04` | 201, 201 |
| Same-org collaborator: Markdown+APA | `D2-05` | 200, 14770 B, sha256 `9e6b5de447d8ba540b4306e0c6d46794f0f25610019370e50030dcc168faca25` |
| Same-org collaborator: LaTeX ZIP | `D2-06` | 200, 6077 B, sha256 `a4b2ada516a87aae55c0b3a5c47ac5734d4eb282b2e9bb83647c75541c9575b2` |
| Owner downloads the same draft | `D2-09`, `D2-10` | byte-identical to the collaborator's (same sha256) |
| Cross-org workspace viewer | `D2-07`, `D2-08` | 404, 404 (drafts are org-scoped) |
| Members removed again; collaborator loses access | `D2-11`, `D2-12`, `D2-13` | 204, 204, then 404 |

### BibTeX / APA re-check (was 43/49 on 2026-09-30 with 6 missing years)

The 2026-09-30 script (`2026-09-30-goo-291-downloads.md`, "Script used"), pointed at the collaborator's downloads and the same document metadata files, now gives **49/49 PASS**: every `docN` has `year` equal to the year of `metadata.publication_date` (2017, 2020, 2017, 2020, 2025, 2018). The APA References section now carries the year, e.g. `Vaswani, A., … & Polosukhin, I. (2017). Attention Is All You Need. arXiv:1706.03762v7`. The year defect from 09-30 is fixed on the deployed image.

## Eval-project draft `a941ca8e-ef56-4a50-b58e-29324ab03bd3` (project `2f7056ed-…`)

This draft was created in this session (see GOO-292/297). `approver` is an editor of the eval workspace.

| check | evidence | result |
|---|---|---|
| Collaborator Markdown+APA | `D3-01` | 200, 3895 B; reference carries `(2026)` |
| Collaborator LaTeX ZIP | `D3-02` | 200, 2328 B; `references.bib` has `year = "2026"`, `eprint = "2609.04767v1"` |
| Foreign org | `D3-03` | 404 |
| Cross-org workspace editor | `D3-04` | 404 |

## NOT RUN

None of the requested GOO-291 checks.
