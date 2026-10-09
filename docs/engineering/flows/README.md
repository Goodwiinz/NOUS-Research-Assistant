# Feature flow charts

One Mermaid chart per feature in `../feature-map.yaml`. A node labelled
`checkpoint: <name>` is a screenshot the mapped QA scenario takes with
`evidence.checkpoint('<name>')`; `scripts/ci/check_feature_map.py` fails when a
`covered` feature's checkpoint has no matching call in
`tests/e2e/qa/scenarios.mjs`. Edit the chart and the scenario together.

The contract for this directory is [../verification.md](../verification.md).

| File | Feature |
| --- | --- |
| [login.md](login.md) | login |
| [chat-send-stream-reload.md](chat-send-stream-reload.md) | chat send, stream, reload persists |
| [hitl-approve-deny.md](hitl-approve-deny.md) | HITL approve and deny |
| [document-upload.md](document-upload.md) | document upload and detail |
| [project-creation-via-chat.md](project-creation-via-chat.md) | project creation via chat |
