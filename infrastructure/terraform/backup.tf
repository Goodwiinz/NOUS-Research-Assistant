# =============================================================================
# Backup and Disaster Recovery Configuration
# =============================================================================

# =============================================================================
# S3 Buckets for Backup Storage
# =============================================================================

resource "aws_s3_bucket" "backups" {
  bucket = "${var.project_name}-${var.environment}-backups-${random_string.backup_suffix.result}"

  tags = {
    Name        = "${var.project_name}-backups"
    Environment = var.environment
    Purpose     = "Backup Storage"
  }
}

resource "random_string" "backup_suffix" {
  length  = 8
  special = false
  upper   = false
}

# Backup bucket configuration
resource "aws_s3_bucket_versioning" "backups" {
  bucket = aws_s3_bucket.backups.id
  versioning_configuration {
    status = "Enabled"
  }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "backups" {
  bucket = aws_s3_bucket.backups.id

  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

resource "aws_s3_bucket_public_access_block" "backups" {
  bucket = aws_s3_bucket.backups.id

  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

# Cross-region replication for disaster recovery
resource "aws_s3_bucket" "backups_dr" {
  count    = var.enable_cross_region_backup ? 1 : 0
  bucket   = "${var.project_name}-${var.environment}-backups-dr-${random_string.backup_dr_suffix[0].result}"
  provider = aws.dr

  tags = {
    Name        = "${var.project_name}-backups-dr"
    Environment = var.environment
    Purpose     = "Disaster Recovery Backup Storage"
  }
}

resource "random_string" "backup_dr_suffix" {
  count   = var.enable_cross_region_backup ? 1 : 0
  length  = 8
  special = false
  upper   = false
}

resource "aws_s3_bucket_versioning" "backups_dr" {
  count    = var.enable_cross_region_backup ? 1 : 0
  bucket   = aws_s3_bucket.backups_dr[0].id
  provider = aws.dr

  versioning_configuration {
    status = "Enabled"
  }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "backups_dr" {
  count    = var.enable_cross_region_backup ? 1 : 0
  bucket   = aws_s3_bucket.backups_dr[0].id
  provider = aws.dr

  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

# Replication configuration
resource "aws_s3_bucket_replication_configuration" "backups_replication" {
  count  = var.enable_cross_region_backup ? 1 : 0
  role   = aws_iam_role.backup_replication[0].arn
  bucket = aws_s3_bucket.backups.id

  rule {
    id     = "backup_replication"
    status = "Enabled"

    destination {
      bucket        = aws_s3_bucket.backups_dr[0].arn
      storage_class = "STANDARD_IA"
      account       = data.aws_caller_identity.dr[0].account_id
    }

    delete_marker_replication {
      status = "Enabled"
    }
  }

  depends_on = [aws_iam_role_policy_attachment.backup_replication_policy]
}

# =============================================================================
# IAM Role for Backup Operations
# =============================================================================

resource "aws_iam_role" "backup_role" {
  name = "${var.project_name}-backup-role"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Action = "sts:AssumeRole"
        Effect = "Allow"
        Principal = {
          Service = "backup.amazonaws.com"
        }
      }
    ]
  })

  tags = {
    Name = "${var.project_name}-backup-role"
  }
}

resource "aws_iam_role_policy_attachment" "backup_role_policy" {
  policy_arn = "arn:aws:iam::aws:policy/service-role/AWSBackupServiceRolePolicyForBackup"
  role       = aws_iam_role.backup_role.name
}

# Additional policy for S3 access
resource "aws_iam_policy" "backup_s3_policy" {
  name = "${var.project_name}-backup-s3-policy"

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect = "Allow"
        Action = [
          "s3:GetBucketLocation",
          "s3:ListBucket",
          "s3:GetBucketVersioning",
          "s3:PutObject",
          "s3:GetObject",
          "s3:DeleteObject",
          "s3:ListMultipartUploadParts",
          "s3:AbortMultipartUpload"
        ]
        Resource = [
          aws_s3_bucket.backups.arn,
          "${aws_s3_bucket.backups.arn}/*"
        ]
      },
      {
        Effect = "Allow"
        Action = [
          "s3:GetBucketLocation",
          "s3:ListBucket",
          "s3:GetBucketVersioning",
          "s3:PutObject",
          "s3:GetObject",
          "s3:DeleteObject",
          "s3:ListMultipartUploadParts",
          "s3:AbortMultipartUpload"
        ]
        Resource = [
          var.enable_cross_region_backup ? aws_s3_bucket.backups_dr[0].arn : "",
          var.enable_cross_region_backup ? "${aws_s3_bucket.backups_dr[0].arn}/*" : ""
        ]
        Condition = {
          StringEquals = {
            "aws:RequestedRegion" = var.backup_region
          }
        }
      }
    ]
  })
}

