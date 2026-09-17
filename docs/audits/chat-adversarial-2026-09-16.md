# Chat adversarial browser audit — 2026-09-16

**Final status (2026-09-17):** Local repairs are complete and independently
reviewed on `codex/chat-audit-20260916` (product head `236867d21`). Luna max
implemented the fixes. This report tracks 15 original audit findings and two
additional authorization findings from integration review. All seven repair
plan tasks are complete. **The live website has not been redeployed.**

Live target: https://goodwiinz.tech/chat. Tested with agent-browser 0.27.0 and Playwright attached to its Chromium session, using the account authorized in the conversation. Credentials and browser authentication state are not included in this report. Original audit source baseline: `177c63e65` (`origin/develop` when work began). The resumed repair branch incorporates upstream `7ddc961f0`, which changes tablet layout and labels. The serving backend revision was not established; repository fixes are not deployment verification.

## Confirmed defects

| ID | Severity | Reproduction and observed result | Cause / repair |
|---|---|---|---|
| A1 | High | Send a long response, click **Stop agent** after its first text, then reload. The UI first showed **Stopped** and about five partial lines; reload resumed the producer and displayed sixty completed lines without **Stopped**. | Normal Stop only aborts the browser connection. The cancel endpoint only supports parked confirmations. Add an owned, idempotent durable cancellation command and producer acknowledgement. |
| A2 | High | Upload the 118-byte synthetic `qa-source-20260916.txt` using **Attach file**. HTTP 400: `Failed to upload file: 'NoneType' object has no attribute 'upload_fileobj'`. | `FileService.upload_file` directly uses uninitialized `_s3_helper` / `_storage_helper`, bypassing their lazy properties. Initialize through the existing properties; preserve cleanup and safe public errors. |
| A3 | High | Hold an upload response, type a prompt, click Send. The chip still says uploading, but an agent request is sent without attachment IDs and the chip disappears. A failed real upload also allowed Send. | Shared composer submission guard ignores uploading/error chips. Block Send, Enter, and Queue until retained attachments are usable. |
| A4 | High | Export as PDF. The downloaded `.pdf` is 14,033 bytes beginning `<!DOCTYPE html>`; a PDF parser rejects it. | Backend silently falls back to HTML when WeasyPrint is absent; frontend overrides the server filename with `.pdf`. Supply a verified renderer and fail explicitly if unavailable. |
| A5 | Medium | At 768px width, the two fixed side panels leave a 172px chat column. Source switch and counter overlap; Commands is covered by the right panel. | Desktop conversation and context columns both start at `md`. Align the conversation column and drawer/trigger at a wider breakpoint. |
| A6 | Medium | At 390×520, open `/`. The listbox is 605.5px tall and starts at y≈−291; the first six commands are outside the usable viewport, with no scroll cap. | Bound the menu by available viewport space and scroll keyboard selection into view. |
| A7 | Medium | Edit the last exchange in a three-exchange thread. Four user and four assistant rows appear for about 11 seconds; final reconciliation returns to three each. | Replacement optimistic display retains the exchange being replaced. Hide the replaced suffix while its replacement runs, with rollback on failure. |
| A8 | Medium | Type an unsent draft in B, select A, then return to B. The draft is visible in both conversations. | Composer text has one unscoped owner. Preserve drafts by thread/new-chat context and restore the correct draft on selection. |
| A9 | Medium | Load older threads (50→100), note a unique older title, reload, then search for it. No matching threads and no older-page button. Padded queries also miss loaded titles. | Search only covers loaded rows and hides pagination. Keep older pages reachable during search, explain coverage, trim query whitespace. |
| A10 | Medium | Intercept this browser's stream POST with HTTP 503 and a synthetic internal path. The raw server detail appears in the chat. | Render stable category-specific errors instead of arbitrary transport/backend text. Real upload A2 independently exposed an internal exception in its HTTP response. |
| A11 | Medium | JSON and Markdown export report zero token counts although the same persisted answers show provider input/output usage. | Export reads legacy `token_count`, ignoring persisted `token_usage`. Export explicitly labeled measured usage; preserve unknown/legacy semantics. |
| A13 | Medium | Open a source artifact, open the command palette with Ctrl+K, press Escape while its input has focus. Both the palette and underlying artifact close. | The artifact listener checks the live DOM after Radix has already removed the overlay; respect consumed events and their original propagation path. |
| A14 | High | Open a nonexistent thread deep link after using thread A. Two thread-detail requests return 404, but the URL stays on the missing ID while A appears. Pressing Send targets A's thread ID; the audit intercepted this request before it could write. | URL and selected-thread ownership diverge on unavailable initial targets. Verify initialization/navigation guards and keep the URL, visible transcript and submit target consistent. |
| A15 | Medium | User reports reasoning visible in CLI but absent in web chat. Source comparison confirms the CLI renders `plan.reasoning`; web stores it in a closure and omits it from live `ChatInlinePlan`. Provider summaries exist only in streaming state and are discarded at completion. | Connect planner rationale to the live UI, use a discoverable reasoning control, and preserve bounded provider summaries through canonical persistence/hydration. Confirmed again with a browser-local SSE fixture: plan visible / rationale absent during streaming; provider summary visible during streaming then absent after completion while the final reply remained visible. |
| A12 | Low | Axe flags `aria-expanded` on the native textarea and shortcut text contrast of 4.32:1; the rename textbox has no accessible name. | Correct the ARIA contract, strengthen shortcut contrast, label rename input. |

