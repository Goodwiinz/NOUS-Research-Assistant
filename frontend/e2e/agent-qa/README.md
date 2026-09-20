# Live agent Q&A evaluation

This Playwright suite sends the versioned cases in
`e2e/fixtures/agent-qa.v1.json` through the deployed chat UI. Each case opens
a new chat, waits for committed assistant prose, and checks a deterministic
concept rubric. Reference answers document the intended meaning; the grader
does not require exact wording except where a case explicitly requests it.

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

`exactAnswer` compares the displayed answer case-sensitively, including
punctuation. Only surrounding whitespace from the browser extraction is
trimmed. General concept checks continue to ignore case and punctuation.

To inspect cases without making model calls, leave `AGENT_QA_LIVE` unset and
run `corepack pnpm@10.18.2 --dir frontend test:e2e:agent-qa --list`.

## Maintain the dataset

Version 1.0.4 extends the citation check to entries in Markdown bullets,
numbered lists, and blockquotes. These formats remain subject to the same
metadata requirements; they must not bypass the unsupported-status check.

Version 1.0.3 accepts citation-integrity paraphrases such as "do not assign"
and checks for unsupported "unpublished" labels in proposed reference entries.
The case-specific `forbiddenPatterns` match the displayed answer with
case-insensitive, multiline regular expressions; `reason` explains each failure.
They distinguish the observed citation formats from prose warning against those
labels, but are not a general semantic citation verifier. Regression cases in
`agentQaCitationIntegrity.test.ts` cover both forms. The backend's shared agent
guidance now explicitly keeps missing bibliographic metadata unknown; deploying
that backend change is required before a live browser run can verify its effect.

Version 1.0.2 corrects false negatives observed in the first live run: it accepts
"Digital Object Identifier", plural citation/paper terms, and method-only
paraphrases, and removes the arbitrary minimum length for the percentage
calculation. Reviewed answers and incorrect counterexamples are covered by
`agentQaParaphrases.test.ts`. Regrading recorded answers checks the revised
rubric; it is not a fresh live run or an independent estimate of agent quality.

- Keep prompts self-contained and independent of current events or tenant data.
- Add explicit wording variants to each required concept group.
- Update the semantic version when case meaning or grading changes.
- Run the focused unit tests; they verify unique IDs and prove every reference
  answer passes its rubric.

## Verify the citation fix after deployment

After the backend revision containing the shared citation guidance is running,
rerun the affected case using the saved session. The backend release process is
documented in [the workflow guide](../../../.github/workflows/README.md).
This command keeps the initial full-run artifacts by using separate output
and report directories:

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
unpublished without evidence. Regrading a saved answer does not verify a
deployed prompt change.
