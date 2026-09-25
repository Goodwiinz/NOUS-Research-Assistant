# Live agent Q&A evaluation

This Playwright suite sends thirty short research-note questions from
`e2e/fixtures/agent-qa.v1.json` through the deployed NOUS chat UI. Each case
opens a new chat, waits for an answer, and checks a small concept rubric.
Every answer should use only the note included in its question. The checks
assess chat content; they do not create or inspect saved research notes.

| Question             | Expected answer                                                     |
| -------------------- | ------------------------------------------------------------------- |
| Paper summary        | A search tool helped 20 students find papers faster.                |
| Main finding         | Semantic search found more relevant papers.                         |
| Study method         | Researchers interviewed 12 librarians.                              |
| Study limitation     | Five students or one university limits the study.                   |
| Supporting source    | Note A supports reduced reading time.                               |
| Compare paper notes  | Paper A saved time; Paper B did not.                                |
| Missing results      | No better method can be identified without results.                 |
| Incomplete citation  | Mark it incomplete, verify the source, and avoid invented metadata. |
| Draft research note  | State the relevance finding and small sample limitation in chat.    |
| Next research step   | Test the tool with a larger group.                                  |
| Paper author         | Maya Chen.                                                          |
| Paper year           | 2022.                                                               |
| Study sample         | 18 students.                                                        |
| Study duration       | Six weeks.                                                          |
| Search source        | Paper Search.                                                       |
| Citation source      | Citation Checks.                                                    |
| Study location       | The university is not stated.                                       |
| Untested group       | Test the tool with students.                                        |
| Comparison group     | Compare results with the old tool.                                  |
| Search keywords      | Librarians; summaries.                                              |
| Narrow search        | Add librarians.                                                     |
| Citation title/year  | Useful Summaries (2020).                                            |
| Citation author/year | Alex Rivera, 2024.                                                  |
| Supporting studies   | Two papers.                                                         |
| Total participants   | 25 students.                                                        |
| Reading time         | Summaries saved 10 minutes.                                         |
| Note section         | Limitation.                                                         |
| Reading status       | To read.                                                            |
| Project description  | Compare search tools used by librarians.                            |
| Follow-up task       | Read the full paper.                                                |

## Run

Use either a saved Playwright session or an email/password pair. For a saved
session on a server or SSH terminal, run the login helper headlessly from the
repository root:

```sh
NOUS_BASE_URL=https://goodwiinz.tech \
corepack pnpm@10.18.2 --dir tools/nous-playwright auth --headless

AGENT_QA_LIVE=1 \
AGENT_QA_BASE_URL=https://goodwiinz.tech \
AGENT_QA_AUTH_STATE=../tools/nous-playwright/.auth/nous.json \
AGENT_QA_RUN_LABEL=example-full-v128 \
corepack pnpm@10.18.2 --dir frontend test:e2e:agent-qa
```

The helper prompts for your email and password; password input is hidden.
It automatically chooses headless mode on Linux without `DISPLAY`, so the
flag is optional there. On a computer with a display, use `auth --headed`
for manual browser sign-in (including SSO or MFA), then confirm in the terminal.
State paths are resolved relative to `frontend/`; absolute paths also work. The
existing `NOUS_BASE_URL` and `NOUS_AUTH_STATE` variables are accepted too.
Save a fresh session with the helper when authentication expires.

For unattended runs, set credentials through your runtime's secret environment:

```sh
AGENT_QA_LIVE=1 \
AGENT_QA_BASE_URL=https://example.test \
AGENT_QA_EMAIL=user@example.test \
AGENT_QA_PASSWORD=secret \
AGENT_QA_RUN_LABEL=example-run-1 \
corepack pnpm@10.18.2 --dir frontend test:e2e:agent-qa
```

Without `AGENT_QA_LIVE=1`, the suite skips. With it enabled, a missing target,
missing credentials, or unreadable state file fails immediately with a setup
error. Existing `SMOKE_BASE_URL`, `SMOKE_USER_EMAIL`, and
`SMOKE_USER_PASSWORD` settings also work. Credentials are never mixed between
the Q&A and smoke namespaces. Live runs also require a new
`AGENT_QA_RUN_LABEL` consisting of lowercase letters, digits, and single
hyphens. Reusing a label with existing artifacts fails before the run begins.

The password login runs once outside recorded test contexts. Each case then
gets a fresh browser context and submits one turn to the live agent; the agent
may make several model or tool calls for that turn. The resulting chat threads
remain in the test account.

Each case attaches a phase: `not-sent` if the question was not submitted,
`sent` if submission occurred but a committed answer was not validated,
`rendered-empty` if a committed answer contains no text, or `graded` with a
separate pass/fail result. Graded turns attach the prompt, reference answer,
actual answer, rubric result, elapsed time, and thread URL. Browser failures
retain a trace and screenshot. The rubric checks
explicit concepts and forbidden phrases. It is repeatable and easy to audit,
but it cannot detect every factual contradiction or substitute for human or
model-based semantic review.

Concept checks ignore case and punctuation. They catch straightforward omissions and wrong answers, but cannot establish full factual correctness. The separate `tools/nous-playwright` workflow tests project creation, attachments, approval, and persistence in the actual NOUS workflow; this chat suite does not test those actions.

To inspect cases without making model calls, leave `AGENT_QA_LIVE` unset and
run `corepack pnpm@10.18.2 --dir frontend test:e2e:agent-qa --list`.
To verify report creation through a real Playwright worker without signing in
or sending a question, run the offline report probe with a fresh label:

