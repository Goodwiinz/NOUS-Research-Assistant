# Grounded draft workflow

## Behavior

Draft creation and revision select bounded passages by claim and theme terms. Source
page anchors are retained when present, and legacy text remains unanchored. New PDF
extractions store stable `[Page N]` markers.

Every cited document is reviewed before a draft version is inserted or promoted.
`exact` and `minor` verdicts may persist; reviewer errors, `major`, `unverified`, or
skipped cited documents fail the operation. The complete review is stored in
`generation_params.citation_review`, while each citation stores the decisive review
passage in `snippet` and its page or source location in `context`.

Chat draft creation waits for a bounded period and returns the terminal generation
payload. A timeout returns `pending` truthfully. Frontend execution and plan state is
derived from the result payload and is rehydrated from persisted assistant messages.

## Acceptance checks

- Late-page Attention Is All You Need benchmark passages containing 28.4 BLEU,
  41.8 BLEU, and 3.5 days on 8 GPUs are selected inside the evidence budget.
- Unsupported or unverifiable claims block both creation and revision persistence.
- Minor reviews persist with inspectable evidence and location metadata.
- Chat tool results contain `draft_id` on success and a readable terminal failure.
- Pending results remain incomplete across live rendering and thread reload.
- Focused backend and frontend tests run without live model or network calls.
