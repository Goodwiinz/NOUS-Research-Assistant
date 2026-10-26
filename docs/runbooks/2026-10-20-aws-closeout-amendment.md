# AWS migration close-out amendment — 2026-10-20

Status: partial verification; migration close-out and DigitalOcean teardown remain
blocked. This dated amendment preserves the historical
[cutover procedure](2026-09-14-aws-cutover.md) and
[decommission proposal](aws-decommission.md). It does not authorize deletion.
Source baseline: `67d329594a89993a38457b6f1a62b6cc4d51db3f` from freshly fetched
`origin/develop`. Tracking: [GOO-441](https://linear.app/goodwiinz/issue/GOO-441)
and children GOO-443 through GOO-446. Observations are from October 20;
all technical schedule times below are UTC.

## RDS backup schedule repair (GOO-443)

Live RDS maintenance is Monday 03:00–04:00; automated backups are daily
04:00–06:00 with seven-day retention. AWS Backup previously started at 02:00
with a 60-minute start window. Job `7C291417-B5A0-591B-61ED-91A1E4DA1BF4`
failed October 19 because its start was inside or too close to maintenance.
[AWS documents the one-hour exclusion before maintenance](https://docs.aws.amazon.com/aws-backup/latest/devguide/plan-options-and-configuration.html).

Both AWS Backup rules now start at 08:00, with a 60-minute start window and
180-minute completion window. Daily/weekly retention remains 30/90 days;
the weekly rule remains Sunday (`1` in AWS cron). RDS settings were not
modified. API update and read-back verified plan
`786183bb-d056-4637-b7eb-53567d4a635b`, version
`NTYzYTk1ZWEtZGE0Yi00ODExLWE4OWYtYmI0YmJhOTY2NzNi`, at 22:57:58 UTC.
The matching source is [backup.tf](../../infrastructure/terraform/backup.tf).

Pending: verify the next daily execution on October 21 and the formerly
conflicting Monday execution on October 26 at 08:00–09:00 UTC. Require a
COMPLETED job with the new plan version/rule, plus retained recovery point.
Do not infer that future-window proof from an on-demand backup. The failure
and expiry alarms repaired under [GOO-442](https://linear.app/goodwiinz/issue/GOO-442)
cover the same vault; confirmed SNS delivery remains a separate acceptance gate.
No broad Terraform apply was run. Refresh state and review the plan before
applying this change alongside PR #2036's alert reconciliation.

## Data parity and recoverability (GOO-444)

- Full current S3 inventory: 1,082 objects, 1,096,959,817 bytes. Sequential
  batches returned 1,000, 82, then zero objects. Prefix counts: bedrock 294,
  documents 786, images 2. This is destination inventory, not source parity.
- Five oldest active S3-backed document references passed HeadObject and a
  16-byte ranged GetObject. No document content or object names is retained
  here. Sampling does not prove readability of every object.
- Read-only RDS queries found 189 public tables and Alembic revision
  `jd01_document_embeddings`. Document rows: S3 388 COMPLETED, 6 PENDING,
  1 PROCESSING, 7 FAILED; local 101 COMPLETED. No document file/storage path
  contained a digitaloceanspaces.com URL.
- All 101 local COMPLETED rows are active, are PDFs created April–July 2026,
  and reference filenames absent from the running backend filesystem. The
  local download path checks the stored file path directly. This establishes
  unresolved local-file references, not when or why the files disappeared.
  S3 key matching found artifacts for all 101 document UUIDs: 42 have exactly
  one PDF candidate under bedrock; the other 59 have text/metadata artifacts
  only. Across the matches: 101 documents TXT, 101 bedrock JSON, 59 bedrock
  TXT and 42 bedrock PDF objects. These are recovery candidates, not verified
  original-file parity. Preserve rows; validate ownership, original checksums
  and candidate readability before changing references. No mappings were changed.
- No `.cutover` directory was found in registered worktrees; the named source
  dumps/count files were not found in the checked primary checkout. Current
  storage inventory had no dump/manifest/cutover archive candidates. This
  bounded search does not establish that archives do not exist elsewhere.
- Neo4j snapshot restoration, matching counts/content hash and offline store
  consistency passed under GOO-442. That is recovery evidence for the tested
  snapshot, not migration-source parity or coordinated multi-database recovery.

Closure requires retained PostgreSQL/Spaces/Neo4j source evidence, accounting
for post-cutover changes, and resolution of the 101 active local-file references.

## DigitalOcean inventory and future disposition (GOO-445)

Scope: current doctl default context and matching authenticated console team.
The direct knowledge-base API returned an empty list without a next page.
The retained Spaces credentials returned no buckets and NoSuchBucket for the
configured source. The authenticated console's account-wide Spaces page also
showed an empty inventory. No source bucket contents could be compared.

The rag-system project detail page and API show no resources; the dashboard's
four-resource badge contradicts them and appears stale. Its Activity page
records two Spaces removals as “9 days ago” and the cluster, two databases,
load balancer, two volumes and two app-pool nodes as “23 days ago,” attributed
to the account owner. These are the UI's relative timestamps and “removed”
wording, not independently verified deletion timestamps or archival proof.

| Retained resource | Evidence and proposed disposition — no action authorized |
| --- | --- |
| onubuntu droplet 548108637 | Active, 8 vCPU/16 GiB; preserve as unrelated/shared compute until owner confirms dependencies |
| ragsystemregistry | Backend: 1,560 tags/630 manifests; frontend: 461 tags/155 manifests; retain until image parity and all consumers are audited |
| Two April Neo4j volume snapshots | Preserve recovery material; these predate migration and are not proof of September cutover parity |
| Reserved IP and gen-text.app domain | Preserve until ownership and DNS dependencies are confirmed |
| Three Spaces access keys | Bucket inventory is empty, but do not revoke until consumers and archive requirements are reviewed |

October invoice preview (generated October 20 05:15 UTC) totals $98.25:
droplet $76.00, registry subscription $13.57, registry storage $5.27,
unused reserved IP $3.39, snapshots $0.02. These are month-to-date account
charges, not a migration-only or final monthly bill. No resources or keys
were deleted, revoked, resized or otherwise changed in DigitalOcean.

## Release, application acceptance and cost (GOO-446)

Argo aws-dev, nous-dev-aws and nous-dev-aws-ingress are Synced/Healthy at
`67d329594a89993a38457b6f1a62b6cc4d51db3f`. The running backend digest is
`sha256:263a39bfbf68686e9f6a639fef8ccc9ec2d6da02c67cf9a92ffc77c4392a33b1`,
produced for source `7fbd027ce9f9b7a222c4038edf7ea321416a86dd` by
[Build Backend Image job 116264783148](https://github.com/Goodwiinz/NOUS-Research-Assistant/actions/runs/38687217907/job/116264783148).
A newer manifest revision does not mean a newer backend image was deployed.
Public health/readiness returned healthy/ready at 22:58 UTC.

Synthetic Job `nous-dev-aws-synthetic-traffic-29875600` recorded general_qa
flag=ok, zero errors and zero unmet expectations, with zero tool executions.
The script drives the agent in-process; it does not prove browser login,
HTTP chat streaming, upload/ingest/search, graph UI reads or worker completion.
Those controlled-account checks remain pending account selection. Argo's
94 root and 3 application/ingress orphan warnings remain unresolved; inventory
ownership must be established before any cleanup.

AWS Cost Explorer, October 1 inclusive through October 20 exclusive, reports
$412.12 estimated Usage-record UnblendedCost for the account. EKS is $273.60:
$45.60 per-cluster plus **$228.00 extended support**. Other account services
are included, so this is not a tag-isolated application bill. Credits make the
net all-record view approximately zero; that is not the infrastructure run rate.
The [original plan](../plans/2026-09-14-do-to-aws-migration-design.md) targeted
$220–275/month and assumed $73/month control-plane cost. Current two SPOT
t3.large nodes and EKS 1.31 extended support require a new cost baseline and
a separately reviewed Kubernetes upgrade plan. No cluster upgrade was attempted.

## Validation and remaining gates

Terraform validate and changed-file fmt check passed. All four Helm overlays
linted/rendered; network policy, image digest, PDB, deployment consistency and
Compose assertions passed. A fresh provider install hit disk exhaustion;
its incomplete generated cache was removed and validation reused the prior
installed provider/module cache. No Terraform state was changed.

Keep close-out open until scheduled-window proof, source evidence/file recovery,
resource ownership, controlled authenticated acceptance and final PR checks
are resolved. DigitalOcean decommission still requires explicit authorization.