## Executed checks

| Area | Observed result |
|---|---|
| Login and return to requested chat | Passed. |
| Approval reload and Deny | Pending create-project confirmation survived reload. Deny produced a cancelled result, which remained cancelled after another reload. |
| Rapid repeated approval | Two immediate DOM clicks on Approve produced one confirmation POST and one matching disposable project. Completed result survived reload. |
| Project binding | Attached test thread C to the disposable project; correct project link and binding survived reload. |
| New chat and existing history | Passed; initial setup can be slow. |
| Empty/whitespace submission | Send disabled. |
| Enter, repeated Enter, Shift+Enter | Enter sends, three rapid Enters produced one stream POST and one exchange, Shift+Enter inserts a newline. |
| Input boundaries | 4,000 characters enabled Send; 4,001 disabled it. |
| Markdown and Unicode | Headings, table, Python highlighting, CJK and emoji rendered. Literal script text remained inert. |
| Inline math | Model emitted `\(x^2\)`, displayed as literal `(x^2)`. Dollar-delimited math has not yet been compared. |
| Copy response | Clipboard preserved Markdown, code and Unicode. |
| Feedback | Thumbs-up persisted after its async request. |
| Slash commands | `/help`, `/clear`, `/papers`, paper selection and Escape worked. `/clear` retained persisted transcript rows. Short viewport menu failed A6. |
| Normal subsequent streaming | Continuous DOM observation retained two user/two assistant rows; earlier normal-send duplication did not reproduce. |
| Queue | Adding and removing a queued follow-up worked; removed prompt was not sent. |
| Export while streaming | Disabled as expected. |
| Early Stop and reload | Failed A1. A separate late Stop attempt was inconclusive because output was already nearly complete. |
| Edit/cancel/regenerate | Cancel retained original text. Edit and regenerate persisted correctly after reload; edit displayed transient duplicate exchange A7. |
| Offline send → reconnect → Retry | Clear failure and one successful retry; no duplicate persisted exchange. |
| HTTP 503 → Retry | Recovered after removing browser interception; raw-detail disclosure A10. |
| Malformed SSE / missing terminal frame | Displayed failure rather than claiming completion. |
| Pending and failed attachments | Failed A3; real cloud upload failed A2. |
| Markdown/JSON download | Files downloaded and JSON parsed; all eight expected messages present. Usage metadata failed A11. |
| PDF download | Failed A4. |
| Reload and deep-link selection | Main audit thread URL and persisted transcript retained. |
| Draft isolation | Failed A8. |
| Background turn → New chat → return | New chat remained empty throughout ten samples over about 18 seconds; returning showed the completed turn once. |
| Missing thread deep link | Failed A14; a controlled intercepted submission confirmed the wrong target without writing. |
| CLI/web reasoning parity | Failed A15. With a controlled response in the live browser: one live plan and zero matching rationale nodes; provider summary visible while streaming; after completion, expanding the plan revealed rationale but provider summary was absent while the reply remained. |
| History pagination/search | Loading 50→100 rows worked; searching older unloaded title failed A9. |
| Desktop/phone layout | 1440px and 320/375/390px Send/header pointer hit tests passed. |
| Tablet/short viewport layout | Failed A5/A6. |
| Accessibility smoke | Twenty axe rules passed on the sampled desktop surface; A12 remains. This is not a WCAG certification. |
| Source → document navigation | Source panel and Working folders both opened the correct `1706.03762v7.pdf` viewer. |
| Nested overlay Escape | Failed A13 twice, including with explicit focus in the palette combobox. |
| Named indexed source retrieval | Returned **Attention Is All You Need**, with that exact paper in the clickable source list. Two passages/one document are not treated as a count defect. |
| Missing attachment research attempt | The failed-upload prompt ran anyway and returned a research-model timeout after about 57 seconds. One observation; provider cause not established. |

