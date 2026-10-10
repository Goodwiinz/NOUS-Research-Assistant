# Task 4 mutation evidence

These focused mutations verify the behavior guards added for the responsive
chat controls and Escape ownership. Each mutant was applied alone under Node
24.21.0, its covering test failed, and the original source bytes were restored
before the next mutant. Every post-restore sanity run passed.

| Guard                          | Mutation and covering test                                                                                                                          | Result                                                                           |
| ------------------------------ | --------------------------------------------------------------------------------------------------------------------------------------------------- | -------------------------------------------------------------------------------- |
| Slash menu viewport cap        | Changed the menu `overflowY` style from `auto` to `hidden`; `SlashCommandMenu.test.tsx -t "caps its height"`                                        | Killed; the test no longer observed the scrollable menu contract.                |
| Slash active-option visibility | Disabled the `scrollIntoView({ block: 'nearest' })` call; `SlashCommandMenu.test.tsx -t "caps its height"`                                          | Killed; the highlighted option was no longer scrolled into view.                 |
| Trimmed thread search          | Removed `.trim()` before lower-casing; `ChatSidebar.test.tsx -t "trims surrounding whitespace"`                                                     | Killed; the padded exact title no longer matched.                                |
| Search paging reachability     | Restored the old `!hasSearchQuery` condition; `ChatSidebar.test.tsx -t "keeps older-thread pagination"`                                             | Killed; the loading action and loaded-thread guidance disappeared during search. |
| Rename accessible name         | Removed the `Thread name` label; `ChatDialogs.ime.test.tsx -t "gives the rename textbox"`                                                           | Killed; the textbox lost its accessible name.                                    |
| Textarea autocomplete contract | Replaced `aria-haspopup=listbox` with invalid `aria-expanded` on the textarea; `ChatInput-streaming.test.tsx -t "uses valid textarea autocomplete"` | Killed; the test observed the invalid attribute.                                 |
| Radix Escape ownership         | Removed the `event.composedPath()` overlay check; `ArtifactPanel.test.tsx -t "keeps the artifact open when a Radix palette consumes Escape"`        | Killed; the detached Radix dialog no longer protected the artifact panel.        |

The final mutation logs are under `/tmp/chat-audit-20260916/task4-mutation-*.log`.
The first A13 test allowed another guard to satisfy the assertion; that test
was strengthened to dispatch a real Radix dialog Escape with
`defaultPrevented === false` after the dialog unmounts, then the event-path
mutant was rerun and killed. Color-token assertions were deliberately omitted;
selected slash-row and shortcut contrast are verified by the real browser axe
check owned by the root agent.
