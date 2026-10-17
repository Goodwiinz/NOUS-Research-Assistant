# Quick Wins & Roadmap Summary

> **Not a live roadmap (2026-10-17): this is a dated competitive snapshot. Its priorities, effort estimates and status markers were not maintained. Current work is tracked in Linear and in [plans](../plans/README.md); verify any item against the code before treating it as open or done.**

## 🚀 Immediate Quick Wins (This Sprint)

### 1. **Inline Citation Links** — 2-3 days
```
BEFORE: "Studies show X is effective"
AFTER: "Studies show X is effective [1, 2]" with clickable links to source
```
**Why:** Every competitor does this. It's table stakes for research trust.

### 2. **Evidence Agreement Meter** — 1-2 days
Visual indicator: "🟢 8 of 10 sources agree" or "🟡 Mixed evidence (5 agree, 3 disagree)"
**Why:** Consensus.app's killer feature. Easy to implement, high user trust impact.

### 3. **BibTeX/RIS Export Button** — 1 day
Export search results to standard citation formats.
**Why:** Researchers live in Zotero/Mendeley. Friction killer.

### 4. **Retraction Warning Badge** — 2 days
Call Crossref API on document ingest. Show "⚠️ RETRACTED" badge if flagged.
**Why:** Research integrity is trending. Easy win for credibility.

### 5. **Reproducibility Hash** — 1 day
Show: `Query fingerprint: abc123` — hash of query + model version + index version
**Why:** Proves determinism. Unique to your positioning.

---

## 📋 30-Day Roadmap

### Week 1-2: Trust Foundations
- [ ] Inline citations with source linking
- [ ] Evidence agreement visualization
- [ ] Export to BibTeX/RIS
- [ ] Retraction status checking

### Week 3-4: Differentiation
- [ ] Reproducibility hash display
- [ ] "How I found this" expandable reasoning trace
- [ ] Claim-level extraction (prep for factored verification)
- [ ] OpenAlex/Semantic Scholar API integration for 200M+ papers

---

## 🎯 90-Day Strategic Milestones

### Month 1: TABLE STAKES
- All quick wins implemented
- 200M+ paper access via OpenAlex
- Basic systematic review workflow

### Month 2: DIFFERENTIATION  
- Factored verification (claim-by-claim citation)
- Knowledge graph explorer UI (leverage Neo4j)
- Multimodal figure analysis (charts → data)

### Month 3: BLUE OCEAN
- Full research audit trail export
- Contradiction detection across papers
- Domain-specific presets (Medical, Legal)

---

## 💰 Revenue Positioning

| Tier | Price | Key Features |
|------|-------|--------------|
| **Free** | $0 | 10 queries/day, basic search |
| **Researcher** | $15/mo | Unlimited search, exports, citations |
| **Pro** | $45/mo | Systematic review, knowledge graph, multimodal |
| **Enterprise** | Custom | Audit trails, SSO, custom domains, SLA |

**Insight:** Elicit charges $42/mo for Pro. Your multimodal + determinism features justify similar or higher pricing.

---

## 🏆 Competitive Moat Checklist

Your unique combination NO competitor has:

- [x] **Multimodal** — PDF, images, audio, video (competitors are text-only)
- [x] **Knowledge Graph** — Neo4j for relationship discovery
- [x] **Multi-Agent** — CrewAI specialized agents
- [ ] **Deterministic** — Reproducible outputs (implement and market!)
- [ ] **Full Audit Trail** — Query → Search → Rank → Answer provenance

Build these 5 together = defensible position.

---

## 📣 Marketing Messages to Test

1. "The only research AI that gives you the same answer every time"
2. "Citation-backed answers you can trace to the source"
3. "Find connections in your research that others miss"
4. "Research AI for people who can't afford to be wrong"
5. "Multimodal research: analyze papers, figures, and podcasts together"

---

## Next Steps

1. **This week:** Implement inline citations + evidence meter
2. **Review:** Share this analysis with team
3. **Validate:** User interviews on trust/reproducibility needs
4. **Track:** Set up competitor monitoring (Elicit blog, Twitter, Product Hunt)
