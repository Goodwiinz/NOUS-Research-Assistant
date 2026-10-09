# Flow: HITL approve and deny

States: HITL-pending → done | error. No runnable scenario exists yet;
`adversarial.hitl-synthetic-scope` is a permanent `BLOCKED` placeholder, so
the approve and deny journeys are added with the runner work.

```mermaid
flowchart TD
  A[Ask the agent for a destructive tool call] --> B[Run pauses on event: confirmation]
  B --> C[Approval needed dialog shows Approve and Deny]
  C --> D["checkpoint: hitl.pending"]
  D -->|Approve| E[POST /api/v1/agent/stream/confirm confirmed=true]
  E --> F[Run resumes; the tool's effect exists]
  F --> G["checkpoint: hitl.approved"]
  D -->|Deny| H[POST /api/v1/agent/stream/confirm confirmed=false]
  H --> I[Run ends without the effect; transcript says so]
  I --> J["checkpoint: hitl.denied"]
```
