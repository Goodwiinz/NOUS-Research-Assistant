# @nous/chat-runtime

Private workspace package holding the chat runtime logic that both the web app
(`frontend/`, for example `src/components/chat/aui/ChatRuntimeProvider.tsx` and
`src/hooks/chat/useChatStreaming.ts`) and the terminal client (`terminal/src/`)
depend on. It declares `react` as a peer dependency and exposes three entry
points through `package.json`:

| Entry | File | Contents |
| --- | --- | --- |
| `@nous/chat-runtime` | `runtime.ts` | `useChatRuntimeAdapter` and `ChatRuntimeProps`, the hook that adapts a chat store to the assistant runtime. |
| `@nous/chat-runtime/message` | `message.ts` | `convertMessage`, `toToolCallParts`, `extractConfirmationPreview` and the `HITL_APPROVAL_TOOL` marker used for human-approval tool calls. |
| `@nous/chat-runtime/types` | `types.ts` | `ActivityStep`, `RuntimeAttachment` and `RuntimeMessage` types. |

The package ships TypeScript source directly (no build step). Both consumers
list it as `workspace:*`. A change here affects the web chat and the terminal at
once: run `pnpm --dir frontend type-check` and the terminal tests under
`terminal/tests/` after touching it.
