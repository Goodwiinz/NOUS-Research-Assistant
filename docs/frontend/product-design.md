# NOUS — Product Design Doc

**Team:** NOUS (solo — Abdel / [@goodwiins](https://github.com/goodwiins))
**Track:** AI Software Track (Coding Required)
**Hackathon:** CUNY AI Innovation Challenge
**Submission repo:** [goodwiins/rag](https://github.com/goodwiins/rag)
**Live showcase:** [goodwiins.github.io/nous](https://goodwiins.github.io/nous/)

---

## 1. Brainstorm

Ideas considered before landing on NOUS:

| #   | Idea                                                  | Why rejected / evolved                                          |
| --- | ----------------------------------------------------- | --------------------------------------------------------------- |
| 1   | ChatGPT-style wrapper over course PDFs                | Too commoditized. No moat, no technical story for judges.       |
| 2   | Citation-finder browser extension                     | Narrow use case, weak multimodal story.                         |
| 3   | "Smart folder" that auto-tags research files          | Organizing ≠ reasoning. Doesn't hit the AI-usage criterion.     |
| 4   | **Multimodal RAG with agent + citation graph (NOUS)** | **Chosen.** Hits all six criteria; real user pain; unique.      |
| 5   | Research-to-slides generator                          | Folded in as a future feature; too small to be a whole product. |

**Decision driver:** the AI Software Track rewards _appropriate AI usage_ (20%) and _understanding of AI concepts_ (15%) more than surface novelty. A grounded, hybrid-retrieval RAG with a LangGraph agent and human-in-the-loop shows real AI engineering — not just a prompt wrapper.

---

## 2. Project Description

**NOUS** (Greek: _νοῦς_, "mind") is a multimodal intelligence platform that turns scattered research material — PDFs, images, audio, arXiv papers — into grounded, cite-backed answers and literature review drafts. A LangGraph agent routes user intent across specialized subgraphs (research / writing / data / general), retrieves via a hybrid index (vector + keyword + knowledge graph), and calls destructive tools only after a human-in-the-loop confirmation.

### Alignment with grading criteria

| Criterion                          | Weight | How NOUS delivers                                                                                                                          |
| ---------------------------------- | ------ | ------------------------------------------------------------------------------------------------------------------------------------------ |
| **Real-World Impact & User Value** | 30%    | Solves a live pain for researchers — hours saved on literature reviews, every claim linked to a source. Usable by CUNY students + faculty. |
| **Technical Implementation**       | 20%    | Full-stack (Next.js 15 + FastAPI), 4 data stores (Postgres/Qdrant/Neo4j/Redis), Docker + K8s deployable, CI green, >70% test coverage.     |
| **Use of AI & Algorithmic Design** | 20%    | LangGraph StateGraph · hybrid retrieval + rerank · HITL interrupts · eval-gated quality (DeepEval faithfulness > 90%).                     |
| **Creativity & Innovation**        | 10%    | Citation-graph-aware retrieval · WebLLM for offline inference · agent subgraphs with filtered tool sets.                                   |
| **Understanding of AI Concepts**   | 15%    | Code quality: TS strict, mypy strict, Pydantic v2, eval harness, structured logging, Prometheus + LangSmith.                               |
| **Presentation**                   | 5%     | Dedicated showcase site, hackathon-ready README, brand system, live demo.                                                                  |

---

## 3. Users

NOUS targets knowledge workers whose day is eaten by synthesizing unstructured material.

### Primary personas

1. **CUNY Graduate Student — Priya, PhD candidate in neuroscience**
   - Drowning in 300+ arXiv / PubMed papers across three thesis chapters.
   - Needs: search across her corpus, draft a literature-review section, export BibTeX.
   - Win condition: goes from "I have a folder of PDFs" to "I have a cited draft" in one afternoon.

2. **CUNY Undergraduate Researcher — Marco, CS junior on a REU project**
   - Given 15 papers by his PI, has to brief the lab next Tuesday.
   - Needs: ask questions across the set, get answers with citations he can verify.
   - Win condition: no hallucinated claims in his memo.

3. **Faculty / Research Staff — Dr. Chen, lab PI**
   - Maintains shared group library; wants team members to stop re-reading the same papers.
   - Needs: shared knowledge base, audit trail on who cited what, summarization.
   - Win condition: onboard a new student in 10 minutes.

### Secondary users

- NYC journalists / policy analysts synthesizing public-record PDFs
- R&D teams in small biotech / climate orgs with internal technical corpora

### Not-users (explicitly out of scope for MVP)

- Casual consumer chat users (NOUS isn't ChatGPT)
- Enterprise legal-review workflows (different compliance surface)

---

## 4. MVP Features

What the judges will see in the demo — the critical path, not every feature we've built.

### Priority 0 (must work on demo day)

| Feature                      | What it does                                                             | Where it lives in the repo                                    |
| ---------------------------- | ------------------------------------------------------------------------ | ------------------------------------------------------------- |
| **arXiv search + ingest**    | Search arXiv, pick papers, ingest into the KB with HITL confirm          | `backend/src/api/arxiv/`, `frontend/src/components/research/` |
| **Hybrid grounded Q&A**      | User asks a question → agent retrieves → cites chunks → no hallucination | `backend/src/services/agent/graph.py`, `frontend/app/chat/`   |
| **Literature-review drafts** | Pick a project → agent generates multi-theme draft with inline citations | `backend/src/services/draft_generation.py`                    |
| **Bibliography export**      | BibTeX / APA / IEEE / MLA export from citation graph                     | `backend/src/api/citations/export.py`                         |
| **Streaming + HITL UI**      | SSE token stream + interrupt confirmation modal for destructive actions  | `frontend/src/hooks/useAgentStream.ts`                        |

### Priority 1 (nice-to-have for demo, stable but not on critical path)

- Multimodal image/audio ingest with Whisper transcription
- Cytoscape.js citation-graph visualization
- WebLLM local inference toggle
- Entity extraction + Neo4j KG navigation

### Priority 2 (post-hackathon)

- Shared/team workspaces with RBAC beyond single-tenant
- Mobile-responsive layouts for long drafting sessions
- Real-time collaborative editing on drafts
- LaTeX-to-Overleaf push integration

---

## 5. Role Assignment

Solo team — all roles = Abdel ([@goodwiins](https://github.com/goodwiins)).

| Role             | Owner | Notes                                      |
| ---------------- | ----- | ------------------------------------------ |
| Product / design | Abdel | Brand system, UX flows, showcase site      |
| Backend / AI     | Abdel | FastAPI, LangGraph agent, retrieval, evals |
| Frontend         | Abdel | Next.js 15, shadcn/ui, streaming UI        |
| Infra / DevOps   | Abdel | Docker Compose, CI, K8s manifests          |
| Demo / pitch     | Abdel | Video recording, pitch deck, presentation  |

---

## 6. Timeline

Counting down to submission. `T` = submission deadline.

| Window       | Milestone                                                        | Status              |
| ------------ | ---------------------------------------------------------------- | ------------------- |
| `T -4 weeks` | Core agent graph + hybrid retrieval working end-to-end           | Done                |
| `T -3 weeks` | Multimodal ingest (PDF, image, audio) + citation pipeline        | Done                |
| `T -2 weeks` | Literature-review draft generation + LaTeX/BibTeX export         | Done                |
| `T -1 week`  | DeepEval thresholds wired into CI, Prometheus + LangSmith traces | Done                |
| `T -5 days`  | Showcase site published (`goodwiins.github.io/nous`)             | Done                |
| `T -3 days`  | Hackathon-ready README merged                                    | In review (PR #387) |
| `T -2 days`  | Demo video recorded + uploaded                                   | Pending             |
| `T -1 day`   | Pitch deck finalized + dry run                                   | Pending             |
| `T -0`       | Submit — GitHub link + website + video + deck                    | Pending             |

### Blockers / dependencies

- Demo video blocks pitch deck (screenshots pulled from the demo)
- README PR #387 must merge before the showcase site is linked publicly in the submission form
- Pitch deck signoff needs README merged (deliverables table is the source of truth)

---

## 7. Project Tracker

| Feature / Deliverable       | Owner | Status      | Notes                                                         |
| --------------------------- | ----- | ----------- | ------------------------------------------------------------- |
| LangGraph agent system      | Abdel | Done        | 12 tools · 4 subgraphs · HITL on destructive ops              |
| Hybrid retrieval + rerank   | Abdel | Done        | Qdrant + Postgres FTS + Neo4j fused · Cohere rerank           |
| Multimodal ingest pipeline  | Abdel | Done        | PDF/OCR, Whisper, image detection, video frames               |
| Citation extraction + graph | Abdel | Done        | arXiv + Semantic Scholar + CrossRef + PDF parser              |
| Literature-review drafts    | Abdel | Done        | Multi-theme synthesis · inline citations · LaTeX export       |
| Bibliography export         | Abdel | Done        | BibTeX / APA / IEEE / MLA with warnings                       |
| DeepEval in CI              | Abdel | Done        | Faithfulness >90%, relevancy >70%, hallucination <10%         |
| Prometheus + LangSmith      | Abdel | Done        | Traces + metrics wired                                        |
| K8s + Helm deployment       | Abdel | Done        | Terraform for AWS resources                                   |
| **Showcase website**        | Abdel | Done        | `goodwiins.github.io/nous`                                    |
| **GitHub repo**             | Abdel | Done        | Public · MIT · README overhaul in PR #387                     |
| **Hackathon README**        | Abdel | In review   | PR #387 — drafted, CI green, awaiting merge to `develop`      |
| **Demo video**              | Abdel | Not started | Screen recording of full arXiv → draft flow                   |
| **Pitch deck (PPT)**        | Abdel | Not started | `brand/NOUS-Product-Overview.docx` exists — needs PPT version |

---

## 8. Risks & Mitigations

| Risk                                     | Mitigation                                                               |
| ---------------------------------------- | ------------------------------------------------------------------------ |
| Live demo fails (API keys, rate limits)  | Pre-recorded video as backup · seeded demo user with pre-ingested papers |
| LangGraph HITL interrupt confuses judges | Cover it explicitly in the video narration — it's a feature, not a bug   |
| Agent hallucinates during live demo      | DeepEval thresholds + "no citation → no claim" UI guard                  |
| Scope creep before submission            | All P2 features frozen; only demo-critical bugs get fixed                |
| Missing deliverable penalty (10% each)   | Deliverables table in README + this doc as signoff checklist             |

---

## 9. Signoff

- [ ] Organizers (`cuny-ai-innovation-challenge-organizers@googlegroups.com`) granted edit access on the Google Doc copy of this plan
- [ ] Plan reviewed and approved by Friday 5pm
- [ ] All four deliverables (website, GitHub, video, deck) linked and reachable

---

_Last updated: 2026-04-24_
