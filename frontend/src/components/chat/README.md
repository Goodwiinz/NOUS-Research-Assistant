# chat/

Standard RAG chat UI: message list, composer, sidebar, and citation surface. All components render over `useChatStore` (Zustand) and the `ChatPageMessage` / `cloudMessageView` view-model layer.

Distinct from `agent-chat/`: that directory owns the LangGraph agent panel — SSE tool-execution cards, HITL confirmation cards, plan cards, a floating action button, and an agent thread list. `chat/` is the primary research chat surface (threads, RAG toggle, slash commands); `agent-chat/` is the overlay that appears when the LangGraph agent runs tools inside those threads. The two share `InlineAgentSummary` (in `shared/`) as the seam.

---

## Key components

| Component                             | What it renders                                                                                                                                                                                                                                                                                                                                                                             |
| ------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `ChatMessageList`                     | Scrollable transcript. Memoized. Throttled auto-scroll via `requestAnimationFrame`. Renders `ChatBubble` (from `shared/`) per message, an in-flight streaming bubble via `storeIsStreaming`, ephemeral `CommandOutputBubble`s, and a "scroll to bottom" button when the user scrolls away. Reads `streamingCitations` from `useChatStore` to show a "Reading N sources" chip mid-stream.    |
| `shared/ChatBubble`                   | Single message bubble (user pill or assistant manuscript column). Shows a `ThinkingPill` (animated Sol dot) before any token arrives; switches to raw text while streaming with a blinking cursor; renders `CitationRenderer` once the message is complete. Hosts a `ToolStrip` (tools used, source count, response time, "Stopped" flag) and hover-reveal copy/retry actions.              |
| `ChatInput`                           | Composer. Auto-resizing `<textarea>` capped at 200 px. Top strip: phase-aware status pill (retrieving → writing → thinking), "Use my sources" RAG switch, character count bar. Bottom bar: file attach, image attach, voice input (`SpeechRecognition` API), slash-command trigger, Send/Stop. Delegates slash-command navigation to `useSlashCommandMenu`.                                 |
| `CitationRenderer`                    | Renders assistant content with inline `[Doc N]` markers interleaved as `CitationLink` chips. Splits fenced code out before citation parsing so `arr[1]` in a code block never becomes a chip. All markdown goes through `ChatMarkdown`; falls back to the plain path when no citation markers are present.                                                                                  |
| `ChatMarkdown`                        | The single `react-markdown` config for message bodies: `remark-gfm` (tables, task lists, strikethrough, autolinks), dynamically-loaded `SyntaxHighlighter` (SSR disabled) for languaged fences, plain `<pre>` for unlanguaged/indented blocks, scrollable tables, and `rel="noopener noreferrer"` on all LLM-authored links. `inline` variant unwraps `<p>`→`<span>` for citation segments. |
| `CitationLink`                        | Superscript chip for an inline citation reference. Passes through to the citation panel on click.                                                                                                                                                                                                                                                                                           |
| `CitationPanel`                       | Slide-in panel showing the full source list.                                                                                                                                                                                                                                                                                                                                                |
| `CommandOutputBubble`                 | Terminal-style bubble for slash-command results. Shows echoed command, monospace `lines`, or clickable `items` (switch thread, set project, cite paper). Ephemeral — not persisted or sent to the agent.                                                                                                                                                                                    |
| `ChatHeader`                          | Thread title, settings trigger, model badge.                                                                                                                                                                                                                                                                                                                                                |
| `ChatSidebar` / `ConversationSidebar` | Left panel listing threads. Delegates to `VirtualizedConversationList` when the thread count exceeds 50 (uses `react-window` `VariableSizeList`); falls back to a plain list below that threshold. Supports select mode, inline rename, bookmark, archive, export, and tag.                                                                                                                 |
| `VirtualizedConversationList`         | `react-window`-based virtualized thread list with time-grouped headers (Today / Yesterday / This Week / This Month / Older). Switches to non-virtualized rendering under 50 conversations.                                                                                                                                                                                                  |
| `SlashCommandMenu`                    | ARIA listbox (`role="listbox"`) positioned above the composer. Keyboard-navigable (↑↓ Tab Enter Esc). Populated by `useSlashCommandMenu` filtering `SLASH_COMMANDS`.                                                                                                                                                                                                                        |
| `WelcomeState`                        | Empty-state screen shown before the first message.                                                                                                                                                                                                                                                                                                                                          |
| `ChatDialogs`                         | Modal dialogs for confirmation flows.                                                                                                                                                                                                                                                                                                                                                       |
| `shared/InlineAgentSummary`           | Collapsed pill above an assistant bubble showing agent steps for the current thread. Reads from `agentActivityStore` (the same store that powers the `agent-chat/` side rail). Expands to a step-by-step list on click.                                                                                                                                                                     |
| `shared/SearchComposer`               | Composer variant used in search-page chat mode.                                                                                                                                                                                                                                                                                                                                             |

---

## Streaming

`ChatMessageList` receives `storeIsStreaming` and `storeStreamingContent` from the parent page (sourced from `useChatStore`). While streaming it renders a synthetic `ChatBubble` with `isStreaming=true`. Inside `ChatBubble`, the streaming branch renders raw `whitespace-pre-wrap` text plus an animated cursor (`data-testid="streaming-cursor"`); `CitationRenderer` is only mounted once `isStreaming` is false and the message is committed.

The status pill keeps its 3×3 activity matrix while narrating real stream progress: server status details, plan creation, retrieval counts, and active tool work, with the coarse phase as fallback. It never cycles invented phrases.

---

## State sources

- `useChatStore` (`@/store/chat-store`): streaming state, `streamingCitations`, active thread.
- `useAgentActivityStore` (`@/stores/agentActivityStore`): agent run steps, consumed by `InlineAgentSummary`.
- `shared/cloudMessageView.ts`: `selectDisplayedMessages` reconciles optimistic local messages with the store's authoritative `ChatMessage[]` (prefers store when at least as long as local).
- `shared/messageViewModel.ts`: maps search-result payloads to `ChatMessageViewModel[]` (used by search-page chat mode).
- `shared/threadConversationState.ts`: `upsertConversationFromThread` — upserts thread metadata plus an explicit bounded message page into the conversation list.

---

## Subdir: `shared/`

Logic and primitives shared across chat surfaces:

| File                         | Purpose                                                                                                   |
| ---------------------------- | --------------------------------------------------------------------------------------------------------- |
| `ChatBubble.tsx`             | Canonical bubble component (used by both `ChatMessageList` and the streaming slot)                        |
| `InlineAgentSummary.tsx`     | Agent-step pill; seam between `chat/` and `agent-chat/`                                                   |
| `SearchComposer.tsx`         | Composer for the search-page chat entry point                                                             |
| `cloudMessageView.ts`        | View-model mapping: store `ChatMessage[]` → `ChatPageMessage[]`; `selectDisplayedMessages` reconciliation |
| `messageViewModel.ts`        | Maps chat-route and search-result payloads to `ChatMessageViewModel`                                      |
| `threadConversationState.ts` | Thread upsert helper for conversation lists                                                               |
| `threadCreation.ts`          | Helpers for creating new threads                                                                          |
| `threadPreviewHydration.ts`  | Hydrates sidebar preview text from thread detail                                                          |
| `chatNavigation.ts`          | Utilities for navigating between threads                                                                  |
| `exportConversation.ts`      | Serialises a conversation to markdown or JSON for download                                                |