```sh
AGENT_QA_RUN_LABEL=offline-report-probe-1 \
corepack pnpm@10.18.2 --dir frontend exec playwright test \
  --config=playwright.agent-qa-report-probe.config.ts
```

## Maintain the dataset

Version 1.1.0 replaces the general research and instruction questions with
self-contained research-note questions. Results are not directly comparable to
1.0.x. The original examples in `agentQaParaphrases.test.ts` are authored
offline checks. Captured live observations are labeled separately. The
citation-integrity regression tests still cover unsupported publication status
in a proposed reference.

Version 1.2.0 adds twenty supplied-note cases without changing the first ten.
Aggregate scores are not directly comparable with 1.1.0; compare the shared
ten cases separately. The paper records and notes are synthetic. Organizational
prompts ask for draft text in chat, not actual saves.

Version 1.2.1 clarifies three prompts and adds wording alternatives to their
existing concept checks. All thirty cases remain in place, and the other
twenty-seven cases are unchanged. Complete answers captured in the 2026-09-20
version 1.2.0 run and the first version 1.2.1 three-case rerun are separate
regression examples; the other paraphrase examples are authored offline. The
historical 26/30 score has not been recomputed or rewritten. These lexical
checks still have limits: passing them does not prove that every statement in
an answer is supported by the note.

Version 1.2.2 keeps the same thirty prompts and accepts the equivalent wording
observed in the 2026-09-25 live run. The captured project description used a
valid form of "compare" but also introduced evaluation topics absent from the
brief; that case now flags those observed additions. Phrase checks cannot
exhaustively detect invented project scope. They can also flag a harmless
negated caveat such as "the brief does not specify features or usability."
Treat a project-scope phrase failure as a request for human review of the
complete answer, not proof that the answer invented those topics. A passing
phrase check is likewise not proof of grounding. Review the full answer.
The original 25/30 raw result remains the record of that run; the new rubric
needs its own live result before comparing scores.

Version 1.2.3 accepts the observed "bigger and more diverse participant group"
wording. It also treats a visible restored thread in the context rail as ready
to leave, even when the app has not reflected that selection in the URL. The
2026-09-25 v1.2.2 production run scored 19/30: nine cases were not sent because
the earlier URL-based readiness check timed out, one valid larger-group answer
missed the rubric, and the project description added topics absent from its
brief. Those results remain separate from this revision's rerun.

Version 1.2.4 accepts two more observed wording variants: "does not include
their results" and plural "old tools." The v1.2.3 production run scored 25/30:
two complete answers missed those phrases, one New chat navigation returned to
the previous thread before submission, one submitted turn did not commit an
answer within four minutes, and the project description added evaluation topics
absent from the brief. An isolated retry of the stalled turn also failed while
the test machine was out of disk space; after reclaiming space, it passed in
ten seconds. That supports an environmental explanation for the stall but does
not prove the earlier attempt's cause. Keep each run's raw score and phases
separate; regrading old answers with a new rubric is not a new live run.

Version 1.2.5 adds three complete, grounded answers from the v1.2.4 full live
run as regressions. The phrases "does not include the results," "sample size
was small," and "old one" now match their respective concepts. That run
scored 26/30: all thirty questions were sent and committed; three valid
answers missed phrase alternatives, and the project description again added
evaluation topics absent from the brief. Its raw result remains 26/30.

Version 1.2.6 adds two valid answer variants from the v1.2.5 full live run:
"does not report the results" and "larger-scale evaluation." That run scored
27/30 with all thirty questions sent and committed. These two answers missed
the phrase rubric; the project description again added unsupported criteria.
The raw v1.2.5 score remains 27/30. The local chat-route fix still requires
verification after deployment; live runs against the merged production build
do not exercise it.

Version 1.2.7 adds a bounded pattern for the larger-follow-up-study concept.
The v1.2.6 two-case production rerun passed `missing-results` but missed
"larger, well-powered follow-up study" because modifiers split the exact
phrase. A targeted v1.2.7 live rerun of `next-research-step` passed, but review
found that its arbitrary-word gap could also accept a larger database with the
same study group. Version 1.2.8 limits the intervening words to relevant study
modifiers and adds those false positives as regression tests. It remains a
lexical check, so a full-answer grounding review is still needed. A targeted
v1.2.8 live rerun of `next-research-step` passed.

- Keep prompts short, self-contained, and independent of current events or tenant data.
- Accept normal wording variants without requiring arbitrary answer length.
- Update the semantic version when a case or rubric changes.
- Run the focused unit tests and inspect actual live answers alongside rubric results.

## Rerun the citation case

Run against a build that contains these chat and rubric changes. Use the saved
session to rerun the citation case, or select the five 2026-09-25 failures with
`--grep 'incomplete-citation|next-research-step|draft-project-description|extract-paper-year|citation-author-year'`.
Each run label writes JSON to `frontend/test-results/agent-qa-runs/<label>/results.json`,
the HTML report to `frontend/playwright-agent-qa-report/runs/<label>/`, and
traces/screenshots under the corresponding test-results directory. These
locations preserve the original v1.2.1 result.

```sh
AGENT_QA_LIVE=1 \
AGENT_QA_BASE_URL=https://goodwiinz.tech \
AGENT_QA_AUTH_STATE=../tools/nous-playwright/.auth/nous.json \
AGENT_QA_RUN_LABEL=example-citation-v128 \
corepack pnpm@10.18.2 --dir frontend test:e2e:agent-qa \
  --grep incomplete-citation
```

Inspect the actual answer as well as the rubric result. A valid answer must
leave missing publication details unknown and must not label the source as
unpublished without evidence. Regrading a saved answer does not verify live behavior.
