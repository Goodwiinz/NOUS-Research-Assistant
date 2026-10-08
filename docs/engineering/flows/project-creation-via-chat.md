# Flow: project creation via chat

States: HITL-pending → done. `create_project` is a destructive tool, so this
journey always passes through the approval dialog. No scenario exists yet; it
is added with the runner work and cleans up with
`DELETE /api/v1/projects/{id}`.

```mermaid
flowchart TD
  A[Ask the agent in /chat to create a named project] --> B[Approval needed dialog]
  B --> C["checkpoint: project.pending"]
  C -->|Approve| D[GET /api/v1/projects?search=name returns the project]
  D --> E[Open /projects/id; the project renders]
  E --> F["checkpoint: project.created"]
```
