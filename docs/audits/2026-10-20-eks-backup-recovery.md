# EKS backup repair and Neo4j recovery evidence

Date: 2026-10-20. Issue: [GOO-442](https://linear.app/goodwiinz/issue/GOO-442).
Source baseline: `421859286` on fetched `origin/develop`; changes were made
in the dedicated `codex/goo-442-eks-backup` worktree. This is a dated
operational evidence record, not a statement about future runtime health.

## Diagnosis and repair

`DescribeCluster` returned authentication mode `CONFIG_MAP`, and
`ListAccessEntries` reproduced `InvalidRequestException` requiring `API`
or `API_AND_CONFIG_MAP`. The backup role already had
`AWSBackupServiceRolePolicyForBackup`. The failed daily job
`7B57C849-90F6-91EA-934C-E5D6865696B9` had zero child jobs.

Authorized EKS update `95486f3b-8777-36ad-93c7-78ed01f858b7` completed
successfully, enabling `API_AND_CONFIG_MAP` while preserving `aws-auth`
mappings. API authentication cannot be disabled again. Nodes stayed Ready
and the application health endpoint returned healthy after the update.
AWS Backup then created its own backup-role access entry and associated
`AWSBackupFullAccessPolicyForBackup` without a manual cluster-admin grant.

The Terraform module v19 lacks an authentication-mode input. The repair
therefore records the API bootstrap operation and adds a live-mode
precondition on backup selection; no module major-version upgrade or
unrelated infrastructure apply was performed.

## Backup and restore evidence

| Evidence | Identifier/result |
| --- | --- |
| On-demand parent | `18ca34bb-3eac-4ccd-8ee3-550cf71eaac8` |
| Cluster-state child | `42DB5F3C-BFB8-C1C8-9AC6-B965295739B4`, completed |
| Neo4j EBS child | `A83E8CB3-6B4A-0D46-CC89-F3BCD657C1C3`, completed |
| Other PVC child | `003D3F44-0970-B2CD-6125-AAF5DCEB7F4B`, completed |
| Neo4j source volume | `vol-08afc9662c9442aab` |
| Neo4j snapshot | `snap-0b9f89fa5837f4879`, completed, encrypted, 5 GiB |
| Isolated restored volume | `vol-0d685567849eda548`, encrypted, us-east-1c |
| Verification namespace | `goo-442-recovery` |
| Exact image digest | `sha256:f1e52d4dc6da122468c4a3c1050a4f5e117f6d3268625237c34d49166f0d6082` |
| Live and recovered nodes | 7,407 |
| Live and recovered relationships | 19,952 (`RELATED_TO`) |
| Label distributions | Identical across all 14 labels, including `Entity` |
| Ordered record SHA-256, live and clone | `189701c7cf6a83ae90a02b1ca7febdce3daeff7d337b077d695235cab87c67fb` |
| Offline check | `neo4j-admin database check neo4j --check-property-owners=true --max-off-heap-memory=256m --threads=1 --report-path=/tmp`, pod Succeeded, exit 0 |

The clone used the actual EBS child snapshot. It had no Service/Ingress,
no application secrets, no service-account token mount, deny-all network
policy, and loopback-only database listeners. Ordered record output was
hashed inside each container; customer properties were not returned or
saved. Both pipelines propagated command failure before returning a hash.
The restored database was shut down before the offline consistency check.
The source Neo4j pod and volume were not stopped or modified.

Recovery completed within approximately three minutes of clone creation
(22:31–22:34 UTC), excluding backup duration. This is one small-store
observation, not a recovery-time SLA. It proves this Neo4j snapshot can be
recovered with matching content and a consistent store; it does not prove
full-cluster restoration or cross-database transaction consistency.

## Alerting

The existing alarm was stuck in `INSUFFICIENT_DATA`, watching the nonexistent
`BackupJobFailures` metric. It now uses `NumberOfBackupJobsFailed` and a
second alarm watches `NumberOfBackupJobsExpired`, both scoped to the backup
vault with five-minute periods. Real CloudWatch data contained historical
failure and expiry datapoints. Both corrected alarms subsequently evaluated
to `OK`. The existing SNS topic had no subscribers; an email subscription
was requested for the user-approved recipient. Confirmation and end-to-end
notification delivery remain separate checks; alarm state alone proves neither.

## Validation and scope

- `terraform init -backend=false` and `terraform validate`: passed.
- Changed backup configuration `terraform fmt -check`: passed.
- All four Helm overlays linted and rendered; network policy, image digest,
  PDB, deployment-consistency and Compose config assertions passed.
- Directory documentation lint and `git diff --check`: passed.
- No full Terraform plan/apply or state import was performed. Reconcile
  API-created alarm/subscription resources as described in the
  [runbook](../runbooks/eks-backup-recovery.md) before a stack apply.
- No DigitalOcean resources or keys were changed.

Parent aggregation, scheduled verification and temporary-resource cleanup
are recorded in the issue as they reach terminal outcomes. Do not infer
those outcomes from completed child jobs alone.
