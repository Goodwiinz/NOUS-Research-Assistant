# Live evidence journey — 2026-09-30b (provisioned supervisor, reviewers and foreign user)

Backend: https://dev-api.goodwiinz.tech, image `8ad2c82` per the dispatch (GitOps PR #1774 `chore(gitops): propose dev image 8ad2c82` is on `develop`; `kubectl`/`aws` were not usable from this session — AWS session expired — so the running pod SHA was not read directly). Every request below, in order. Bearer tokens and idempotency-key values in request bodies are redacted to `<redacted>`; no passwords are stored.

Principals (all JIT-provisioned on first `/auth/me`, `00-*` files):

| alias | email | backend user id | org |
|---|---|---|---|
| owner | allocs16@gmail.com | 87d4b23a-dc89-4c9a-817e-e9ca9e42fd2a | e050bd43-6b0b-4425-9848-1bc5ad9d5cc2 |
| supervisor | eval-2026-09-30-supervisor@nous.invalid | d0a0dd30-0cba-4802-821e-b74ce646e2c0 | bd7619bb-… (own org) |
| foreign | eval-2026-09-30-foreign@nous.invalid | e4457f63-d394-4c6f-b84e-c99e031ac487 | 917b5fbc-… (own org) |
| approver | eval-2026-09-30-approver@nous.invalid | 4d2e5308-01b5-4e71-99e4-2e01295eb3a4 | e050bd43-… (owner org) |
| reviewer | eval-2026-09-30-reviewer@nous.invalid | 0747f5eb-7df2-4e9f-95c1-9ab91db3ac9b | e050bd43-… (owner org) |
| reviewer2 | eval-2026-09-30-reviewer2@nous.invalid | 56f4d19d-fdb8-425a-acbc-19dad813c970 | e050bd43-… (owner org) |

`approver`, `reviewer` and `reviewer2` were created on 2026-09-30 through the Supabase admin API with `email_confirm: true` and `app_metadata.organization_id` set to the owner's org, because the pre-provisioned `supervisor` landed in its own org and research-engine roles are org-scoped (`A1-02` → 422, `A1-03`/`A1-04` → 404). `approver` plays the supervisor role for the rest of the journey.

