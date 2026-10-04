# Live evidence journey — 2026-09-30 (owner-side)

Backend: https://dev-api.goodwiinz.tech. Deployed SHA not readable from this session (kubectl → `aws login` session expired; `/health` exposes only `version: 1.0.0`). GitOps-pinned image on develop (`values-aws.yaml`) is `5ee337cfed3e2394c2246f5b7b35af6133c5793e` (contains #1730; does not contain #1742, consistent with D1-03 still returning 500). All requests made as the owner (allocs16@gmail.com, user 87d4b23a-dc89-4c9a-817e-e9ca9e42fd2a). Bearer tokens redacted; no idempotency keys were sent.

The D2-01 query string is the same as `2026-09-29-live-evidence/C2-01-generate-draft.json` (12 `document_ids`); the full path is in the JSON file.

| step | method | path | status | ids / detail |
|---|---|---|---|---|
| 00-auth-me-owner | GET | `/api/v1/auth/me` | 200 |  |
| D1-01-drafts-list | GET | `/api/v1/projects/2f7056ed-b0fb-488a-8290-8bf1c8502652/drafts` | 200 |  |
| D1-02-draft-reviews | GET | `/api/v1/projects/2f7056ed-b0fb-488a-8290-8bf1c8502652/drafts/reviews` | 200 | reviews=2e3adf79:blocked |
| D1-03-get-collection | GET | `/api/v2/workspaces/f7538b4a-d043-4a10-b763-e45ce369d69e/collections/2f7056ed-b0fb-488a-8290-8bf1c8502652` | 500 | error=A database error occurred. Please try again later. |
| D2-01-generate-draft | POST | `/api/v1/projects/2f7056ed-b0fb-488a-8290-8bf1c8502652/drafts` | 202 | task_id=ad15f6592223; task_status=pending |
| D2-02-draft-status | GET | `/api/v1/projects/2f7056ed-b0fb-488a-8290-8bf1c8502652/drafts/status/ad15f6592223` | 200 | task_id=ad15f6592223; task_status=failed |
| D2-03-draft-reviews | GET | `/api/v1/projects/2f7056ed-b0fb-488a-8290-8bf1c8502652/drafts/reviews` | 200 | reviews=be869bdc:blocked,2e3adf79:blocked |
| D2-04-draft-current | GET | `/api/v1/projects/2f7056ed-b0fb-488a-8290-8bf1c8502652/drafts/current` | 404 | error=No current draft found |
| D2-05-drafts-list | GET | `/api/v1/projects/2f7056ed-b0fb-488a-8290-8bf1c8502652/drafts` | 200 |  |
| E1-01-get-blueprint | GET | `/api/v1/research-engine/blueprints/fab71505-9dd2-4b00-b186-9f843c2b9ccf` | 200 | id=fab71505-9dd2-4b00-b186-9f843c2b9ccf |
| E1-02-list-protocols | GET | `/api/v1/research-engine/projects/2f7056ed-b0fb-488a-8290-8bf1c8502652/protocols` | 200 |  |
| E1-03-get-project | GET | `/api/v1/research-engine/projects/2f7056ed-b0fb-488a-8290-8bf1c8502652` | 200 | id=2f7056ed-b0fb-488a-8290-8bf1c8502652 |
| E1-04-run-draft-protocol-retry | POST | `/api/v1/research-engine/blueprints/fab71505-9dd2-4b00-b186-9f843c2b9ccf/runs` | 409 | error=approved_protocol_required |
| F1-01-get-draft | GET | `/api/v1/projects/a7460d29-feee-456b-abf2-441a8008921a/drafts/1f01336d-2c57-44cd-a4a5-2924533ab12d` | 200 | id=1f01336d-2c57-44cd-a4a5-2924533ab12d |
| F1-02-draft-citations | GET | `/api/v1/projects/a7460d29-feee-456b-abf2-441a8008921a/drafts/1f01336d-2c57-44cd-a4a5-2924533ab12d/citations` | 200 |  |
| F1-03-export-markdown-apa | POST | `/api/v1/projects/a7460d29-feee-456b-abf2-441a8008921a/drafts/1f01336d-2c57-44cd-a4a5-2924533ab12d/export?format=markdown&bib_format=apa` | 200 | sha256=8d34856e558ad6cf1a3dff25b3d971508f8312bdd8c1fbbefd221a94ff856708; bytes=14722 |
| F1-04-export-latex-zip | POST | `/api/v1/projects/a7460d29-feee-456b-abf2-441a8008921a/drafts/1f01336d-2c57-44cd-a4a5-2924533ab12d/export?format=latex` | 200 | sha256=37d92ec2e622d1497d17ae9287f7854223153cf7305ce28434135b92a7603aed; bytes=6057 |
| F1-05-get-document-doc1 | GET | `/api/v1/documents/74c232ea-e007-4a73-b8ed-df0f88bae654` | 200 | id=74c232ea-e007-4a73-b8ed-df0f88bae654 |
| F1-06-get-document-doc2 | GET | `/api/v1/documents/20492039-b419-48a5-80d8-b4d8c69dca6e` | 200 | id=20492039-b419-48a5-80d8-b4d8c69dca6e |
| F1-07-get-document-doc3 | GET | `/api/v1/documents/1ddadd2f-8817-48ac-95b3-1a7a20b7e703` | 200 | id=1ddadd2f-8817-48ac-95b3-1a7a20b7e703 |
| F1-08-get-document-doc4 | GET | `/api/v1/documents/4f279d5b-35e3-4a6e-bfc1-38b7120dd1b9` | 200 | id=4f279d5b-35e3-4a6e-bfc1-38b7120dd1b9 |
| F1-09-get-document-doc5 | GET | `/api/v1/documents/8b220cbe-6d5f-459c-a435-b7c590694e51` | 200 | id=8b220cbe-6d5f-459c-a435-b7c590694e51 |
| F1-10-get-document-doc6 | GET | `/api/v1/documents/1299ae5f-c4c2-4169-95b2-cf6b3206969c` | 200 | id=1299ae5f-c4c2-4169-95b2-cf6b3206969c |

Per-ticket write-ups: `../2026-09-30-goo-290-export.md`, `../2026-09-30-goo-291-downloads.md`, `../2026-09-30-goo-292-review.md`.
