# Flow: HITL approve and deny

States: HITL-pending → done | error. Scenarios:
`workflow.project-creation-via-chat` (Approve, using `create_project` as the
destructive tool) and `workflow.hitl-deny` (Deny). The Approve path's pending
screenshot is `project.pending`; `hitl.pending` is taken on the Deny path.
`adversarial.hitl-synthetic-scope` stays a `BLOCKED` placeholder.

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
