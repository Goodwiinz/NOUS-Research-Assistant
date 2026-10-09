# Chat reading experience: footnotes direction

Design decision for the /chat transcript, chosen 2026-09-06 from three canvas directions (Footnotes, Marginalia, Evidence ledger). Canvas: https://claude.ai/code/artifact/55a92e19-ccbc-4c9e-8684-447a8931c777, page "Chat".

## Goal

Make provenance the visible structure of every assistant reply, in the manuscript register the homepage and auth pages now use: serif reading text, superscript numerals, a numbered Sources list under each reply with the quoted passage and where it came from. Reading an answer and checking its sources should be one motion, not a hover.

## What changes

**Assistant reply body.** Keep `.nous-chat-body` (Source Serif 4) but raise the reading size to 17px / 1.7 at `lg` and cap the measure at 720px. Inline citation markers become superscript numerals (`1`, `2`) in Sol-safe, replacing the current pill-shaped `[Doc N]` chips. The numeral is the same `CitationLink` (popover on hover, split panel on click), restyled.

**Sources list.** Under the reply, replace the horizontal `CitationChips` row (title + relevance %) with a numbered list, one row per distinct cited document in citation order:

- numeral (Sol-safe, 11px semibold) · document title (13px medium) · locator (page or section when known) · retrieved-via label (vector / graph / vector + graph, from `retrieval_source` when present, else omitted)
- the quoted `snippet` in serif 14px/1.5, secondary colour, in quotation marks, clamped to two lines
- rows separated by hairlines; the whole block headed by a small "Sources" label

Clicking a row opens the document in the artifact panel at that page (same handler `CitationChips` uses today). Relevance percentages leave the default view; they stay available in the retrieval diagnostics link, which remains at the end of the list.

**Actions row.** Copy / Regenerate / Helpful become quiet text-with-icon buttons in one row under the sources, 12px, always visible (no hover reveal), 44px hit area on touch. The rating control stays where it is functionally; only its chrome changes.

**User turn.** Aurum background, hairline Apollo border, 14px radius with a 4px inside corner, serif 16px. Max width 540px as today.

**Composer.** The "Ultra Thinking" mono chip is the RAG grounding toggle; it becomes a labelled switch "Use my sources" in UI type, same state and same tooltip copy ("Grounds answers in your sources." / "Answers without your sources."). The keyboard-hint footer row is removed; Enter to send stays discoverable via the Send button tooltip. Character counter unchanged.

**Thread list.** Each row's second line shows `N sources · M turns` when the thread has citations, else the existing preview snippet.

**Right rail.** Unchanged in this pass except the "Sources in this thread" idea is deferred (see Out of scope). The rail already got sentence-case headers and honest states in #1605/#1607.

## Out of scope (deliberate)

- Marginalia and the evidence table: rejected directions.
- Rail redesign, welcome state, first-run: separate pass.
- New backend fields. `retrieved-via` renders only when the citation already carries a source label; nothing is invented.
- Dark theme: follows tokens; no dark-specific work.

## Constraints

- Tokens only (DESIGN.md). Serif via `--nous-font-body`, UI via `--nous-font-ui`.
- AA contrast: Sol-safe for numerals on light, Helios on dark (both already tokenised).
- No em dashes in copy. Sentence case.
- Existing tests for `CitationChips`, `AuiMessage`, `ChatInput` and `ChatSidebar` keep passing or are updated for the new copy/markup; test IDs preserved.
- No browser verification in CI; the executor states what was not seen.
