# Live evidence journey — 2026-09-29

Backend: https://dev-api.goodwiinz.tech (GIT_SHA 1b3ee4d50850d9c02d6df7fba74760142a169051). Every request below, in order.

| step | as | method | path | status | ids / detail |
|---|---|---|---|---|---|
| 00-auth-me-owner | owner | GET | `/api/v1/auth/me` | 200 |  |
| 00-auth-me-supervisor | supervisor | GET | `/api/v1/auth/me` | 500 |  |
| 00-auth-me-foreign | foreign | GET | `/api/v1/auth/me` | 500 |  |
| 00-auth-me-supervisor-retry | supervisor | GET | `/api/v1/auth/me` | 500 |  |
| 00-auth-me-supervisor-example | sup2 | GET | `/api/v1/auth/me` | 500 |  |
| 00-workspaces-supervisor-probe | sup2 | GET | `/api/v2/workspaces` | 500 |  |
| 00-auth-me-probe-nometadata | probe | GET | `/api/v1/auth/me` | 500 |  |
| 00-auth-session-owner | owner | GET | `/api/v1/auth/session` | 200 |  |
| A1-01-list-workspaces | owner | GET | `/api/v2/workspaces` | 200 |  |
| A1-02-create-workspace | owner | POST | `/api/v2/workspaces` | 201 | id=f7538b4a-d043-4a10-b763-e45ce369d69e |
| A1-03-create-collection | owner | POST | `/api/v2/workspaces/f7538b4a-d043-4a10-b763-e45ce369d69e/collections` | 201 | id=2f7056ed-b0fb-488a-8290-8bf1c8502652 |
| A2-01-enable-engine | owner | POST | `/api/v1/research-engine/projects` | 201 | id=2f7056ed-b0fb-488a-8290-8bf1c8502652; project_id=2f7056ed-b0fb-488a-8290-8bf1c8502652; research_engine_project_id=2dff0f55-c5c0-4fa2-9b22-0922d7eb3bad; blueprint_id=None |
| A2-02-get-project-canonical | owner | GET | `/api/v1/research-engine/projects/2f7056ed-b0fb-488a-8290-8bf1c8502652` | 200 | id=2f7056ed-b0fb-488a-8290-8bf1c8502652; project_id=2f7056ed-b0fb-488a-8290-8bf1c8502652; research_engine_project_id=2dff0f55-c5c0-4fa2-9b22-0922d7eb3bad; blueprint_id=None |
| A2-03-get-project-by-engine-id | owner | GET | `/api/v1/research-engine/projects/2dff0f55-c5c0-4fa2-9b22-0922d7eb3bad` | 307 | Location=/api/v1/research-engine/projects/2f7056ed-b0fb-488a-8290-8bf1c8502652 |
| A3-01-add-member-supervisor | owner | POST | `/api/v2/workspaces/f7538b4a-d043-4a10-b763-e45ce369d69e/members` | 400 |  |
| A3-02-assign-supervisor-role | owner | PUT | `/api/v1/research-engine/projects/2f7056ed-b0fb-488a-8290-8bf1c8502652/roles` | 422 |  |
| A3-03-list-roles | owner | GET | `/api/v1/research-engine/projects/2f7056ed-b0fb-488a-8290-8bf1c8502652/roles` | 200 |  |
| A4-01-foreign-get-project | foreign | GET | `/api/v1/research-engine/projects/2f7056ed-b0fb-488a-8290-8bf1c8502652` | 500 |  |
| A4-02-foreign-get-protocols | foreign | GET | `/api/v1/research-engine/projects/2f7056ed-b0fb-488a-8290-8bf1c8502652/protocols` | 500 |  |
| A4-03-foreign-put-roles | foreign | PUT | `/api/v1/research-engine/projects/2f7056ed-b0fb-488a-8290-8bf1c8502652/roles` | 500 |  |
| A5-01-create-archive-collection | owner | POST | `/api/v2/workspaces/f7538b4a-d043-4a10-b763-e45ce369d69e/collections` | 201 | id=f40af11e-87a0-49a6-a715-714ef2298799 |
| A5-02-enable-engine-archive | owner | POST | `/api/v1/research-engine/projects` | 201 | id=f40af11e-87a0-49a6-a715-714ef2298799; project_id=f40af11e-87a0-49a6-a715-714ef2298799; research_engine_project_id=be1a35e4-c8cb-4200-b3ef-e9d6910894f0; blueprint_id=None |
| A5-03-question-before-archive | owner | POST | `/api/v1/research-engine/projects/f40af11e-87a0-49a6-a715-714ef2298799/questions` | 201 | id=0a512a6a-0f4b-4f72-9740-c6d8b955d74c; project_id=f40af11e-87a0-49a6-a715-714ef2298799 |
| A5-04-patch-archived | owner | PATCH | `/api/v1/projects/f40af11e-87a0-49a6-a715-714ef2298799` | 200 | id=f40af11e-87a0-49a6-a715-714ef2298799; research_engine_project_id=be1a35e4-c8cb-4200-b3ef-e9d6910894f0 |
| A5-05-question-after-archive | owner | POST | `/api/v1/research-engine/projects/f40af11e-87a0-49a6-a715-714ef2298799/questions` | 409 |  |
| A5-06-get-after-archive | owner | GET | `/api/v1/research-engine/projects/f40af11e-87a0-49a6-a715-714ef2298799` | 200 | id=f40af11e-87a0-49a6-a715-714ef2298799; project_id=f40af11e-87a0-49a6-a715-714ef2298799; research_engine_project_id=be1a35e4-c8cb-4200-b3ef-e9d6910894f0; blueprint_id=None |
| A5-07-patch-back-active | owner | PATCH | `/api/v1/projects/f40af11e-87a0-49a6-a715-714ef2298799` | 409 |  |
| A6-01-create-deleted-collection | owner | POST | `/api/v2/workspaces/f7538b4a-d043-4a10-b763-e45ce369d69e/collections` | 201 | id=e5160575-a1d7-43e0-a833-cf33efff2b7e |
| A6-02-enable-engine-deleted | owner | POST | `/api/v1/research-engine/projects` | 201 | id=e5160575-a1d7-43e0-a833-cf33efff2b7e; project_id=e5160575-a1d7-43e0-a833-cf33efff2b7e; research_engine_project_id=b4e79731-f41e-499c-8ede-b4a7f50b1339; blueprint_id=None |
| A6-03-get-before-delete | owner | GET | `/api/v1/research-engine/projects/e5160575-a1d7-43e0-a833-cf33efff2b7e` | 200 | id=e5160575-a1d7-43e0-a833-cf33efff2b7e; project_id=e5160575-a1d7-43e0-a833-cf33efff2b7e; research_engine_project_id=b4e79731-f41e-499c-8ede-b4a7f50b1339; blueprint_id=None |
| A6-04-delete-collection | owner | DELETE | `/api/v2/workspaces/f7538b4a-d043-4a10-b763-e45ce369d69e/collections/e5160575-a1d7-43e0-a833-cf33efff2b7e` | 204 |  |
| A6-05-get-after-delete | owner | GET | `/api/v1/research-engine/projects/e5160575-a1d7-43e0-a833-cf33efff2b7e` | 404 |  |
| A6-06-protocols-after-delete | owner | GET | `/api/v1/research-engine/projects/e5160575-a1d7-43e0-a833-cf33efff2b7e/protocols` | 404 |  |
| A6-07-get-deleted-by-engine-id | owner | GET | `/api/v1/research-engine/projects/b4e79731-f41e-499c-8ede-b4a7f50b1339` | 404 |  |
| A6-08-legacy-deleted | owner | GET | `/api/v1/research-engine/legacy-projects/b4e79731-f41e-499c-8ede-b4a7f50b1339` | 404 |  |
| A7-01-legacy-project | owner | GET | `/api/v1/research-engine/legacy-projects/2dff0f55-c5c0-4fa2-9b22-0922d7eb3bad` | 200 | project_id=2f7056ed-b0fb-488a-8290-8bf1c8502652; research_engine_project_id=2dff0f55-c5c0-4fa2-9b22-0922d7eb3bad |
| B1-01-templates | owner | GET | `/api/v1/research-engine/blueprints/templates` | 200 |  |
| B1-02-template-evidence-synthesis | owner | GET | `/api/v1/research-engine/blueprints/templates/evidence_synthesis` | 200 |  |
| B1-03-create-question | owner | POST | `/api/v1/research-engine/projects/2f7056ed-b0fb-488a-8290-8bf1c8502652/questions` | 201 | id=90f5a91a-dfcf-4936-8929-04684bd88e3f; project_id=2f7056ed-b0fb-488a-8290-8bf1c8502652 |
| B1-04-question-version | owner | POST | `/api/v1/research-engine/questions/90f5a91a-dfcf-4936-8929-04684bd88e3f/versions` | 201 | id=90f5a91a-dfcf-4936-8929-04684bd88e3f; project_id=2f7056ed-b0fb-488a-8290-8bf1c8502652 |
| B1-05-create-blueprint | owner | POST | `/api/v1/research-engine/blueprints/projects/2f7056ed-b0fb-488a-8290-8bf1c8502652` | 201 | id=fab71505-9dd2-4b00-b186-9f843c2b9ccf; project_id=2f7056ed-b0fb-488a-8290-8bf1c8502652; research_engine_project_id=2dff0f55-c5c0-4fa2-9b22-0922d7eb3bad |
| B1-06-create-protocol | owner | POST | `/api/v1/research-engine/projects/2f7056ed-b0fb-488a-8290-8bf1c8502652/protocols` | 201 | id=4a88e789-e605-400f-ac8c-7437f8f59072; project_id=2f7056ed-b0fb-488a-8290-8bf1c8502652 |
| B2-01-owner-approve-no-role | owner | POST | `/api/v1/research-engine/protocols/4a88e789-e605-400f-ac8c-7437f8f59072/versions/d38ab741-2deb-4c29-aae2-679754b74177/approve` | 403 |  |
| B2-02-owner-self-assign-supervisor | owner | PUT | `/api/v1/research-engine/projects/2f7056ed-b0fb-488a-8290-8bf1c8502652/roles` | 200 | id=7f2329f2-ecce-4e5b-a0b9-21746e43d826; project_id=2f7056ed-b0fb-488a-8290-8bf1c8502652 |
| B2-03-owner-self-approve-as-supervisor | owner | POST | `/api/v1/research-engine/protocols/4a88e789-e605-400f-ac8c-7437f8f59072/versions/d38ab741-2deb-4c29-aae2-679754b74177/approve` | 403 |  |
| B2-04-supervisor-approve-unprovisioned | sup2 | POST | `/api/v1/research-engine/protocols/4a88e789-e605-400f-ac8c-7437f8f59072/versions/d38ab741-2deb-4c29-aae2-679754b74177/approve` | 500 |  |
| B3-01-run-draft-protocol | owner | POST | `/api/v1/research-engine/blueprints/fab71505-9dd2-4b00-b186-9f843c2b9ccf/runs` | 409 |  |
| B3-02-run-no-protocol | owner | POST | `/api/v1/research-engine/blueprints/fab71505-9dd2-4b00-b186-9f843c2b9ccf/runs` | 409 |  |
| C1-01-list-documents | owner | GET | `/api/v1/documents?page=1&size=100` | 200 |  |
| C1-02-attach-documents | owner | POST | `/api/v2/workspaces/f7538b4a-d043-4a10-b763-e45ce369d69e/collections/2f7056ed-b0fb-488a-8290-8bf1c8502652/documents` | 200 | id=2f7056ed-b0fb-488a-8290-8bf1c8502652 |
| C1-03-get-collection-after-attach | owner | GET | `/api/v2/workspaces/f7538b4a-d043-4a10-b763-e45ce369d69e/collections/2f7056ed-b0fb-488a-8290-8bf1c8502652` | 500 |  |
| C1-04-get-research-project | owner | GET | `/api/v1/projects/2f7056ed-b0fb-488a-8290-8bf1c8502652` | 200 | id=2f7056ed-b0fb-488a-8290-8bf1c8502652; research_engine_project_id=2dff0f55-c5c0-4fa2-9b22-0922d7eb3bad |
| C1-05-get-collection-retry | owner | GET | `/api/v2/workspaces/f7538b4a-d043-4a10-b763-e45ce369d69e/collections/2f7056ed-b0fb-488a-8290-8bf1c8502652` | 500 |  |
| C1-06-get-empty-collection-control | owner | GET | `/api/v2/workspaces/f7538b4a-d043-4a10-b763-e45ce369d69e/collections/f40af11e-87a0-49a6-a715-714ef2298799` | 200 | id=f40af11e-87a0-49a6-a715-714ef2298799 |
| C1-07-get-collection-v2-flat | owner | GET | `/api/v2/collections/2f7056ed-b0fb-488a-8290-8bf1c8502652` | 500 |  |
| C1-08-get-document-one | owner | GET | `/api/v1/documents/23ade6c7-10f1-4c95-9e4a-4bccff6ece2c` | 200 | id=23ade6c7-10f1-4c95-9e4a-4bccff6ece2c |
| C2-01-generate-draft | owner | POST | `/api/v1/projects/2f7056ed-b0fb-488a-8290-8bf1c8502652/drafts?themes=retrieval-augmented%20generation%20and%20hallucination&themes=grounding%20and%20verification%20of%20LLM%20outputs&style=academic&max_sections=4&include_abstract=true&document_ids=23ade6c7-10f1-4c95-9e4a-4bccff6ece2c&document_ids=0b861512-9a44-46d6-aa58-56ed0142adb0&document_ids=aa67db1b-8077-4c15-b9e5-9ec7a1beaa62&document_ids=f92e6be6-20b5-41b6-9300-b29b8b7e3fb1&document_ids=8f7f2119-182d-4a00-b19a-7a7e16abea17&document_ids=03be7c34-698e-4c1b-8df0-e2440a0db101&document_ids=0d73d115-762d-4c3c-95bb-75ee470c6813&document_ids=6a9b211c-5505-41c0-9ccc-c81c04c92c8a&document_ids=43d53bca-a5f4-4eda-9c43-ec91068e74e8&document_ids=b0fec607-9fe1-4024-a2bb-fb3201d709c3&document_ids=22aa7651-4a11-4a54-ae71-20af5a45dd70&document_ids=14c3a929-e371-461c-a76b-d38ed177a950` | 202 | task_id=212088fd5ead |
| C2-02-draft-status | owner | GET | `/api/v1/projects/2f7056ed-b0fb-488a-8290-8bf1c8502652/drafts/status/212088fd5ead` | 200 | project_id=2f7056ed-b0fb-488a-8290-8bf1c8502652; task_id=212088fd5ead |
| C2-02-draft-status | owner | GET | `/api/v1/projects/2f7056ed-b0fb-488a-8290-8bf1c8502652/drafts/status/212088fd5ead` | 200 | project_id=2f7056ed-b0fb-488a-8290-8bf1c8502652; task_id=212088fd5ead |
| 00-auth-me-supervisor-example-retry2 | sup2 | GET | `/api/v1/auth/me` | 500 |  |
| 00-auth-me-foreign-retry2 | foreign | GET | `/api/v1/auth/me` | 500 |  |
| C2-02-draft-status | owner | GET | `/api/v1/projects/2f7056ed-b0fb-488a-8290-8bf1c8502652/drafts/status/212088fd5ead` | 200 | project_id=2f7056ed-b0fb-488a-8290-8bf1c8502652; task_id=212088fd5ead |
| C2-02-draft-status | owner | GET | `/api/v1/projects/2f7056ed-b0fb-488a-8290-8bf1c8502652/drafts/status/212088fd5ead` | 200 | project_id=2f7056ed-b0fb-488a-8290-8bf1c8502652; task_id=212088fd5ead |
| C2-03-draft-reviews | owner | GET | `/api/v1/projects/2f7056ed-b0fb-488a-8290-8bf1c8502652/drafts/reviews` | 200 |  |
| C2-04-draft-current | owner | GET | `/api/v1/projects/2f7056ed-b0fb-488a-8290-8bf1c8502652/drafts/current` | 404 |  |
| C2-05-drafts-list | owner | GET | `/api/v1/projects/2f7056ed-b0fb-488a-8290-8bf1c8502652/drafts` | 200 |  |
| C3-01-foreign-draft-reviews | foreign | GET | `/api/v1/projects/2f7056ed-b0fb-488a-8290-8bf1c8502652/drafts/reviews` | 500 |  |
| C2-06-draft-status-list | owner | GET | `/api/v1/projects/2f7056ed-b0fb-488a-8290-8bf1c8502652/drafts/status` | 200 | project_id=2f7056ed-b0fb-488a-8290-8bf1c8502652; task_id=212088fd5ead |
| 00-auth-me-supervisor-nousinvalid-retry | supervisor | GET | `/api/v1/auth/me` | 500 |  |