## Timing observations

Single observations, with browser instrumentation overhead, not percentiles: first short response showed a cursor at about 14.1 seconds after Enter and settled at about 18.2 seconds; a warm formatting response showed text at about 4.8 seconds and settled at 9.3 seconds. Another new thread required about 28.3 seconds for text and 33.5 seconds to settle, including approximately 22 seconds of thread setup. The named-source reply showed a server first-word badge of 18.5 seconds. These badges exclude some browser setup/transport/reconciliation time.

## Test data and evidence

Clearly marked QA conversations, synthetic upload attempts, and one empty [QA-20260916-DISPOSABLE project](https://goodwiinz.tech/projects/5c773a78-8912-4000-b0e0-29fc050f3566) were created. Existing project/document contents were not edited. Test threads:

- [A: streaming and cancellation](https://goodwiinz.tech/chat?thread=3b14f4fd-ba98-494f-a703-1367c20c91f6)
- [B: recovery and replacement](https://goodwiinz.tech/chat?thread=3e4f95ad-05fa-4099-bbe1-ae0b8f3983c7)
- [C: attachments and sources](https://goodwiinz.tech/chat?thread=188b920a-864c-48aa-a9d5-5044f10374d2)

Private local evidence: `/tmp/chat-audit-20260916/` (screenshots, redacted request metadata, exports of synthetic conversations, DOM count timelines). Fault injections affected only this browser's intercepted requests, not shared server configuration. Evidence includes `early-stop-reload.json`, `real-upload.json`, `upload-pending-send.json`, `export.pdf`, `responsive-a11y.json`, `mobile-commands.json`, `edit-regenerate.json`, `older-search-confirmed.json`, `503-retry.json`, `project-approval-deny.json`, `project-approve-double.json`, `project-attach.json`, `background-navigation.json`, and `invalid-route-send.json`.

Untested limits at this stage: physical phones/keyboards, Safari/Firefox, real microphone capture, load/capacity testing, multi-user authorization boundaries, >75-message virtualization, and destructive operations on preexisting data. The live audit was closed at the user's request to finish; local repair verification is recorded below.

## Local repair verification

Task 1 complete: lazy S3/Supabase helper repair and safe upload errors committed as `ac19783bd`, with typing follow-up `e46ca0371`. The 29 focused upload/compensation/guard tests and 12 existing route tests passed. Required mypy passed for both new test modules; the 29-test suite was rerun cleanly with the timeout plugin. Ruff, Black, isort and diff checks passed. Independent review is approved. Live cloud storage has not been retested against these local commits.

Local browser fixture at `/visual-test/chat-mobile-controls` loaded with meaningful controls, no framework error overlay, and no reported browser runtime errors. This baseline fixture checks production control components; it does not reproduce the complete live column layout yet.

Upstream update: PR #1656 (`7ddc961f0`) landed between the original audit and the resumed repair work. It supplies the A5 layout repair with history docking at 1280px, and improves some text sizing/contrast and the composer accessible name. It does not address the remaining cancellation, attachment, export, search, nested-Escape or reasoning findings. Final verification must use this updated layout; the original measurements above are retained as reproduction evidence. Storage commits rebased without conflict to `ac19783bd` and `e46ca0371`.

Reasoning follow-up evidence: `reasoning-mismatch.json` and `25-reasoning-streaming.png`. The browser-local stream supplied known public fixture text and performed no server message write. The fetch override was removed and the page reloaded in cleanup. Both dedicated audit browser sessions were closed after the targeted reproduction.

### 2026-09-17 amendment: durable Stop repair

A1 is repaired locally in `533fdeabf`, `9e0204dc4`, and `3c6e2f500`. Stop
claims the exact owned run, survives reconnect and confirmation continuation,
and reaches Stopped only after producer acknowledgement. Repeated commands,
late acknowledgements, completion/park races, and cancellation cleanup
failures have regression coverage. Acknowledged runs cannot revive stale
approvals on resume. Independent review approved the final follow-up.

Focused verification: 115 backend tests before the final wrapper-only fix;
the final cancellation file passed 11 tests. PostgreSQL tests persisted and
linked stopped partial rows (3 passed); durable-ledger tests assert one
terminal cancellation event. Frontend ownership/service tests: 38 passed,
resume/submit compatibility: 15 passed; the final ownership rerun: 9 passed.
Seven guard mutations failed as intended and passed after restoration;
reproducible commands are in [chat mutation checks](../testing/chat-mutation-checks.md).
Full frontend suite: 2,063 passed across 295 files. This is local verification,
not a claim that the public deployment has changed.

Additional branch checks at `3c6e2f500`: thread services/routes passed 188
tests against isolated PostgreSQL. API/architecture checks passed 764 tests
and initially failed one Alembic subprocess check because the private test
environment lacked Alembic; installing the repository-pinned package made
that check pass. Changed-file quality, generated contracts and frontend
type-check passed. The frontend comparator initially caught a prefer-const
error, fixed in the final Stop commit; both frontend comparators then passed.
Existing full-tree lint debt and the Pydantic schema warning remain reported.

Reasoning migration checkpoint (work in progress): the new nullable
`reasoning_summary` column upgraded, downgraded and upgraded again on an
isolated database. The fixture was provisioned from models and stamped at
the prior migration head; this verifies the new migration delta, not a
replay of the full historical migration chain. Reasoning behavior remains
under implementation and review at this checkpoint.

### 2026-09-17 amendment: reasoning and public error repair

A10 and A15 are repaired locally in `ed4ebe7e2`, with test follow-ups
`35b749785` and `9927b2138`. Live planner rationale now reaches the web
reasoning panel, including rationale with no plan steps. Provider-authored
public summaries survive normal completion, Stop, confirmation continuation
and history hydration through a separate nullable `reasoning_summary` field
bounded to 8,000 characters. Only typed public summary text is extracted;
raw provider reasoning is not exposed. Thread ownership prevents summaries
from appearing in another displayed conversation.

Stream failures render stable category-specific messages and recovery
actions. Arbitrary backend detail stays out of visible and accessible error
content. The focused frontend matrix passed 130 tests in nine files and the
backend matrix passed 29 tests in six files. Additional terminal regressions
passed 19 frontend tests and one backend test. Fixture cleanup passed seven
backend and nine frontend tests. Independent spec and quality review approved
the final change. The new summary ownership assertion killed a disabled-guard
mutation and passed after exact restoration.

TypeScript, changed Python lint/format, required new-migration typing, OpenAPI
and generated API type checks passed. The migration delta passed upgrade,
downgrade and upgrade on isolated PostgreSQL, as qualified above. Existing
Pydantic warnings and expected adversarial fixture diagnostics remain. These
commits have not been deployed to goodwiinz.tech.

### 2026-09-17 amendment: attachment and thread-state repair

A3, A7, A8 and A14 are repaired locally in `1c9b5cd0b`. Send, Enter and Queue
share a guard that retains pending or failed attachments and the draft until
the user finishes or removes those attachments. Drafts belong to their account,
workspace and thread; late preflight cancellation cannot overwrite another
chat, including a different thread in the same account. A successful first
send leaves a subsequent New chat blank.

The persistence initializer now defers to explicit thread URLs. Existing
unavailable-link recovery and deliberate-navigation supersession remain in
control. Editing projects the replacement over the old canonical suffix
without modifying cached server rows; failure restores that suffix and a
successful canonical refresh reconciles one replacement exchange. Tests
observe the displayed rows throughout those transitions.

Independent spec and quality review approved the patch. The focused suite
passed 123 tests under Node 24.21.0/pnpm 10.18.2; TypeScript, changed-file
ESLint comparator, exclusion checks, Prettier and diff checks passed. The
full frontend suite passed 2,079 tests across 295 files on this frozen commit
in 78.23 seconds. Guard mutations failed as intended and passed after exact
restoration; see [Task 3 mutation evidence](../testing/chat-adversarial-task3-mutations.md).
An earlier Node 22 run was superseded by the required Node 24 run. Existing
adversarial fixture diagnostics remain visible in test output. No deployment
or browser validation of these local hook changes is claimed here.

### 2026-09-17 broader backend verification

With the cancellation and reasoning repairs present at `1c9b5cd0b`, the
architecture and API suite passed **769 tests** in 23.77 seconds. It used
private PostgreSQL/Redis endpoints and the isolated audit Python environment.
The 29 warnings include existing synchronous tests carrying an asyncio mark;
they do not represent failed assertions. This run predates the export repair,
which requires its own updated validation. Log: `post-task5-api-architecture.log`
in the private evidence directory.

The thread service and route matrix then passed **188 tests** in 96.66 seconds,
using the same isolated services. One existing Pydantic `schema_extra` warning
remains. Log: `post-task5-thread-matrix.log`.

### 2026-09-17 amendment: responsive and accessibility repairs

A6, A9, A12 and A13 are repaired in `1e89b9eb7`, with fixture and coverage
follow-up `cf9e9c820`. The upstream A5 history breakpoint at 1280px is
preserved and verified in the full chat composition. Search trims its query
and keeps older pages reachable; a regression loads a unique page-two match
without clearing the query. The rename input is labeled, the textarea uses
valid listbox autocomplete attributes, and Escape consumed by a dialog leaves
the source artifact open even after its portal disappears.

A further local check found selected slash-command contrast of 4.27:1 for the
label and 1.05:1 for its description. Both are repaired; the selected-menu axe
check now has zero violations and zero incomplete results. The menu is capped
by its actual anchor position and scrolls keyboard selection into view.

Verification:

- 59 focused component tests across five files, TypeScript, Prettier and the
  frontend quality comparator passed. Seven behavior mutations were caught
  and restored; see [Task 4 mutation evidence](../testing/chat-adversarial-task4-mutations.md).
- All ten tracked Playwright tests passed in one minute, covering controls,
  keyboard reachability and launcher route behavior at five widths.
- The full production-component fixture passed actual pointer-hit and overflow
  checks at 320, 375, 390, 768, 800, 900, 960, 1023, 1024, 1279, 1280 and 1440
  pixels, plus 390×520. At 768px the composer measured 400px wide; the earlier
  live layout left the entire chat column only 172px wide.
- At 390×520, all 13 slash commands remain keyboard reachable inside a 335px
  menu, reduced to 245px with pending attachment content. The selected last
  option stays visible, focus stays in the textarea, and Escape dismisses it.
- Six targeted axe rules at 390, 768 and 1440px returned zero violations.
  Some unrelated text nodes require manual contrast evaluation; this is a
  focused check, not a complete accessibility certification.
- Browser checks passed for pending/failed attachment Enter guards, retained
  drafts, recovery by removing a failed chip, and one submission after a
  successful upload. The delayed fixture originally retained a live FileList;
  the follow-up snapshots File[] before the native input resets. Production
  upload handling already performs that synchronous copy.
- Live and completed planner/provider summaries render through production
  components. Drawer Tab wrapping, Escape focus restoration, docking to the
  composer at 1280px, and remaining closed on undocking all passed.

Independent review approves the final task with no outstanding findings.
Browser evidence is fixture-backed and does not claim cloud upload, model
execution or production deployment verification. Persisted reasoning and Stop
behavior are covered separately by the hook/backend/database tests above.
Logs: `task4-frozen-playwright.log`, `task4-frozen-browser-rerun.log`, and
`task4-followup-browser.log` in the private evidence directory. The first
frozen matrix attempt navigated before fixture hydration and remained on the
fixture URL; its failures were a harness setup error. The corrected run waits
for the explicit `/chat` route state. Preliminary mutable-tree runs are not
used as final acceptance evidence.

### 2026-09-17 amendment: truthful PDF and usage exports

A4 and A11 are repaired in `a587706f3`. PDF export uses pinned WeasyPrint 70
with the native libraries in the production Dockerfile and a build-time
conversion smoke check. Rendering runs off the event loop, blocks local and
remote resource fetching, and requires real PDF bytes. Missing renderers and
conversion failures return stable 503/500 errors. Authenticated downloads
respect the server filename, validate MIME and signature, and preserve the
requested batch format when ZIP is disabled.

Provider input/output usage is exported from persisted measurements. Partial
values remain explicitly unknown; legacy counters remain distinct and are not
backfilled. Human-readable exports no longer imply a measured zero total.

The frozen baseline regression run failed 12 tests, passed two and deselected
one. It used a private archive of `cf9e9c820`, removing only an unavailable new
schema import from the copied test and deselecting that schema's construction
test. An earlier mixed-source run is not accepted as baseline evidence.
Post-fix focused suites passed 19 new backend tests, 20 existing exporter tests
and seven frontend download/export tests. Independent review approved the
implementation with no findings.

Root verification generated a real 9,498-byte, one-page PDF and extracted its
text with a PDF parser: measured usage was 42 input/7 output; partial usage
was 5 input/unknown output. JSON retained the separate legacy count. Actual
renderer attempts to load file, HTTP and data resources were all blocked.
The browser downloaded identical real PDF bytes with the server-selected
filename, and rejected both an HTML MIME response and HTML bytes labeled as
PDF (three requests, only one download). Browser transport responses were
intercepted locally; this does not claim production route verification.

The full frontend suite passed **2,092 tests across 297 files** in 83.22 seconds
on the frozen export commit. Evidence: `task6-root-real-smoke.log`,
`task6-real-smoke.json`, `task6-root-browser.log`, `final-pdf-browser.json` and
`final-frontend.log` in the private evidence directory. A disposable production
base-image smoke also produced a real PDF; a complete production application
image build has not been run. Root subsequently applied AST-equivalent Black
formatting to three Python files to satisfy the repository's required gate.

The required added-file mypy gate initially caught 19 typing errors in the
new test fixtures. Follow-up `f2a4925e3` fixes those fixtures without changing
product behavior; both added modules pass mypy and all 19 focused tests pass.
Black, isort and Ruff pass. The Prettier-only `a787363e4` completes formatting;
all 41 changed frontend source/test files pass. Independent review approved
both follow-ups with no findings.

### Integration finding A16: member Stop and reconnect authorization

Independent whole-branch review found an additional source-level defect:
workspace editors/admins could start an agent run through the canonical
editable-thread policy, but Stop and reconnect used an owner-only query.
Those members received 404 for their own run. The same owner-only queries
also omitted the canonical deleted-ancestor checks. This is an integration
finding, separate from the 15 live/source-assisted audit findings above; no
multi-user production probing is claimed.

The repair must retain two independent boundaries: access to an editable,
non-deleted thread, and access to the caller's own run. A thread's Redis
stream pointer is insufficient proof of run ownership. Buffered frames can
contain unfinished text and tool events, so replay must match the selected
stream to a caller/org-scoped active or latest run. Missing or mismatched
correlations must return idle rather than another caller's buffered events.

The repair is committed as `2f78c21b0`. Both routes delegate to the existing
canonical resolver without creating threads. Replay requires an exact match
between the caller-scoped active/latest run and the selected stream. Buffers
with no durable ownership correlation, including legacy buffers, return 204.
Stop retains its organization/user/thread/exact-run cancellation checks.

The author verified 31 focused route tests and 11 existing real-model/resolver
tests, with changed-file Ruff, Black, isort and compile checks passing. New
route tests mock the canonical resolver at that boundary; actual membership
semantics and deleted-parent rules are covered by the model/resolver and
previous 188-test service/route matrix. This is layered regression evidence,
not a claim of a live multi-account test.

Root independently disabled the active and terminal stream-correlation guards
one at a time. Each new mismatch regression failed with **200 replay instead
of the required 204**, then both passed after exact byte restoration to the
commit. Two preliminary author mutation attempts failed for unrelated reasons
(old owner-only 404 and invalid temporary syntax); neither is accepted as
proof. See [durable mutation evidence](../testing/chat-mutation-checks.md).

### Final verification on the repair branch

On frozen product commit `2f78c21b0`, the final architecture/API suite passed
**785 tests** in 22.24 seconds with 29 existing warnings. The full frontend
suite passed **2,092 tests / 297 files** on `a587706f3`; subsequent frontend
changes only reflowed one call with Prettier. All 41 changed frontend files
pass formatting. The earlier **188-test** thread service/route matrix and
focused producer/storage/export suites remain applicable to unchanged code.

The final repository wrapper uses Node 24, pnpm 10.18.2 and the private Python
environment. Its test flag is disabled because the suites above run separately:
`scripts/ci/run_local_ci.sh --base origin/develop --skip-tests --frontend`.
Backend full-tree Ruff, all 35 changed Python files' Ruff/Black/isort gates,
all five added Python files' mypy gate, directory docs, OpenAPI drift,
generated TypeScript API contracts, Alembic graph checks, frontend typing,
and the lint/exclusion comparators passed. Baselines were not changed.

Both the targeted evidence migration probe and **full Alembic upgrade from an
empty database** passed on the owned PostgreSQL instance. This final empty-DB
run supersedes the earlier narrower migration-only verification limit; the
new reasoning migration's up/down/up check also passed separately. Scratch
migration databases were removed by the wrapper. This is local database
verification, not a migration of the live site.

The existing frontend baseline remains 116 lint errors, 2,005 warnings and
21 production type-check exclusions. Passing its blocking comparator does not
mean the whole repository is lint-clean. Logs: `final-api-architecture.log`,
`final-frontend.log`, `final-frontend-format.log`, and `final-ci-wrapper.log` in
the private evidence directory.

The wrapper completed with exit 0 and all blocking gates passed, with no
conditional gate skipped. Its advisory full frontend lint exited 1 for the
existing baseline debt reported above.

### Integration finding A17: parked approval disclosure on reconnect

Final review found a further Important authorization gap in the fallback used
when no Redis stream remains. `_pending_confirmation_frame` performs scoped
run lookups, but when the caller has no run it can still load a checkpoint by
shared thread ID and emit its parked approval/tool arguments. It did not
compare the checkpoint's recorded `user_id` to the requesting member. The
normal approval continuation already performs this owner check. This finding
is based on code review, not a live cross-account probe.

The narrow follow-up requires authenticated reconnect to reject checkpoints
with a missing or different owner before emitting an approval prompt. Its
regression must exercise the actual resume-to-checkpoint path, including the
same-owner success case. Earlier green suites lacked this case and therefore
did not establish its safety.

A17 is repaired in `236867d21`. The authenticated fallback now reads the
checkpoint owner and returns idle for a different, absent or malformed owner.
The existing non-DB helper behavior is preserved; the public route supplies
the authenticated database path. The three new route-level tests exercise the
real checkpoint fallback, with graph/storage dependencies mocked. Foreign
and missing owners return 204; the owner returns 200. The focused suite
passed **34 tests**, and changed Python static checks passed.

Root independently bypassed only this owner-check block. Both denied cases
then failed with **200 instead of 204**. Exact bytes were restored in `finally`
and all three cases passed. Independent review approved the follow-up and
closed the final Important finding. No unresolved substantive review finding
remains in the repair scope.

### Closure

After the final checkpoint-owner repair, the architecture/API suite passed
**788 tests** in 23.90 seconds, with the same 29 existing warnings. The backend
CI wrapper was rerun on `236867d21` and passed with no skipped gates, including
contracts, typing/formatting, the targeted migration probe and full empty-DB
Alembic upgrade. The frontend files were unchanged by this follow-up, so the
previous full frontend suite, browser acceptance and frontend CI results
remain applicable without an unrelated repeat. Final logs:
`final-api-architecture-after-checkpoint.log` and
`final-backend-ci-after-checkpoint.log`.

Every task received independent review. Final integration review identified
A16/A17 and both were repaired and re-reviewed; no substantive finding remains
open in the agreed scope. The final product review range is
`7ddc961f0..236867d21`. The original dirty main checkout was kept separate.

The repair branch and worktree are preserved. Local test browser/dev server
and the two audit-owned PostgreSQL/Redis containers were closed after testing.
No deployment, merge or push was performed. Release still requires the
reasoning-summary migration (`u3v4w5x6y7z8`), the updated backend image with PDF
native dependencies, and the frontend build. Live Stop/reload, cloud upload,
PDF and member-access smoke checks should follow that release; the local
results above do not claim the serving site already contains these fixes.