resource "aws_iam_role_policy_attachment" "backup_s3_policy_attachment" {
  policy_arn = aws_iam_policy.backup_s3_policy.arn
  role       = aws_iam_role.backup_role.name
}

# Role for cross-region replication
resource "aws_iam_role" "backup_replication" {
  count = var.enable_cross_region_backup ? 1 : 0
  name  = "${var.project_name}-backup-replication-role"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Action = "sts:AssumeRole"
        Effect = "Allow"
        Principal = {
          Service = "s3.amazonaws.com"
        }
      }
    ]
  })

  tags = {
    Name = "${var.project_name}-backup-replication-role"
  }
}

resource "aws_iam_policy" "backup_replication_policy" {
  count = var.enable_cross_region_backup ? 1 : 0
  name  = "${var.project_name}-backup-replication-policy"

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect = "Allow"
        Action = [
          "s3:GetReplicationConfiguration",
          "s3:ListBucket",
          "s3:GetBucketVersioning",
          "s3:GetBucketLocation"
        ]
        Resource = [
          aws_s3_bucket.backups.arn
        ]
      },
      {
        Effect = "Allow"
        Action = [
          "s3:ReplicateObject",
          "s3:ReplicateDelete",
          "s3:ReplicateTags",
          "s3:GetBucketVersioning",
          "s3:PutBucketVersioning"
        ]
        Resource = [
          aws_s3_bucket.backups_dr[0].arn,
          "${aws_s3_bucket.backups_dr[0].arn}/*"
        ]
      }
    ]
  })
}

resource "aws_iam_role_policy_attachment" "backup_replication_policy" {
  count      = var.enable_cross_region_backup ? 1 : 0
  policy_arn = aws_iam_policy.backup_replication_policy[0].arn
  role       = aws_iam_role.backup_replication[0].name
}

# =============================================================================
# AWS Backup Configuration
# =============================================================================

resource "aws_backup_vault" "main" {
  name = "${var.project_name}-backup-vault"

  tags = {
    Name = "${var.project_name}-backup-vault"
  }
}

resource "aws_kms_key" "backup" {
  description             = "KMS key for ${var.project_name} backup encryption"
  deletion_window_in_days = 10
  enable_key_rotation     = true

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid    = "Enable IAM User Permissions"
        Effect = "Allow"
        Principal = {
          AWS = "arn:aws:iam::${data.aws_caller_identity.current.account_id}:root"
        }
        Action   = "kms:*"
        Resource = "*"
      },
      {
        Sid    = "Allow AWS Backup"
        Effect = "Allow"
        Principal = {
          Service = "backup.amazonaws.com"
        }
        Action = [
          "kms:Decrypt",
          "kms:DescribeKey",
          "kms:Encrypt",
          "kms:GenerateDataKey*",
          "kms:ReEncrypt*"
        ]
        Resource = "*"
      }
    ]
  })

  tags = {
    Name = "${var.project_name}-backup-key"
  }
}

resource "aws_kms_alias" "backup" {
  name          = "alias/${var.project_name}-backup-key"
  target_key_id = aws_kms_key.backup.key_id
}

