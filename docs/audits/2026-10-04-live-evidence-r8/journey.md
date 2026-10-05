# R8 live journey: 2026-10-04 (local real-process run)

Times are UTC. The run started at 06:58:13 and ended at 07:39:31. Each scenario is a separate world (organization, users, workspace, collection, approved protocol, completed run with a pinned search strategy, four baseline works) so schedules cannot affect each other. Check names refer to `checks.json` (34 checks, all passed).

| step | time | what happened | observed | checks |
| --- | --- | --- | --- | --- |
| 0 | 06:58:13 | Live provider search picks two baseline fixtures; four worlds seeded; one schedule each, `M * * * *` in `Europe/London`, fires at 07:01, 07:05, 07:09, 07:11 | Parent review frozen in the main world before its fire (4 attributable exclusions); the revoke world's workspace archived | `chain.root_carries_four_decisions` |
| 1 | 06:58:14 | Private Redis, worker 1 with Crossref blocked (proxy to a closed port), beat 1 | Processes up | |
| 2 | 07:01:19 | Outage fire | `started` 07:01:19.430, `succeeded` 07:01:30.595. Crossref `failed` (`ConnectError`), PubMed and OpenAlex `ok`. `new` 20, `unknown` 4, `corrected_retracted` 0. The retracted DOI is `unknown`/`provider_failed` (publication check `failed`) | `outage.*` (5) |
| 3 | 07:01:33 | Worker 1 stopped with SIGTERM; worker 2 (normal network) and a second beat started | Both beats now tick every minute | `outage.finished_before_main_fire` |
| 4 | 07:05:19 | Main fire with two beats ticking | `started` 07:05:19.777, `succeeded` 07:05:27.299, one execution, one import receipt. `new` 27, `changed` 1, `unchanged` 1, `corrected_retracted` 1 (live Crossref correction and retraction notices), `unknown` 1 (`provider_capped`). Baseline snapshot `7e7e7da5…`, post-fire snapshot `507fda31…` | `main.*` (8) |
| 5 | about 07:05:30 | GOO-320 chain on that real delta: version 2 accepted, targeted queue assigned, reviewer resolves 29 works, accounting and exports checked, role denials | Accounting error while work open, then reconciled with five consistency checks true; exports reload identically; reviewer 403, foreign 404 | `chain.*` (10) |
| 6 | 07:09:19 | Crash fire; the harness SIGKILLs worker 2's process group in the same second the `started` attempt appears, then starts worker 3 | `started` 07:09:19.433 by pid 48682; the killed worker exits -9 | |
| 7 | 07:09:19 to 07:12:40 | Observation window of 200 s (more than three ticks) | Attempts stay `[started]`: no second run while the attempt is live | `crash.no_second_run_while_attempt_is_live` |
| 8 | 07:11:19 | Revoke fire (workspace archived at step 0) | `started` 07:11:19.454, `failed` `project_archived` 07:11:19.512; no import receipt | `revoke.*` (3) |
| 9 | 07:39:19 | The stale window (30 minutes) ends; worker 3's next tick retries | `started` 07:39:19.461 with `retry_of` the first attempt, `succeeded` 07:39:22.851 by pid 55389; one execution, one import receipt | `crash.*` (5) |
| 10 | 07:39:31 | All children stopped | Worker and beat exit codes 0, the killed worker -9, Redis 0; no process left on port 6391 | |

Ticks: beat 1 sent 41 and beat 2 sent 37 (78 in total); workers 1, 2 and 3 received 3, 15 and 60 (78). One tick was in flight when worker 2 was killed (15 received, 14 succeeded). Details in `log-summary.json`; matching log lines in `log-excerpts.txt`.
