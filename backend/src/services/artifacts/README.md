# Artifact services

Server-owned artifacts: versioned, immutable documents that agents and users publish, edit, share and read. HTTP routes are in `backend/src/api/artifacts.py` (`/api/v1/artifacts`); ORM models are `artifact.py` and `artifact_share.py` under `backend/src/models/`.

## Files

| File | Purpose |
| --- | --- |
| `service.py` | Publication protocol: reserve, store, finalize, read. |
| `storage.py` | Private artifact byte storage over the configured object-storage backend. |
| `editing.py` | Browser-owned text edits, submitted through the same immutable publication protocol. |
| `native.py` | Durable publication of completed, server-owned drafts. |
| `sharing.py` | Exact-version sharing with live creator rights and explicit recipients. |
| `lifecycle.py` | Lifecycle workers: announce committed versions to open runs and sweep stale reservations. Driven by the beat tasks in `backend/src/tasks/artifact_tasks.py` (`drain-artifacts` runs every 2 seconds). |

## Rules

- A published version is immutable; an edit creates a new version rather than rewriting bytes.
- Reservations that never finalize are swept by the lifecycle worker, so a crashed publish does not leave a permanent hold.
- A share names an exact version and explicit recipients, and the creator must still hold rights when the share is read.
- Bytes are private: reads go through the service, which checks the caller, never through a public URL.
