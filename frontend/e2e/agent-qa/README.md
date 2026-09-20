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
corepack pnpm@10.18.2 --dir frontend test:e2e:agent-qa
```

Without `AGENT_QA_LIVE=1`, the suite skips. With it enabled, a missing target,
missing credentials, or unreadable state file fails immediately with a setup
error. Existing `SMOKE_BASE_URL`, `SMOKE_USER_EMAIL`, and
`SMOKE_USER_PASSWORD` settings also work. Credentials are never mixed between
the Q&A and smoke namespaces.

The password login runs once outside recorded test contexts. Each case then
gets a fresh browser context and submits one turn to the live agent; the agent
may make several model or tool calls for that turn. The resulting chat threads
remain in the test account.

Each completed turn attaches the prompt, reference answer, actual answer,
rubric result, elapsed time, and thread URL to the Playwright report. Browser
failures retain a trace and screenshot. The rubric checks
explicit concepts and forbidden phrases. It is repeatable and easy to audit,
but it cannot detect every factual contradiction or substitute for human or
model-based semantic review.

Concept checks ignore case and punctuation. They catch straightforward omissions and wrong answers, but cannot establish full factual correctness. The separate `tools/nous-playwright` workflow tests project creation, attachments, approval, and persistence in the actual NOUS workflow; this chat suite does not test those actions.

To inspect cases without making model calls, leave `AGENT_QA_LIVE` unset and
run `corepack pnpm@10.18.2 --dir frontend test:e2e:agent-qa --list`.

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

- Keep prompts short, self-contained, and independent of current events or tenant data.
- Accept normal wording variants without requiring arbitrary answer length.
- Update the semantic version when a case or rubric changes.
- Run the focused unit tests and inspect actual live answers alongside rubric results.

## Rerun the citation case

Use the saved session to rerun the citation case against the target deployment.
This command keeps prior full-run artifacts by using separate output and report
directories:

```sh
AGENT_QA_LIVE=1 \
AGENT_QA_BASE_URL=https://goodwiinz.tech \
AGENT_QA_AUTH_STATE=../tools/nous-playwright/.auth/nous.json \
PLAYWRIGHT_HTML_OUTPUT_DIR=playwright-agent-qa-report/citation-fix \
PLAYWRIGHT_HTML_OPEN=never \
corepack pnpm@10.18.2 --dir frontend test:e2e:agent-qa \
  --grep incomplete-citation \
  --output=test-results/agent-qa-citation \
  --reporter=list,html
```

Inspect the actual answer as well as the rubric result. A valid answer must
leave missing publication details unknown and must not label the source as
unpublished without evidence. Regrading a saved answer does not verify live behavior.