# Backup Plans
resource "aws_backup_plan" "rds_backup" {
  name = "${var.project_name}-rds-backup-plan"

  rule {
    rule_name         = "daily_backups"
    target_vault_name = aws_backup_vault.main.name
    schedule          = "cron(0 2 ? * *)"

    lifecycle {
      delete_after = 30
    }

    recovery_point_tags = {
      Environment = var.environment
      Project     = var.project_name
      Type        = "RDS"
    }
  }

  rule {
    rule_name         = "weekly_backups"
    target_vault_name = aws_backup_vault.main.name
    schedule          = "cron(0 2 ? * 1)"

    lifecycle {
      delete_after = 90
    }

    recovery_point_tags = {
      Environment = var.environment
      Project     = var.project_name
      Type        = "RDS-Weekly"
    }
  }

  tags = {
    Name = "${var.project_name}-rds-backup-plan"
  }
}

resource "aws_backup_plan" "eks_backup" {
  name = "${var.project_name}-eks-backup-plan"

  rule {
    rule_name         = "daily_eks_backups"
    target_vault_name = aws_backup_vault.main.name
    schedule          = "cron(0 3 ? * *)"

    lifecycle {
      delete_after = 30
    }

    recovery_point_tags = {
      Environment = var.environment
      Project     = var.project_name
      Type        = "EKS"
    }
  }

  rule {
    rule_name         = "weekly_eks_backups"
    target_vault_name = aws_backup_vault.main.name
    schedule          = "cron(0 3 ? * 1)"

    lifecycle {
      delete_after = 90
    }

    recovery_point_tags = {
      Environment = var.environment
      Project     = var.project_name
      Type        = "EKS-Weekly"
    }
  }

  tags = {
    Name = "${var.project_name}-eks-backup-plan"
  }
}

# Backup Selections
resource "aws_backup_selection" "rds" {
  name         = "${var.project_name}-rds-selection"
  iam_role_arn = aws_iam_role.backup_role.arn
  plan_id      = aws_backup_plan.rds_backup.id

  resources = [
    module.rds.db_instance_arn
  ]
}

resource "aws_backup_selection" "eks" {
  name         = "${var.project_name}-eks-selection"
  iam_role_arn = aws_iam_role.backup_role.arn
  plan_id      = aws_backup_plan.eks_backup.id

  resources = [
    "arn:aws:eks:${var.aws_region}:${data.aws_caller_identity.current.account_id}:cluster/${module.eks.cluster_name}"
  ]
}

# =============================================================================
# Database Backup Configuration
# =============================================================================

# Enhanced RDS backup settings
resource "aws_db_instance_automated_backups_replication" "rds_dr_replication" {
  count                  = var.enable_cross_region_backup ? 1 : 0
  source_db_instance_arn = module.rds.db_instance_arn
  retention_period       = 7
  provider               = aws.dr
}

# =============================================================================
# EKS Backup using Velero
# =============================================================================

# Velero IAM role for EKS backups
resource "aws_iam_role" "velero" {
  name = "${var.project_name}-velero-server"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Action = "sts:AssumeRole"
        Effect = "Allow"
        Principal = {
          Service = "ec2.amazonaws.com"
        }
      }
    ]
  })

  tags = {
    Name = "${var.project_name}-velero-server"
  }
}

resource "aws_iam_role_policy" "velero" {
  name = "${var.project_name}-velero-policy"
  role = aws_iam_role.velero.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect = "Allow"
        Action = [
          "ec2:DescribeVolumes",
          "ec2:DescribeSnapshots",
          "ec2:CreateTags",
          "ec2:CreateVolume",
          "ec2:CreateSnapshot",
          "ec2:DeleteSnapshot"
        ]
        Resource = "*"
      },
      {
        Effect = "Allow"
        Action = [
          "s3:GetObject",
          "s3:DeleteObject",
          "s3:PutObject",
          "s3:AbortMultipartUpload",
          "s3:ListMultipartUploadParts"
        ]
        Resource = [
          "${aws_s3_bucket.backups.arn}/*"
        ]
      },
      {
        Effect = "Allow"
        Action = [
          "s3:ListBucket"
        ]
        Resource = [
          aws_s3_bucket.backups.arn
        ]
      },
      {
        Effect = "Allow"
        Action = [
          "iam:PassRole"
        ]
        Resource = [
          aws_iam_role.velero.arn
        ]
      }
    ]
  })
}

