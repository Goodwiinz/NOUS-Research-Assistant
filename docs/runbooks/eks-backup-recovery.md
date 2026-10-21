# EKS backup and isolated Neo4j recovery

This operational procedure accompanies [GOO-442](https://linear.app/goodwiinz/issue/GOO-442).
The original migration and decommission records remain historical evidence.
Backup, restore and infrastructure writes require operational authorization.
This procedure does not authorize deleting DigitalOcean resources.

## Backup prerequisite

AWS Backup needs EKS authentication mode `API` or `API_AND_CONFIG_MAP` to
create its access entry. IAM backup permission alone is insufficient.
The current Terraform EKS module v19 does not expose this setting. After
provisioning a new cluster, use EKS `DescribeCluster`, verify the account,
cluster and existing `aws-auth` mappings, then use `UpdateClusterConfig`
with `accessConfig.authenticationMode=API_AND_CONFIG_MAP`. Wait for
`DescribeUpdate` to report `Successful`; verify nodes and operator access.

Enabling API authentication cannot be reverted to `CONFIG_MAP` alone.
Keep `aws-auth` mappings; do not switch to API-only as part of this repair.
The live authentication check in
[backup.tf](../../infrastructure/terraform/backup.tf) prevents silently
enabling backup selection on an incompatible cluster. It is a guard, not
an automatic authentication migration. When upgrading the EKS module,
declare the existing authentication mode explicitly and review the plan
for access-entry or node-group replacement before applying.

Use the existing backup role with `AWSBackupServiceRolePolicyForBackup`.
AWS Backup creates its own EKS access entry and associates
`AWSBackupFullAccessPolicyForBackup`. Do not grant it cluster-admin manually.
Start an on-demand job with `StartBackupJob` using the existing vault,
cluster ARN and role. Inspect the parent **and every child** with
`DescribeBackupJob` / `ListBackupJobs` (`ByParentJobId`). A pending job,
partial backup, or completed cluster-state child alone does not prove
Neo4j protection. Match the EBS child to the live Neo4j PV's CSI volume ID.

## Verify recovery without restoring application writers

1. Record the live Neo4j image digest, PV volume ID, database node and
   relationship counts, counts by label/type, and timestamp. Keep credentials
   inside the existing container; never print the secret or customer properties.
2. Require a completed EBS child recovery point for that volume. Restore its
   snapshot to a **new** encrypted EBS volume in an availability zone with a
   ready worker node. Tag it with the verification issue and purpose.
3. Create a dedicated verification namespace with deny-all ingress/egress,
   no Service/Ingress, no application secrets, and no service-account token
   mount. Bind a static CSI PV/PVC to the **new** volume, with `Retain`
   reclaim policy. Verify the PV does not reference the live volume.
4. Run the exact Neo4j image digest against this clone. Bind listeners to
   loopback only and omit production credentials/plugins. This allows local
   `kubectl exec` verification without exposing the recovered database.
   Let Neo4j perform transaction-log recovery; do not run an offline check
   on a database that is still running.
5. Compare recovered node/relationship counts and label/type distributions
   with the timestamped baseline. If live writes occurred during backup,
   explain the difference instead of silently declaring parity.
   For an EBS clone with preserved record IDs, also hash the ordered output
   of `id(n), labels(n), properties(n)` and
   `id(r), id(startNode(r)), id(endNode(r)), type(r), properties(r)`.
   Pipe the output to `sha256sum` **inside each container**, with pipeline
   failure propagation enabled; retain only the digest. This comparison
   applies to this snapshot format, not a logical export that reassigns IDs.
6. Stop **only the verification pod**, preserving its clone PVC. Run
   `neo4j-admin database check neo4j --check-property-owners=true` on the clone
   with no database server running. Require a zero exit code and successful
   graph, property, index and count checks. Retain a bounded report without
   customer content.
7. Record parent/child job IDs, snapshot ID, clone volume ID, image digest,
   counts, consistency result and elapsed recovery time. Also require a
   completed scheduled job; an on-demand success alone does not prove the
   scheduler path. A temporary dated verification plan may test scheduling
   without changing the normal daily/weekly plan. Remove only that temporary
   plan/selection after the scheduled job is established; retain recovery points.
8. Clean up only resources created for this verification after retaining
   evidence. Stop the clone before detaching its volume. Never remove the
   source PVC, source volume or DigitalOcean resources.

This proves recovery of the tested Neo4j volume and store. It is not a
full-cluster restore, a coordinated PostgreSQL/Neo4j transaction snapshot,
or a complete application disaster-recovery exercise.

## Failure alerts and Terraform reconciliation

AWS publishes `NumberOfBackupJobsFailed` and `NumberOfBackupJobsExpired`,
not `BackupJobFailures`. The two alarms use the vault dimension and a
five-minute period. Inspect real `GetMetricStatistics` data and alarm state;
do not manufacture a successful notification test by changing alarm state.
An SNS topic with no confirmed subscribers is not end-to-end alert delivery.
Email subscriptions require the recipient to confirm the AWS message.

If alarms or the subscription were repaired via API, reconcile Terraform
before applying the stack: refresh the existing failure alarm, import the
new expiry alarm into `aws_cloudwatch_metric_alarm.backup_expired`, and set
the private deployment variables `enable_alerts=true` and `alert_email` to
the approved recipient before importing the confirmed subscription into
`aws_sns_topic_subscription.email[0]`. Review a full plan; do not apply
unrelated drift or recreate an existing subscription. Do not commit private
tfvars or state. Authentication remains an explicit bootstrap prerequisite
until the EKS module is upgraded.

## Sources

- [AWS EKS backup prerequisites and child recovery points](https://docs.aws.amazon.com/aws-backup/latest/devguide/eks-backups.html)
- [AWS EKS restore behavior](https://docs.aws.amazon.com/aws-backup/latest/devguide/restoring-eks.html)
- [AWS Backup metric names and dimensions](https://docs.aws.amazon.com/AmazonCloudWatch/latest/monitoring/SupportedMetricsForResourceTagsForTelemetry.html)
