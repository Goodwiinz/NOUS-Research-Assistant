# Flow: chat send, stream, reload persists

States: empty → streaming → done | error. Scenario:
`workflow.reload-persistence` (today it seeds the message through the API; the
composer send path is added with the runner work).

```mermaid
flowchart TD
  A[Open /chat signed in] --> B[Type in the Message composer, press Send message]
  B --> C["checkpoint: chat.sent"]
  C --> D[Assistant row streams]
  D -->|stream error| E[Error state shown in the transcript]
  D -->|terminal| F["checkpoint: chat.streamed"]
  F --> G[Reload /chat?thread=id]
  G --> H[User and assistant rows still rendered; API lists the user message]
  H --> I["checkpoint: chat.reloaded"]
```