# aws_iam_role_policy.velero already attaches the inline policy to the role.

# =============================================================================
# Disaster Recovery Configuration
# =============================================================================

# CloudWatch alarm for backup failures
resource "aws_cloudwatch_metric_alarm" "backup_failure" {
  alarm_name          = "${var.project_name}-backup-failure"
  comparison_operator = "GreaterThanThreshold"
  evaluation_periods  = "1"
  metric_name         = "BackupJobFailures"
  namespace           = "AWS/Backup"
  period              = "86400"
  statistic           = "Sum"
  threshold           = "0"
  alarm_description   = "This metric monitors backup failures"
  alarm_actions       = [aws_sns_topic.alerts.arn]

  tags = {
    Name = "${var.project_name}-backup-failure"
  }
}

# Lambda function for backup verification
resource "aws_iam_role" "backup_verification_lambda" {
  name = "${var.project_name}-backup-verification-lambda"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Action = "sts:AssumeRole"
        Effect = "Allow"
        Principal = {
          Service = "lambda.amazonaws.com"
        }
      }
    ]
  })

  tags = {
    Name = "${var.project_name}-backup-verification-lambda"
  }
}

resource "aws_iam_role_policy_attachment" "backup_verification_lambda_logs" {
  policy_arn = "arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"
  role       = aws_iam_role.backup_verification_lambda.name
}

resource "aws_iam_role_policy" "backup_verification_lambda_policy" {
  name = "${var.project_name}-backup-verification-lambda-policy"
  role = aws_iam_role.backup_verification_lambda.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect = "Allow"
        Action = [
          "backup:ListBackupJobs",
          "backup:DescribeBackupJob",
          "backup:ListRecoveryPointsByBackupVault"
        ]
        Resource = "*"
      },
      {
        Effect = "Allow"
        Action = [
          "logs:CreateLogGroup",
          "logs:CreateLogStream",
          "logs:PutLogEvents"
        ]
        Resource = "arn:aws:logs:*:*:*"
      },
      {
        Effect = "Allow"
        Action = [
          "sns:Publish"
        ]
        Resource = aws_sns_topic.alerts.arn
      }
    ]
  })
}

# aws_iam_role_policy.backup_verification_lambda_policy already attaches
# the inline policy to the role.

# =============================================================================
# Data Source for DR Region
# =============================================================================

data "aws_caller_identity" "dr" {
  count    = var.enable_cross_region_backup ? 1 : 0
  provider = aws.dr
}

# =============================================================================
# DR Provider Configuration
# =============================================================================

provider "aws" {
  alias  = "dr"
  region = var.backup_region
}

# =============================================================================
# Outputs
# =============================================================================

output "backup_bucket_name" {
  description = "Name of the primary backup S3 bucket"
  value       = aws_s3_bucket.backups.bucket
}

output "backup_bucket_arn" {
  description = "ARN of the primary backup S3 bucket"
  value       = aws_s3_bucket.backups.arn
}

output "backup_dr_bucket_name" {
  description = "Name of the disaster recovery backup S3 bucket"
  value       = var.enable_cross_region_backup ? aws_s3_bucket.backups_dr[0].bucket : ""
}

output "backup_vault_name" {
  description = "Name of the AWS Backup vault"
  value       = aws_backup_vault.main.name
}

output "backup_vault_arn" {
  description = "ARN of the AWS Backup vault"
  value       = aws_backup_vault.main.arn
}

output "backup_kms_key_arn" {
  description = "ARN of the KMS key for backup encryption"
  value       = aws_kms_key.backup.arn
}

output "backup_role_arn" {
  description = "ARN of the backup IAM role"
  value       = aws_iam_role.backup_role.arn
}

output "velero_role_arn" {
  description = "ARN of the Velero IAM role"
  value       = aws_iam_role.velero.arn
}