## Notes (recorded 2026-09-29, observed facts only)

- Principals created in Supabase auth (dev project): `eval-2026-09-29-supervisor@nous.invalid` (35c6f0d4-99f4-4cac-a186-9a13f6e1a014, app_metadata.organization_id=e050bd43-…), `eval-2026-09-29-foreign@nous.invalid` (c2b858ae-76b3-4274-b3b4-d260a8fc8946, user_metadata.organization_name=eval-2026-09-29-foreign-org), fallback `eval-2026-09-29-supervisor@example.com` (6a3cfda8-4572-4027-9e38-7c9aaba76932), probe `eval-2026-09-29-probe@example.com` (519fc0df-faac-4c18-8de0-f630bab1348e, no metadata). All four logged in (password grant 200) but EVERY backend call with their tokens returned 500 `DATABASE_ERROR` (JIT provisioning never produced a DB user row; no backend user/org ids exist). Domain (.invalid vs example.com) and metadata presence made no difference.
- Consequence: the supervisor-approval path, all foreign-principal denials, and everything downstream of an approved protocol (run, steps, manifest, export, hash checks) could NOT be exercised. Those rows show 500s, not denials.
- A5 was run on a throwaway collection (`eval-2026-09-29-archived`) instead of the canonical project because `archived` is terminal in `project_service._VALID_TRANSITIONS` (`"archived": set()`); the revert PATCH (A5-07) returned 409 as that code predicts.
- `GET /api/v2/workspaces/{ws}/collections/{cid}` (and `/api/v2/collections/{cid}`) returned 500 `DATABASE_ERROR` for the canonical project once it had 12 documents attached; the same GET on an empty collection returned 200.
- The draft (C2) was blocked by citation review (50 uncited assertions); no draft persisted, so D (LaTeX/BibTeX export) had no draft to export.