| step | as | method | path | status | ids / detail |
|---|---|---|---|---|---|
| 00-auth-me-owner | owner | GET | `/api/v1/auth/me` | 200 |  |
| 00-auth-me-supervisor | supervisor | GET | `/api/v1/auth/me` | 200 |  |
| 00-auth-me-foreign | foreign | GET | `/api/v1/auth/me` | 200 |  |
| A1-01-add-member-supervisor | owner | POST | `/api/v2/workspaces/f7538b4a-d043-4a10-b763-e45ce369d69e/members` | 201 | id=cb3c47c9-e31f-4c00-8e34-d094d9dfc574 |
| A1-02-assign-supervisor-role | owner | PUT | `/api/v1/research-engine/projects/2f7056ed-b0fb-488a-8290-8bf1c8502652/roles` | 422 | error=User is not an eligible project member |
| A1-03-supervisor-list-roles | supervisor | GET | `/api/v1/research-engine/projects/2f7056ed-b0fb-488a-8290-8bf1c8502652/roles` | 404 | error=Project not found |
| A1-04-supervisor-get-project | supervisor | GET | `/api/v1/research-engine/projects/2f7056ed-b0fb-488a-8290-8bf1c8502652` | 404 | error=Project not found |
| 00-auth-me-approver | approver | GET | `/api/v1/auth/me` | 200 |  |
| 00-auth-me-reviewer | reviewer | GET | `/api/v1/auth/me` | 200 |  |
| 00-auth-me-reviewer2 | reviewer2 | GET | `/api/v1/auth/me` | 200 |  |
| A1-05-add-member-approver | owner | POST | `/api/v2/workspaces/f7538b4a-d043-4a10-b763-e45ce369d69e/members` | 201 | id=1e8c4bb4-d286-40bd-b496-2291965aa8b7 |
| A1-06-add-member-reviewer | owner | POST | `/api/v2/workspaces/f7538b4a-d043-4a10-b763-e45ce369d69e/members` | 201 | id=cad79390-bb8a-405e-9413-8c89f0267a11 |
| A1-07-add-member-reviewer2 | owner | POST | `/api/v2/workspaces/f7538b4a-d043-4a10-b763-e45ce369d69e/members` | 201 | id=b1d32e01-213b-46dc-966b-45e41defcf0d |
| A2-01-assign-approver-supervisor | owner | PUT | `/api/v1/research-engine/projects/2f7056ed-b0fb-488a-8290-8bf1c8502652/roles` | 200 | id=431af541-531d-4bf0-92eb-8399f3d5ac74 |
| A2-02-assign-reviewer | owner | PUT | `/api/v1/research-engine/projects/2f7056ed-b0fb-488a-8290-8bf1c8502652/roles` | 200 | id=e63d1ba2-f2e2-4430-864b-df042acc6d3f |
| A2-03-assign-reviewer2 | owner | PUT | `/api/v1/research-engine/projects/2f7056ed-b0fb-488a-8290-8bf1c8502652/roles` | 200 | id=a4a440a3-b1e4-4682-99ed-2d206bab5e41 |
| A2-04-assign-owner-adjudicator | owner | PUT | `/api/v1/research-engine/projects/2f7056ed-b0fb-488a-8290-8bf1c8502652/roles` | 200 | id=8aaedcde-3068-4ebe-b84b-44d29daf6e6c |
| A2-05-approver-list-roles | approver | GET | `/api/v1/research-engine/projects/2f7056ed-b0fb-488a-8290-8bf1c8502652/roles` | 200 | items=5 |
| A3-01-foreign-get-project | foreign | GET | `/api/v1/research-engine/projects/2f7056ed-b0fb-488a-8290-8bf1c8502652` | 404 | error=Project not found |
| A3-02-foreign-get-protocols | foreign | GET | `/api/v1/research-engine/projects/2f7056ed-b0fb-488a-8290-8bf1c8502652/protocols` | 404 | error=Project not found |
| A3-03-foreign-put-roles | foreign | PUT | `/api/v1/research-engine/projects/2f7056ed-b0fb-488a-8290-8bf1c8502652/roles` | 404 | error=Project not found |
| A3-04-foreign-get-roles | foreign | GET | `/api/v1/research-engine/projects/2f7056ed-b0fb-488a-8290-8bf1c8502652/roles` | 404 | error=Project not found |
| A4-01-approver-get-archived | approver | GET | `/api/v1/research-engine/projects/f40af11e-87a0-49a6-a715-714ef2298799` | 200 | id=f40af11e-87a0-49a6-a715-714ef2298799; status=archived |
| A4-02-approver-question-archived (1st try) | approver | POST | `/api/v1/research-engine/projects/f40af11e-87a0-49a6-a715-714ef2298799/questions` | 422 | invalid body (missing `question`); file overwritten by the retry below |
| A4-03-approver-get-deleted | approver | GET | `/api/v1/research-engine/projects/e5160575-a1d7-43e0-a833-cf33efff2b7e` | 404 | error=Project not found |
| A4-04-approver-protocols-deleted | approver | GET | `/api/v1/research-engine/projects/e5160575-a1d7-43e0-a833-cf33efff2b7e/protocols` | 404 | error=Project not found |
| A4-05-approver-get-deleted-by-engine-id | approver | GET | `/api/v1/research-engine/projects/b4e79731-f41e-499c-8ede-b4a7f50b1339` | 404 | error=Project not found |
| A4-06-approver-get-project | approver | GET | `/api/v1/research-engine/projects/2f7056ed-b0fb-488a-8290-8bf1c8502652` | 200 | id=2f7056ed-b0fb-488a-8290-8bf1c8502652; status=active |
| A4-02-approver-question-archived | approver | POST | `/api/v1/research-engine/projects/f40af11e-87a0-49a6-a715-714ef2298799/questions` | 409 | error=Project is not writable |
| B1-01-get-protocol | approver | GET | `/api/v1/research-engine/protocols/4a88e789-e605-400f-ac8c-7437f8f59072` | 200 | id=4a88e789-e605-400f-ac8c-7437f8f59072 |
| B1-02-owner-self-approve | owner | POST | `/api/v1/research-engine/protocols/4a88e789-e605-400f-ac8c-7437f8f59072/versions/d38ab741-2deb-4c29-aae2-679754b74177/approve` | 403 | error=Protocol authors cannot self-approve |
| B1-03-foreign-approve | foreign | POST | `/api/v1/research-engine/protocols/4a88e789-e605-400f-ac8c-7437f8f59072/versions/d38ab741-2deb-4c29-aae2-679754b74177/approve` | 404 | error=Project not found |
| B1-04-approver-approve-v1 | approver | POST | `/api/v1/research-engine/protocols/4a88e789-e605-400f-ac8c-7437f8f59072/versions/d38ab741-2deb-4c29-aae2-679754b74177/approve` | 200 |  |
| B2-00-owner-start-run-no-version | owner | POST | `/api/v1/research-engine/blueprints/fab71505-9dd2-4b00-b186-9f843c2b9ccf/runs` | 409 | error=approved_protocol_required |
| B2-01-owner-start-run | owner | POST | `/api/v1/research-engine/blueprints/fab71505-9dd2-4b00-b186-9f843c2b9ccf/runs` | 201 | id=a90c3860-2ed5-4e98-9a8c-390516f16fd3; status=pending |
| B2-02-owner-get-run | owner | GET | `/api/v1/research-engine/runs/a90c3860-2ed5-4e98-9a8c-390516f16fd3` | 200 | id=a90c3860-2ed5-4e98-9a8c-390516f16fd3; status=running |
| B2-03-owner-stream-run | owner | GET | `/api/v1/research-engine/runs/a90c3860-2ed5-4e98-9a8c-390516f16fd3/stream` | 409 | error=Run is in 'running' state and cannot be streamed |
| B2-04-owner-pause-run | owner | POST | `/api/v1/research-engine/runs/a90c3860-2ed5-4e98-9a8c-390516f16fd3/pause` | 200 | id=a90c3860-2ed5-4e98-9a8c-390516f16fd3; status=running |
| B2-05-owner-run-steps | owner | GET | `/api/v1/research-engine/runs/a90c3860-2ed5-4e98-9a8c-390516f16fd3/steps` | 200 | items=0 |
| B2-06-owner-run-manifest | owner | GET | `/api/v1/research-engine/runs/a90c3860-2ed5-4e98-9a8c-390516f16fd3/manifest` | 200 |  |
| B2-07-owner-run-export-json | owner | GET | `/api/v1/research-engine/runs/a90c3860-2ed5-4e98-9a8c-390516f16fd3/export?format=json` | 409 | error={"code": "run_not_completed", "message": "Run is not completed"} |
| B3-01-owner-start-run-2 | owner | POST | `/api/v1/research-engine/blueprints/fab71505-9dd2-4b00-b186-9f843c2b9ccf/runs` | 201 | id=6bc5ccdb-1521-461d-aa44-fff52800f9db; status=pending |
| B3-02-owner-stream-run-2 | owner | GET | `/api/v1/research-engine/runs/6bc5ccdb-1521-461d-aa44-fff52800f9db/stream` | 500 | error=Internal server error during tenant validation |
| B3-03-owner-get-run-2 | owner | GET | `/api/v1/research-engine/runs/6bc5ccdb-1521-461d-aa44-fff52800f9db` | 200 | id=6bc5ccdb-1521-461d-aa44-fff52800f9db; status=running |
| B3-04-owner-run-2-manifest | owner | GET | `/api/v1/research-engine/runs/6bc5ccdb-1521-461d-aa44-fff52800f9db/manifest` | 200 |  |
| B4-01-foreign-get-run | foreign | GET | `/api/v1/research-engine/runs/a90c3860-2ed5-4e98-9a8c-390516f16fd3` | 404 | error=Project not found |
| B4-02-foreign-run-export | foreign | GET | `/api/v1/research-engine/runs/a90c3860-2ed5-4e98-9a8c-390516f16fd3/export?format=json` | 404 | error={"code": "run_not_found", "message": "Run not found"} |
| B4-03-foreign-run-manifest | foreign | GET | `/api/v1/research-engine/runs/a90c3860-2ed5-4e98-9a8c-390516f16fd3/manifest` | 404 | error=Project not found |
| B4-04-foreign-run-steps | foreign | GET | `/api/v1/research-engine/runs/a90c3860-2ed5-4e98-9a8c-390516f16fd3/steps` | 404 | error=Project not found |
| B4-05-approver-run-export | approver | GET | `/api/v1/research-engine/runs/a90c3860-2ed5-4e98-9a8c-390516f16fd3/export?format=json` | 404 | error={"code": "run_not_found", "message": "Run not found"} |
| B4-06-approver-get-run | approver | GET | `/api/v1/research-engine/runs/a90c3860-2ed5-4e98-9a8c-390516f16fd3` | 200 | id=a90c3860-2ed5-4e98-9a8c-390516f16fd3; status=running |
| C1-01-generate-draft | owner | POST | `/api/v1/projects/2f7056ed-b0fb-488a-8290-8bf1c8502652/drafts?themes=retrieval-augmented%20generation%20and%20hallucinati…` | 202 | task_id=6287c09a9769; status=pending |
| D1-01-approver-get-draft-before-member | approver | GET | `/api/v1/projects/a7460d29-feee-456b-abf2-441a8008921a/drafts/1f01336d-2c57-44cd-a4a5-2924533ab12d` | 404 | error=Project not found |
| D1-02-owner-get-project | owner | GET | `/api/v1/projects/a7460d29-feee-456b-abf2-441a8008921a` | 200 | id=a7460d29-feee-456b-abf2-441a8008921a |
| C1-02-draft-status | owner | GET | `/api/v1/projects/2f7056ed-b0fb-488a-8290-8bf1c8502652/drafts/status/6287c09a9769` | 200 | task_id=6287c09a9769; status=failed |
| C1-03-draft-reviews | owner | GET | `/api/v1/projects/2f7056ed-b0fb-488a-8290-8bf1c8502652/drafts/reviews` | 200 |  |
| C1-04-draft-current | owner | GET | `/api/v1/projects/2f7056ed-b0fb-488a-8290-8bf1c8502652/drafts/current` | 404 | error=No current draft found |
| C1-05-drafts-list | owner | GET | `/api/v1/projects/2f7056ed-b0fb-488a-8290-8bf1c8502652/drafts` | 200 |  |
| C2-01-foreign-draft-reviews | foreign | GET | `/api/v1/projects/2f7056ed-b0fb-488a-8290-8bf1c8502652/drafts/reviews` | 404 | error=Project not found |
| C2-02-approver-draft-reviews | approver | GET | `/api/v1/projects/2f7056ed-b0fb-488a-8290-8bf1c8502652/drafts/reviews` | 200 |  |
| D2-01-foreign-export-md | foreign | POST | `/api/v1/projects/a7460d29-feee-456b-abf2-441a8008921a/drafts/1f01336d-2c57-44cd-a4a5-2924533ab12d/export?format=markdown&bib_format=apa` | 404 | error=Project not found |
| D2-02-foreign-export-latex | foreign | POST | `/api/v1/projects/a7460d29-feee-456b-abf2-441a8008921a/drafts/1f01336d-2c57-44cd-a4a5-2924533ab12d/export?format=latex` | 404 | error=Project not found |
| D2-03-add-member-approver-viewer | owner | POST | `/api/v2/workspaces/d7612a30-602e-488b-b6ee-6a3bf9e9f94d/members` | 201 | id=313b6f3b-6083-4c9a-8e49-3bccb71dc28d |
| D2-04-add-member-supervisor-viewer | owner | POST | `/api/v2/workspaces/d7612a30-602e-488b-b6ee-6a3bf9e9f94d/members` | 201 | id=e259f84b-714c-4534-9e79-755e56d0e696 |
| D2-05-approver-export-md | approver | POST | `/api/v1/projects/a7460d29-feee-456b-abf2-441a8008921a/drafts/1f01336d-2c57-44cd-a4a5-2924533ab12d/export?format=markdown&bib_format=apa` | 200 | bytes=14770; sha256=9e6b5de447d8ba540b4306e0c6d46794f0f25610019370e50030dcc168faca25 |
| D2-06-approver-export-latex | approver | POST | `/api/v1/projects/a7460d29-feee-456b-abf2-441a8008921a/drafts/1f01336d-2c57-44cd-a4a5-2924533ab12d/export?format=latex` | 200 | bytes=6077; sha256=a4b2ada516a87aae55c0b3a5c47ac5734d4eb282b2e9bb83647c75541c9575b2 |
| D2-07-supervisor-export-md | supervisor | POST | `/api/v1/projects/a7460d29-feee-456b-abf2-441a8008921a/drafts/1f01336d-2c57-44cd-a4a5-2924533ab12d/export?format=markdown&bib_format=apa` | 404 | error=Project not found |
| D2-08-supervisor-export-latex | supervisor | POST | `/api/v1/projects/a7460d29-feee-456b-abf2-441a8008921a/drafts/1f01336d-2c57-44cd-a4a5-2924533ab12d/export?format=latex` | 404 | error=Project not found |
| D2-09-owner-export-latex | owner | POST | `/api/v1/projects/a7460d29-feee-456b-abf2-441a8008921a/drafts/1f01336d-2c57-44cd-a4a5-2924533ab12d/export?format=latex` | 200 | bytes=6077; sha256=a4b2ada516a87aae55c0b3a5c47ac5734d4eb282b2e9bb83647c75541c9575b2 |
| D2-10-owner-export-md | owner | POST | `/api/v1/projects/a7460d29-feee-456b-abf2-441a8008921a/drafts/1f01336d-2c57-44cd-a4a5-2924533ab12d/export?format=markdown&bib_format=apa` | 200 | bytes=14770; sha256=9e6b5de447d8ba540b4306e0c6d46794f0f25610019370e50030dcc168faca25 |
| D2-11-remove-member-approver | owner | DELETE | `/api/v2/workspaces/d7612a30-602e-488b-b6ee-6a3bf9e9f94d/members/4d2e5308-01b5-4e71-99e4-2e01295eb3a4` | 204 |  |
| D2-12-remove-member-supervisor | owner | DELETE | `/api/v2/workspaces/d7612a30-602e-488b-b6ee-6a3bf9e9f94d/members/d0a0dd30-0cba-4802-821e-b74ce646e2c0` | 204 |  |
| D2-13-approver-export-md-after-removal | approver | POST | `/api/v1/projects/a7460d29-feee-456b-abf2-441a8008921a/drafts/1f01336d-2c57-44cd-a4a5-2924533ab12d/export?format=markdown&bib_format=apa` | 404 | error=Project not found |
| C3-01-status-old-task-0929 | owner | GET | `/api/v1/projects/2f7056ed-b0fb-488a-8290-8bf1c8502652/drafts/status/212088fd5ead` | 200 | task_id=212088fd5ead; status=failed |
| C3-02-status-old-task-0930 | owner | GET | `/api/v1/projects/2f7056ed-b0fb-488a-8290-8bf1c8502652/drafts/status/ad15f6592223` | 200 | task_id=ad15f6592223; status=failed |
| C3-03-foreign-status-task | foreign | GET | `/api/v1/projects/2f7056ed-b0fb-488a-8290-8bf1c8502652/drafts/status/6287c09a9769` | 404 | error=Project not found |
| C4-01-generate-draft-single-doc | owner | POST | `/api/v1/projects/2f7056ed-b0fb-488a-8290-8bf1c8502652/drafts?themes=retrieval-augmented%20generation%20and%20hallucinati…` | 202 | task_id=9c5db734fc20; status=pending |
| C4-02-draft-status-single | owner | GET | `/api/v1/projects/2f7056ed-b0fb-488a-8290-8bf1c8502652/drafts/status/9c5db734fc20` | 200 | task_id=9c5db734fc20; status=completed; draft_id=a941ca8e-ef56-4a50-b58e-29324ab03bd3 |
| C4-03-draft-current | owner | GET | `/api/v1/projects/2f7056ed-b0fb-488a-8290-8bf1c8502652/drafts/current` | 200 | id=a941ca8e-ef56-4a50-b58e-29324ab03bd3; version=1 |
| C4-04-draft-reviews | owner | GET | `/api/v1/projects/2f7056ed-b0fb-488a-8290-8bf1c8502652/drafts/reviews` | 200 |  |
| D3-01-approver-export-md-eval | approver | POST | `/api/v1/projects/2f7056ed-b0fb-488a-8290-8bf1c8502652/drafts/a941ca8e-ef56-4a50-b58e-29324ab03bd3/export?format=markdown&bib_format=apa` | 200 | bytes=3895; sha256=b068881a4ce3f9054ec09e9354bb87aaa8236dbae350c5db188655c3950c8b26 |
| D3-02-approver-export-latex-eval | approver | POST | `/api/v1/projects/2f7056ed-b0fb-488a-8290-8bf1c8502652/drafts/a941ca8e-ef56-4a50-b58e-29324ab03bd3/export?format=latex` | 200 | bytes=2328; sha256=d17f461cc88c93246ad300c40de79d2b3fce418fe101dd36bb94e4f80e350ba5 |
| D3-03-foreign-export-md-eval | foreign | POST | `/api/v1/projects/2f7056ed-b0fb-488a-8290-8bf1c8502652/drafts/a941ca8e-ef56-4a50-b58e-29324ab03bd3/export?format=markdown&bib_format=apa` | 404 | error=Project not found |
| D3-04-supervisor-xorg-export-md-eval | supervisor | POST | `/api/v1/projects/2f7056ed-b0fb-488a-8290-8bf1c8502652/drafts/a941ca8e-ef56-4a50-b58e-29324ab03bd3/export?format=markdown&bib_format=apa` | 404 | error=Project not found |
| E1-01-import-partial-ris | owner | POST | `/api/v1/research-engine/projects/2f7056ed-b0fb-488a-8290-8bf1c8502652/imports` | 201 | id=add02549-aa45-481b-8750-0f840ecacc53; version=1 |
| E1-02-import-partial-ris-replay | owner | POST | `/api/v1/research-engine/projects/2f7056ed-b0fb-488a-8290-8bf1c8502652/imports` | 200 | id=add02549-aa45-481b-8750-0f840ecacc53; version=1 |
| E1-03-import-partial-ris-changed-query | owner | POST | `/api/v1/research-engine/projects/2f7056ed-b0fb-488a-8290-8bf1c8502652/imports` | 409 | error={"code": "import_declaration_conflict", "message": "This file was already imported with a different declaration."} |
| E1-04-import-eval-ris | owner | POST | `/api/v1/research-engine/projects/2f7056ed-b0fb-488a-8290-8bf1c8502652/imports` | 201 | id=5f787db3-f77f-45b4-8f30-576b6c303a0c; version=1 |
| E1-05-foreign-import | foreign | POST | `/api/v1/research-engine/projects/2f7056ed-b0fb-488a-8290-8bf1c8502652/imports` | 404 | error=Project not found |
| E1-06-import-partial-ris-edited | owner | POST | `/api/v1/research-engine/projects/2f7056ed-b0fb-488a-8290-8bf1c8502652/imports` | 201 | id=80d0b3df-262f-4906-9c9a-ef168259b67a; version=2 |
| E1-07-list-imports | owner | GET | `/api/v1/research-engine/projects/2f7056ed-b0fb-488a-8290-8bf1c8502652/imports` | 200 | items=3 |
| E1-08-get-import-detail | owner | GET | `/api/v1/research-engine/projects/2f7056ed-b0fb-488a-8290-8bf1c8502652/imports/add02549-aa45-481b-8750-0f840ecacc53` | 200 | id=add02549-aa45-481b-8750-0f840ecacc53; version=1 |
| E2-01-reports | reviewer | GET | `/api/v1/research-engine/projects/2f7056ed-b0fb-488a-8290-8bf1c8502652/reports` | 200 | items=9 |
| E3-01-candidates | reviewer | GET | `/api/v1/research-engine/projects/2f7056ed-b0fb-488a-8290-8bf1c8502652/reports/0642117c-91a3-4ca4-ba56-9eeda4a5d48f/candidates` | 200 |  |
| E3-02-reviewer-merge-denied | reviewer | POST | `/api/v1/research-engine/projects/2f7056ed-b0fb-488a-8290-8bf1c8502652/reports/merge` | 403 | error=adjudicator role required |
| E3-03-owner-adjudicator-merge | owner | POST | `/api/v1/research-engine/projects/2f7056ed-b0fb-488a-8290-8bf1c8502652/reports/merge` | 200 | id=0642117c-91a3-4ca4-ba56-9eeda4a5d48f |
| E3-04-reviewer-propose-study-link | reviewer | POST | `/api/v1/research-engine/projects/2f7056ed-b0fb-488a-8290-8bf1c8502652/reports/5d6149b8-5d7c-402a-9b80-353bf84d9154/study-link` | 200 | id=5d6149b8-5d7c-402a-9b80-353bf84d9154 |
| E3-05-reviewer-confirm-denied | reviewer | POST | `/api/v1/research-engine/projects/2f7056ed-b0fb-488a-8290-8bf1c8502652/reports/5d6149b8-5d7c-402a-9b80-353bf84d9154/study-link` | 403 | error=adjudicator role required |
| E3-06-owner-adjudicator-confirm | owner | POST | `/api/v1/research-engine/projects/2f7056ed-b0fb-488a-8290-8bf1c8502652/reports/5d6149b8-5d7c-402a-9b80-353bf84d9154/study-link` | 200 | id=5d6149b8-5d7c-402a-9b80-353bf84d9154 |
| E3-07-owner-confirm-replay | owner | POST | `/api/v1/research-engine/projects/2f7056ed-b0fb-488a-8290-8bf1c8502652/reports/5d6149b8-5d7c-402a-9b80-353bf84d9154/study-link` | 200 | id=5d6149b8-5d7c-402a-9b80-353bf84d9154 |
| E3-08-owner-confirm-replay-changed | owner | POST | `/api/v1/research-engine/projects/2f7056ed-b0fb-488a-8290-8bf1c8502652/reports/5d6149b8-5d7c-402a-9b80-353bf84d9154/study-link` | 409 | error=Idempotency conflict |
| E3-09-foreign-report-candidates | foreign | GET | `/api/v1/research-engine/projects/2f7056ed-b0fb-488a-8290-8bf1c8502652/reports/5d6149b8-5d7c-402a-9b80-353bf84d9154/candidates` | 404 | error=Project not found |
| E3-10-reports-history | reviewer | GET | `/api/v1/research-engine/projects/2f7056ed-b0fb-488a-8290-8bf1c8502652/reports/history` | 200 | items=3 |
| E4-01-corpus-export-zip | reviewer | GET | `/api/v1/research-engine/projects/2f7056ed-b0fb-488a-8290-8bf1c8502652/corpus/export?format=zip` | 200 | bytes=5811; sha256=69b53293ba1885a6f3012feba9db957674e6024cd55586db8508cf743e79de3a |
| E4-02-corpus-export-zip-again | owner | GET | `/api/v1/research-engine/projects/2f7056ed-b0fb-488a-8290-8bf1c8502652/corpus/export?format=zip` | 200 | bytes=5812; sha256=a6f6aba5aefda666ece49ba9e62a4a40d33ad4a5b4d6f25f8cc452b7f92c77d4 |
| E4-03-foreign-corpus-export | foreign | GET | `/api/v1/research-engine/projects/2f7056ed-b0fb-488a-8290-8bf1c8502652/corpus/export?format=zip` | 404 | error=Project not found |
| E4-04-corpus-coverage | owner | POST | `/api/v1/research-engine/projects/2f7056ed-b0fb-488a-8290-8bf1c8502652/corpus/coverage` | 200 |  |
| E4-05-archived-corpus-export | approver | GET | `/api/v1/research-engine/projects/f40af11e-87a0-49a6-a715-714ef2298799/corpus/export?format=json` | 200 | body_sha256=5daac59717aabe1f1fcdada436426330d9970bc95f4252dc66e04e8bd6a2770e |
| F1-01-v1-queue-create | owner | POST | `/api/v1/research-engine/projects/2f7056ed-b0fb-488a-8290-8bf1c8502652/screening/queues` | 201 | id=c3b56c4c-6b21-40fc-bbe1-19357e91b428 |
| F1-02-assign-reviewer | owner | POST | `/api/v1/research-engine/projects/2f7056ed-b0fb-488a-8290-8bf1c8502652/screening/queues/c3b56c4c-6b21-40fc-bbe1-19357e91b428/assignments` | 200 | id=8a67c8c8-870d-40d3-98a1-d6fe2b7bc596 |
| F1-03-assign-non-reviewer-422 | owner | POST | `/api/v1/research-engine/projects/2f7056ed-b0fb-488a-8290-8bf1c8502652/screening/queues/c3b56c4c-6b21-40fc-bbe1-19357e91b428/assignments` | 422 | error=User is not an eligible reviewer |
| F1-04-reviewer-mine | reviewer | GET | `/api/v1/research-engine/projects/2f7056ed-b0fb-488a-8290-8bf1c8502652/screening/queues/c3b56c4c-6b21-40fc-bbe1-19357e91b428/mine` | 200 |  |
| F1-05-reviewer-include | reviewer | POST | `/api/v1/research-engine/projects/2f7056ed-b0fb-488a-8290-8bf1c8502652/screening/queues/c3b56c4c-6b21-40fc-bbe1-19357e91b428/observations` | 200 | id=135665f7-a2b5-4b4b-83e2-ecabfc35af7b; decision=include; resolution=single/include |
| F1-06-reviewer-include-replay | reviewer | POST | `/api/v1/research-engine/projects/2f7056ed-b0fb-488a-8290-8bf1c8502652/screening/queues/c3b56c4c-6b21-40fc-bbe1-19357e91b428/observations` | 200 | id=135665f7-a2b5-4b4b-83e2-ecabfc35af7b; decision=include; resolution=single/include |
| F1-07-reviewer-same-key-diff-decision | reviewer | POST | `/api/v1/research-engine/projects/2f7056ed-b0fb-488a-8290-8bf1c8502652/screening/queues/c3b56c4c-6b21-40fc-bbe1-19357e91b428/observations` | 409 | error=Idempotency conflict |
| F1-08-reviewer-new-key-no-supersede | reviewer | POST | `/api/v1/research-engine/projects/2f7056ed-b0fb-488a-8290-8bf1c8502652/screening/queues/c3b56c4c-6b21-40fc-bbe1-19357e91b428/observations` | 409 | error=Report resolved; reopen to change |
| F1-09-owner-submit-no-reviewer-role | owner | POST | `/api/v1/research-engine/projects/2f7056ed-b0fb-488a-8290-8bf1c8502652/screening/queues/c3b56c4c-6b21-40fc-bbe1-19357e91b428/observations` | 403 | error=reviewer role required |
| F1-10-unassigned-reviewer2-submit | reviewer2 | POST | `/api/v1/research-engine/projects/2f7056ed-b0fb-488a-8290-8bf1c8502652/screening/queues/c3b56c4c-6b21-40fc-bbe1-19357e91b428/observations` | 403 | error=Not assigned to this queue |
| F1-11-foreign-submit | foreign | POST | `/api/v1/research-engine/projects/2f7056ed-b0fb-488a-8290-8bf1c8502652/screening/queues/c3b56c4c-6b21-40fc-bbe1-19357e91b428/observations` | 404 | error=Project not found |
| F1-12-reviewer-history | reviewer | GET | `/api/v1/research-engine/projects/2f7056ed-b0fb-488a-8290-8bf1c8502652/screening/queues/c3b56c4c-6b21-40fc-bbe1-19357e91b428/history` | 200 | items=3 |
| F1-13-supervisor-history | approver | GET | `/api/v1/research-engine/projects/2f7056ed-b0fb-488a-8290-8bf1c8502652/screening/queues/c3b56c4c-6b21-40fc-bbe1-19357e91b428/history` | 200 | items=3 |
| G1-01-create-protocol-v2 | owner | POST | `/api/v1/research-engine/protocols/4a88e789-e605-400f-ac8c-7437f8f59072/versions` | 201 | id=4a88e789-e605-400f-ac8c-7437f8f59072 |
| G1-02-approver-approve-v2 | approver | POST | `/api/v1/research-engine/protocols/4a88e789-e605-400f-ac8c-7437f8f59072/versions/94d8590a-34d7-4bce-bed8-b2810b9cc2c2/approve` | 200 |  |
| G1-03-reviewer-submit-stale-v1-queue | reviewer | POST | `/api/v1/research-engine/projects/2f7056ed-b0fb-488a-8290-8bf1c8502652/screening/queues/c3b56c4c-6b21-40fc-bbe1-19357e91b428/observations` | 409 | error=Protocol version changed; reconcile queue |
| G1-04-reviewer-mine-stale | reviewer | GET | `/api/v1/research-engine/projects/2f7056ed-b0fb-488a-8290-8bf1c8502652/screening/queues/c3b56c4c-6b21-40fc-bbe1-19357e91b428/mine` | 200 |  |
| H1-01-dual-queue-create | owner | POST | `/api/v1/research-engine/projects/2f7056ed-b0fb-488a-8290-8bf1c8502652/screening/queues` | 201 | id=55fe3b95-e69b-4bd2-a3de-b59c251b0737 |
| H1-02-assign-reviewer | owner | POST | `/api/v1/research-engine/projects/2f7056ed-b0fb-488a-8290-8bf1c8502652/screening/queues/55fe3b95-e69b-4bd2-a3de-b59c251b0737/assignments` | 200 | id=7982f1f4-a850-4004-beeb-62eee0fac798 |
| H1-03-assign-reviewer2 | owner | POST | `/api/v1/research-engine/projects/2f7056ed-b0fb-488a-8290-8bf1c8502652/screening/queues/55fe3b95-e69b-4bd2-a3de-b59c251b0737/assignments` | 200 | id=6cff85b4-9e9e-4af2-b0a9-b6c5c5172a05 |
| H2-01-reviewer-include-r1-note | reviewer | POST | `/api/v1/research-engine/projects/2f7056ed-b0fb-488a-8290-8bf1c8502652/screening/queues/55fe3b95-e69b-4bd2-a3de-b59c251b0737/observations` | 200 | id=b6e995bc-e559-4ba3-a1c6-d125c1450e58; decision=include |
| H2-02-reviewer2-mine-before-reveal | reviewer2 | GET | `/api/v1/research-engine/projects/2f7056ed-b0fb-488a-8290-8bf1c8502652/screening/queues/55fe3b95-e69b-4bd2-a3de-b59c251b0737/mine` | 200 |  |
| H2-03-reviewer2-history-before-reveal | reviewer2 | GET | `/api/v1/research-engine/projects/2f7056ed-b0fb-488a-8290-8bf1c8502652/screening/queues/55fe3b95-e69b-4bd2-a3de-b59c251b0737/history` | 200 | items=4 |
| H3-01-reviewer2-exclude-r1-reveal | reviewer2 | POST | `/api/v1/research-engine/projects/2f7056ed-b0fb-488a-8290-8bf1c8502652/screening/queues/55fe3b95-e69b-4bd2-a3de-b59c251b0737/observations` | 200 | id=e2ccc9d5-0e40-4645-9031-43c9dfa482b7; decision=exclude; resolution=conflict/None |
| H3-02-reviewer-resubmit-r1-after-resolve | reviewer | POST | `/api/v1/research-engine/projects/2f7056ed-b0fb-488a-8290-8bf1c8502652/screening/queues/55fe3b95-e69b-4bd2-a3de-b59c251b0737/observations` | 409 | error=Report resolved; reopen to change |
| H3-03-reviewer2-mine-after-reveal | reviewer2 | GET | `/api/v1/research-engine/projects/2f7056ed-b0fb-488a-8290-8bf1c8502652/screening/queues/55fe3b95-e69b-4bd2-a3de-b59c251b0737/mine` | 200 |  |
| H4-01-reviewer-include-r2 | reviewer | POST | `/api/v1/research-engine/projects/2f7056ed-b0fb-488a-8290-8bf1c8502652/screening/queues/55fe3b95-e69b-4bd2-a3de-b59c251b0737/observations` | 200 | id=e685bc57-d866-409d-b535-fa272ed034fc; decision=include |
| H4-02-reviewer2-include-r2 | reviewer2 | POST | `/api/v1/research-engine/projects/2f7056ed-b0fb-488a-8290-8bf1c8502652/screening/queues/55fe3b95-e69b-4bd2-a3de-b59c251b0737/observations` | 200 | id=3294fbe6-f4f4-4ddd-b918-4001278b63e4; decision=include; resolution=agreement/include |
| H4-01-reviewer-include-r3 | reviewer | POST | `/api/v1/research-engine/projects/2f7056ed-b0fb-488a-8290-8bf1c8502652/screening/queues/55fe3b95-e69b-4bd2-a3de-b59c251b0737/observations` | 200 | id=0a4518e9-9464-42dc-b49d-f8e22c4b13a9; decision=include |
| H4-02-reviewer2-include-r3 | reviewer2 | POST | `/api/v1/research-engine/projects/2f7056ed-b0fb-488a-8290-8bf1c8502652/screening/queues/55fe3b95-e69b-4bd2-a3de-b59c251b0737/observations` | 200 | id=614522a0-5b35-4f0f-ae26-aef7bc2b756d; decision=include; resolution=agreement/include |
| H4-01-reviewer-include-r4 | reviewer | POST | `/api/v1/research-engine/projects/2f7056ed-b0fb-488a-8290-8bf1c8502652/screening/queues/55fe3b95-e69b-4bd2-a3de-b59c251b0737/observations` | 200 | id=f32c09fb-fb6f-4abf-99d4-10f541bd9e17; decision=include |
| H4-02-reviewer2-include-r4 | reviewer2 | POST | `/api/v1/research-engine/projects/2f7056ed-b0fb-488a-8290-8bf1c8502652/screening/queues/55fe3b95-e69b-4bd2-a3de-b59c251b0737/observations` | 200 | id=26ea3684-27de-4ce7-949c-8cc4dc5079cf; decision=include; resolution=agreement/include |
| H5-01-owner-conflicts | owner | GET | `/api/v1/research-engine/projects/2f7056ed-b0fb-488a-8290-8bf1c8502652/screening/queues/55fe3b95-e69b-4bd2-a3de-b59c251b0737/conflicts` | 200 | items=1 |
| H5-02-reviewer-conflicts-denied | reviewer | GET | `/api/v1/research-engine/projects/2f7056ed-b0fb-488a-8290-8bf1c8502652/screening/queues/55fe3b95-e69b-4bd2-a3de-b59c251b0737/conflicts` | 403 | error=adjudicator role required |
| H5-03-foreign-conflicts | foreign | GET | `/api/v1/research-engine/projects/2f7056ed-b0fb-488a-8290-8bf1c8502652/screening/queues/55fe3b95-e69b-4bd2-a3de-b59c251b0737/conflicts` | 404 | error=Project not found |
| H6-01-approver-adjudicate-denied | approver | POST | `/api/v1/research-engine/projects/2f7056ed-b0fb-488a-8290-8bf1c8502652/screening/queues/55fe3b95-e69b-4bd2-a3de-b59c251b0…` | 403 | error=adjudicator role required |
| H6-02-owner-adjudicate-r1-exclude | owner | POST | `/api/v1/research-engine/projects/2f7056ed-b0fb-488a-8290-8bf1c8502652/screening/queues/55fe3b95-e69b-4bd2-a3de-b59c251b0…` | 200 | id=8203f41f-e1c9-486b-b8c2-ad5cb849390e; basis=adjudicated; outcome=exclude |
| H7-00-reviewer-mine | reviewer | GET | `/api/v1/research-engine/projects/2f7056ed-b0fb-488a-8290-8bf1c8502652/screening/queues/55fe3b95-e69b-4bd2-a3de-b59c251b0737/mine` | 200 |  |
| H7-01-owner-reopen-r4 | owner | POST | `/api/v1/research-engine/projects/2f7056ed-b0fb-488a-8290-8bf1c8502652/screening/queues/55fe3b95-e69b-4bd2-a3de-b59c251b0…` | 200 | id=456df32c-565d-4517-b840-1a78d1653983; basis=reopened |
| H7-02-reviewer-fresh-r4-include | reviewer | POST | `/api/v1/research-engine/projects/2f7056ed-b0fb-488a-8290-8bf1c8502652/screening/queues/55fe3b95-e69b-4bd2-a3de-b59c251b0737/observations` | 409 | error=Observation exists; supersede the current observation |
| H7-02b-reviewer-fresh-r4-include-supersede | reviewer | POST | `/api/v1/research-engine/projects/2f7056ed-b0fb-488a-8290-8bf1c8502652/screening/queues/55fe3b95-e69b-4bd2-a3de-b59c251b0737/observations` | 200 | id=5d9dac48-b5a1-42c0-8c5c-a9c0489f2b85; decision=include |
| H7-03-reviewer2-mine | reviewer2 | GET | `/api/v1/research-engine/projects/2f7056ed-b0fb-488a-8290-8bf1c8502652/screening/queues/55fe3b95-e69b-4bd2-a3de-b59c251b0737/mine` | 200 |  |
| H7-04-reviewer2-fresh-r4-exclude | reviewer2 | POST | `/api/v1/research-engine/projects/2f7056ed-b0fb-488a-8290-8bf1c8502652/screening/queues/55fe3b95-e69b-4bd2-a3de-b59c251b0737/observations` | 200 | id=50064897-6343-429a-96ba-aa6f38b478fc; decision=exclude; resolution=conflict/None |
| H7-05-owner-adjudicate-stale-inputs | owner | POST | `/api/v1/research-engine/projects/2f7056ed-b0fb-488a-8290-8bf1c8502652/screening/queues/55fe3b95-e69b-4bd2-a3de-b59c251b0…` | 409 | error=Adjudication inputs are stale |
| H7-06-owner-adjudicate-r4-current | owner | POST | `/api/v1/research-engine/projects/2f7056ed-b0fb-488a-8290-8bf1c8502652/screening/queues/55fe3b95-e69b-4bd2-a3de-b59c251b0…` | 200 | id=83b3718e-8cf4-4ce8-9a9c-91b9a624e8cc; basis=adjudicated; outcome=include |
| H8-01-supervisor-history | owner | GET | `/api/v1/research-engine/projects/2f7056ed-b0fb-488a-8290-8bf1c8502652/screening/queues/55fe3b95-e69b-4bd2-a3de-b59c251b0737/history` | 200 | items=16 |
| I0-01-prisma-before | owner | GET | `/api/v1/research-engine/projects/2f7056ed-b0fb-488a-8290-8bf1c8502652/prisma` | 200 | body_sha256=ea59accb657875b2bb81ad34082eb9790a54529abf4d7968e07d56a7d5f90039 |
| I1-01-request-fulltext-r2 | owner | POST | `/api/v1/research-engine/projects/2f7056ed-b0fb-488a-8290-8bf1c8502652/fulltext/requests` | 201 | state=pending; request_id=8f03b28f-a714-4269-a907-234f8dabfaf8 |
| I1-01-request-fulltext-r3 | owner | POST | `/api/v1/research-engine/projects/2f7056ed-b0fb-488a-8290-8bf1c8502652/fulltext/requests` | 201 | state=pending; request_id=cdd09024-31e8-49e7-afca-f1305029187f |
| I1-01-request-fulltext-r4 | owner | POST | `/api/v1/research-engine/projects/2f7056ed-b0fb-488a-8290-8bf1c8502652/fulltext/requests` | 201 | state=pending; request_id=a7832d2f-7d22-41eb-8d23-8024ffbf4c74 |
| I1-02-request-replay-r2 | owner | POST | `/api/v1/research-engine/projects/2f7056ed-b0fb-488a-8290-8bf1c8502652/fulltext/requests` | 200 | state=pending; request_id=8f03b28f-a714-4269-a907-234f8dabfaf8 |
| I1-03-request-new-key-r2 | owner | POST | `/api/v1/research-engine/projects/2f7056ed-b0fb-488a-8290-8bf1c8502652/fulltext/requests` | 409 | error=Full text already requested |
| I1-04-request-excluded-r1 | owner | POST | `/api/v1/research-engine/projects/2f7056ed-b0fb-488a-8290-8bf1c8502652/fulltext/requests` | 201 | state=pending; request_id=1449a08c-25a2-44de-99ce-ba403345a9ad |
| I1-05-foreign-request | foreign | POST | `/api/v1/research-engine/projects/2f7056ed-b0fb-488a-8290-8bf1c8502652/fulltext/requests` | 404 | error=Project not found |
| I2-01-r3-unavailable | owner | POST | `/api/v1/research-engine/projects/2f7056ed-b0fb-488a-8290-8bf1c8502652/fulltext/requests/cdd09024-31e8-49e7-afca-f1305029187f/attempts` | 201 | state=unavailable; request_id=cdd09024-31e8-49e7-afca-f1305029187f |
| I2-02-r2-retrieved | owner | POST | `/api/v1/research-engine/projects/2f7056ed-b0fb-488a-8290-8bf1c8502652/fulltext/requests/8f03b28f-a714-4269-a907-234f8dabfaf8/attempts` | 201 | state=retrieved; request_id=8f03b28f-a714-4269-a907-234f8dabfaf8 |
| I2-03-r2-retrieved-replay | owner | POST | `/api/v1/research-engine/projects/2f7056ed-b0fb-488a-8290-8bf1c8502652/fulltext/requests/8f03b28f-a714-4269-a907-234f8dabfaf8/attempts` | 200 | state=retrieved; request_id=8f03b28f-a714-4269-a907-234f8dabfaf8 |
| I2-04-r2-stale-previous-attempt | owner | POST | `/api/v1/research-engine/projects/2f7056ed-b0fb-488a-8290-8bf1c8502652/fulltext/requests/8f03b28f-a714-4269-a907-234f8dabfaf8/attempts` | 409 | error=Full text already retrieved |
| I2-05-r4-unknown-document | owner | POST | `/api/v1/research-engine/projects/2f7056ed-b0fb-488a-8290-8bf1c8502652/fulltext/requests/a7832d2f-7d22-41eb-8d23-8024ffbf4c74/attempts` | 404 | error=Document not found |
| I2-06-r4-retrieved | owner | POST | `/api/v1/research-engine/projects/2f7056ed-b0fb-488a-8290-8bf1c8502652/fulltext/requests/a7832d2f-7d22-41eb-8d23-8024ffbf4c74/attempts` | 201 | state=retrieved; request_id=a7832d2f-7d22-41eb-8d23-8024ffbf4c74 |
| I2-07-document-A | owner | GET | `/api/v1/documents/23ade6c7-10f1-4c95-9e4a-4bccff6ece2c` | 200 | id=23ade6c7-10f1-4c95-9e4a-4bccff6ece2c |
| I2-08-list-fulltext | reviewer | GET | `/api/v1/research-engine/projects/2f7056ed-b0fb-488a-8290-8bf1c8502652/fulltext` | 200 | items=4 |
| I2-09-file-metadata-docA | owner | GET | `/api/v1/files/23ade6c7-10f1-4c95-9e4a-4bccff6ece2c/metadata` | 200 |  |
| I2-10-file-download-docA | owner | GET | `/api/v1/files/23ade6c7-10f1-4c95-9e4a-4bccff6ece2c/download` | 200 | bytes=1750; sha256=78ae5bb1f37b0919869b93c4e7c7f73803b52a85773e806b36fca34a9e822981 |
| J1-01-fulltext-queue-create | owner | POST | `/api/v1/research-engine/projects/2f7056ed-b0fb-488a-8290-8bf1c8502652/screening/queues` | 201 | id=49931aa4-caf2-4013-b0bb-9806b039a615 |
| J1-02-assign-reviewer | owner | POST | `/api/v1/research-engine/projects/2f7056ed-b0fb-488a-8290-8bf1c8502652/screening/queues/49931aa4-caf2-4013-b0bb-9806b039a615/assignments` | 200 | id=e5cb08b8-f647-463a-9785-ac1293cc7f87 |
| J1-03-assign-reviewer2 | owner | POST | `/api/v1/research-engine/projects/2f7056ed-b0fb-488a-8290-8bf1c8502652/screening/queues/49931aa4-caf2-4013-b0bb-9806b039a615/assignments` | 200 | id=b9a9fe6a-fd0f-49c2-9648-b458d5dfa4a3 |
| J2-01-r3-not-retrieved | reviewer | POST | `/api/v1/research-engine/projects/2f7056ed-b0fb-488a-8290-8bf1c8502652/screening/queues/49931aa4-caf2-4013-b0bb-9806b039a615/observations` | 409 | error=Full text not retrieved |
| J2-02-r2-exclude-no-reason | reviewer | POST | `/api/v1/research-engine/projects/2f7056ed-b0fb-488a-8290-8bf1c8502652/screening/queues/49931aa4-caf2-4013-b0bb-9806b039a615/observations` | 422 | error=Full-text exclusion needs a protocol exclusion reason |
| J2-03-r2-exclude-unlisted-reason | reviewer | POST | `/api/v1/research-engine/projects/2f7056ed-b0fb-488a-8290-8bf1c8502652/screening/queues/49931aa4-caf2-4013-b0bb-9806b039a615/observations` | 422 | error=Full-text exclusion needs a protocol exclusion reason |
| J2-04-r2-reviewer-exclude-listed | reviewer | POST | `/api/v1/research-engine/projects/2f7056ed-b0fb-488a-8290-8bf1c8502652/screening/queues/49931aa4-caf2-4013-b0bb-9806b039a615/observations` | 200 | id=eeaa996e-60e2-4e87-b2c8-c8acd1f95f98; decision=exclude |
| J2-05-r2-reviewer2-include | reviewer2 | POST | `/api/v1/research-engine/projects/2f7056ed-b0fb-488a-8290-8bf1c8502652/screening/queues/49931aa4-caf2-4013-b0bb-9806b039a615/observations` | 200 | id=e60450df-6a78-4f58-af3f-f97cbffb834a; decision=include; resolution=conflict/None |
| J2-06-owner-adjudicate-r2-exclude | owner | POST | `/api/v1/research-engine/projects/2f7056ed-b0fb-488a-8290-8bf1c8502652/screening/queues/49931aa4-caf2-4013-b0bb-9806b039a…` | 200 | id=d806145f-8d2d-4b06-8fcb-29dd9d42745a; basis=adjudicated; outcome=exclude |
| J2-07-r4-reviewer-include | reviewer | POST | `/api/v1/research-engine/projects/2f7056ed-b0fb-488a-8290-8bf1c8502652/screening/queues/49931aa4-caf2-4013-b0bb-9806b039a615/observations` | 200 | id=a38e776b-b969-4782-808e-e99e7ddab02b; decision=include |
| J2-08-r4-reviewer2-include | reviewer2 | POST | `/api/v1/research-engine/projects/2f7056ed-b0fb-488a-8290-8bf1c8502652/screening/queues/49931aa4-caf2-4013-b0bb-9806b039a615/observations` | 200 | id=75f752b5-3428-47bf-9568-4d440397fe10; decision=include; resolution=agreement/include |
| K1-01-prisma-export-json | owner | GET | `/api/v1/research-engine/projects/2f7056ed-b0fb-488a-8290-8bf1c8502652/prisma/export?format=json` | 200 | body_sha256=9a5ada19709e5b6501dfc8fa15a6b792612320f19a6fec2c55e305db27e0cb04 |
| K1-02-prisma-export-json-again | reviewer | GET | `/api/v1/research-engine/projects/2f7056ed-b0fb-488a-8290-8bf1c8502652/prisma/export?format=json` | 200 | body_sha256=9a5ada19709e5b6501dfc8fa15a6b792612320f19a6fec2c55e305db27e0cb04 |
| K1-03-prisma-export-md | owner | GET | `/api/v1/research-engine/projects/2f7056ed-b0fb-488a-8290-8bf1c8502652/prisma/export?format=md` | 200 | bytes=924; sha256=406c4e9c71894bf9d43dba6e48d759e8f50f1a3793fb1a24c0530a337542977c |
| K2-01-replay-r2-retrieved | owner | POST | `/api/v1/research-engine/projects/2f7056ed-b0fb-488a-8290-8bf1c8502652/fulltext/requests/8f03b28f-a714-4269-a907-234f8dabfaf8/attempts` | 200 | state=retrieved; request_id=8f03b28f-a714-4269-a907-234f8dabfaf8 |
| K2-02-prisma-after-replay | owner | GET | `/api/v1/research-engine/projects/2f7056ed-b0fb-488a-8290-8bf1c8502652/prisma/export?format=json` | 200 | body_sha256=9a5ada19709e5b6501dfc8fa15a6b792612320f19a6fec2c55e305db27e0cb04 |
| K3-01-r1-unavailable | owner | POST | `/api/v1/research-engine/projects/2f7056ed-b0fb-488a-8290-8bf1c8502652/fulltext/requests/1449a08c-25a2-44de-99ce-ba403345a9ad/attempts` | 201 | state=unavailable; request_id=1449a08c-25a2-44de-99ce-ba403345a9ad |
| K3-02-prisma-after-new-attempt | owner | GET | `/api/v1/research-engine/projects/2f7056ed-b0fb-488a-8290-8bf1c8502652/prisma/export?format=json` | 200 | body_sha256=292967034a0fad6205b4ad3875c8a08c1e1aaeb01bdfdfbb85907640fce96116 |
| K4-01-foreign-prisma | foreign | GET | `/api/v1/research-engine/projects/2f7056ed-b0fb-488a-8290-8bf1c8502652/prisma` | 404 | error=Project not found |
| K4-02-archived-prisma | approver | GET | `/api/v1/research-engine/projects/f40af11e-87a0-49a6-a715-714ef2298799/prisma` | 200 | body_sha256=144d10da50e8ab0fc9359b9ea3e9ca6052145297e5b1ae72d3c0b87f1434a94f |
| K4-03-archived-fulltext-request | owner | POST | `/api/v1/research-engine/projects/f40af11e-87a0-49a6-a715-714ef2298799/fulltext/requests` | 409 | error=Project is not writable |
| L1-01-corpus-export-after-screening | owner | GET | `/api/v1/research-engine/projects/2f7056ed-b0fb-488a-8290-8bf1c8502652/corpus/export?format=zip` | 200 | bytes=5838; sha256=87a1a2311ba997fe0c0a7b4cfaa935c4b5caf1eca308dd684a921bbe015f1d4b |
| M1-01-r3-retry-stale-previous | owner | POST | `/api/v1/research-engine/projects/2f7056ed-b0fb-488a-8290-8bf1c8502652/fulltext/requests/cdd09024-31e8-49e7-afca-f1305029187f/attempts` | 409 | error=Attempt is stale; reload acquisition state |
| M1-02-r3-retry-no-previous | owner | POST | `/api/v1/research-engine/projects/2f7056ed-b0fb-488a-8290-8bf1c8502652/fulltext/requests/cdd09024-31e8-49e7-afca-f1305029187f/attempts` | 409 | error=Attempt is stale; reload acquisition state |
| M1-03-r3-retrieved-after-unavailable | owner | POST | `/api/v1/research-engine/projects/2f7056ed-b0fb-488a-8290-8bf1c8502652/fulltext/requests/cdd09024-31e8-49e7-afca-f1305029187f/attempts` | 422 | error=Document has no content hash yet |
| M1-04-list-fulltext | owner | GET | `/api/v1/research-engine/projects/2f7056ed-b0fb-488a-8290-8bf1c8502652/fulltext` | 200 | items=4 |
| M1-05-prisma-final | owner | GET | `/api/v1/research-engine/projects/2f7056ed-b0fb-488a-8290-8bf1c8502652/prisma/export?format=json` | 200 | body_sha256=292967034a0fad6205b4ad3875c8a08c1e1aaeb01bdfdfbb85907640fce96116 |
| M1-06-prisma-final-md | owner | GET | `/api/v1/research-engine/projects/2f7056ed-b0fb-488a-8290-8bf1c8502652/prisma/export?format=md` | 200 | bytes=924; sha256=27137ecaa641715b5091c8cafc8037d4c06058779ba7edfa187d391290bce4d2 |
| M2-01-r3-retrieved-doc-03be7c34 | owner | POST | `/api/v1/research-engine/projects/2f7056ed-b0fb-488a-8290-8bf1c8502652/fulltext/requests/cdd09024-31e8-49e7-afca-f1305029187f/attempts` | 422 | error=Document has no content hash yet |
| M2-02-r3-retrieved-doc-6a9b211c | owner | POST | `/api/v1/research-engine/projects/2f7056ed-b0fb-488a-8290-8bf1c8502652/fulltext/requests/cdd09024-31e8-49e7-afca-f1305029187f/attempts` | 201 | request_id=cdd09024-31e8-49e7-afca-f1305029187f; state=retrieved |
| M2-10-prisma-final | owner | GET | `/api/v1/research-engine/projects/2f7056ed-b0fb-488a-8290-8bf1c8502652/prisma/export?format=json` | 200 | body_sha256=2fa99f5b4af52110821ceef0145d4c99f68ba2317150c4bd3ee52bc7936d2801 |
| M2-11-list-fulltext-final | reviewer | GET | `/api/v1/research-engine/projects/2f7056ed-b0fb-488a-8290-8bf1c8502652/fulltext` | 200 | items=4 |
| M2-12-prisma-final-md | owner | GET | `/api/v1/research-engine/projects/2f7056ed-b0fb-488a-8290-8bf1c8502652/prisma/export?format=md` | 200 | bytes=924; sha256=b3821ed10c57f8c4871722022f8f169e3c96b97c88d9ae47c7990184bc97d108 |

Notes:

- `B2-03-owner-stream-run` is the second stream call on run `a90c3860…` (409, run already `running`). The first stream call on that run returned HTTP 500 but was made by a helper that raised before saving the body; `B3-02` reproduces it on a fresh run and records the body.
- Downloaded artifacts (draft Markdown/ZIP, corpus ZIPs, file bytes) are recorded as `response_bytes` + `response_sha256`; small text bodies are inlined as `response_text`.

Per-ticket write-ups: `../2026-09-30b-goo-294-roles.md`, `../2026-09-30b-goo-290-run-provenance.md`, `../2026-09-30b-goo-291-downloads.md`, `../2026-09-30b-goo-292-review.md`, `../2026-09-30b-goo-297-task-results.md`, `../2026-09-30b-goo-299-identity.md`, `../2026-09-30b-goo-300-imports.md`, `../2026-09-30b-goo-301-302-screening.md`, `../2026-09-30b-goo-303-acquisition-prisma.md`. Defects: `../2026-09-30b-dev-defects.md`.
