# =============================================================================
# Monitoring and Observability Configuration
# =============================================================================

# =============================================================================
# CloudWatch Log Groups
# =============================================================================

resource "aws_cloudwatch_log_group" "application_logs" {
  name              = "/aws/eks/${var.project_name}/application"
  retention_in_days = var.log_retention_days

  tags = {
    Name = "${var.project_name}-application-logs"
  }
}

resource "aws_cloudwatch_log_group" "system_logs" {
  name              = "/aws/eks/${var.project_name}/system"
  retention_in_days = var.log_retention_days

  tags = {
    Name = "${var.project_name}-system-logs"
  }
}

resource "aws_cloudwatch_log_group" "audit_logs" {
  name              = "/aws/eks/${var.project_name}/audit"
  retention_in_days = var.log_retention_days

  tags = {
    Name = "${var.project_name}-audit-logs"
  }
}

# =============================================================================
# CloudWatch Dashboards
# =============================================================================

resource "aws_cloudwatch_dashboard" "main" {
  dashboard_name = "${var.project_name}-dashboard"

  dashboard_body = jsonencode({
    widgets = [
      {
        type   = "metric"
        x      = 0
        y      = 0
        width  = 12
        height = 6

        properties = {
          metrics = [
            ["AWS/EKS", "ClusterResourceCount", "ClusterName", module.eks.cluster_name],
            [".", "NodeCount", "ClusterName", module.eks.cluster_name],
            [".", "RunningPodCount", "ClusterName", module.eks.cluster_name]
          ]
          view    = "timeSeries"
          stacked = false
          region  = var.aws_region
          title   = "EKS Cluster Metrics"
          period  = 300
        }
      },
      {
        type   = "metric"
        x      = 0
        y      = 6
        width  = 12
        height = 6

        properties = {
          metrics = [
            ["AWS/RDS", "CPUUtilization", "DBInstanceIdentifier", module.rds.db_instance_identifier],
            [".", "DatabaseConnections", "DBInstanceIdentifier", module.rds.db_instance_identifier],
            [".", "FreeStorageSpace", "DBInstanceIdentifier", module.rds.db_instance_identifier]
          ]
          view    = "timeSeries"
          stacked = false
          region  = var.aws_region
          title   = "RDS Database Metrics"
          period  = 300
        }
      },
      {
        type   = "metric"
        x      = 0
        y      = 12
        width  = 12
        height = 6

        properties = {
          metrics = [
            ["AWS/ElastiCache", "CurrItems", "ReplicationGroupId", module.elasticache.replication_group_id],
            [".", "BytesUsedForCache", "ReplicationGroupId", module.elasticache.replication_group_id],
            [".", "CurrConnections", "ReplicationGroupId", module.elasticache.replication_group_id]
          ]
          view    = "timeSeries"
          stacked = false
          region  = var.aws_region
          title   = "Redis Cache Metrics"
          period  = 300
        }
      },
      {
        type   = "metric"
        x      = 0
        y      = 18
        width  = 12
        height = 6

        properties = {
          metrics = [
            ["AWS/ApplicationELB", "RequestCount", "LoadBalancer", "arn:aws:elasticloadbalancing:${var.aws_region}:${data.aws_caller_identity.current.account_id}:loadbalancer/app/${var.project_name}-internal/*"],
            [".", "TargetConnectionErrorCount", ".", "."],
            [".", "HTTPCode_Target_5XX_Count", ".", "."]
          ]
          view    = "timeSeries"
          stacked = false
          region  = var.aws_region
          title   = "Load Balancer Metrics"
          period  = 300
        }
      },
      {
        type   = "log"
        x      = 12
        y      = 0
        width  = 12
        height = 12

        properties = {
          query  = "SOURCE '${aws_cloudwatch_log_group.application_logs.name}' | fields @timestamp, @message | sort @timestamp desc | limit 100"
          region = var.aws_region
          title  = "Application Logs"
          view   = "table"
        }
      },
      {
        type   = "metric"
        x      = 12
        y      = 12
        width  = 12
        height = 6

        properties = {
          metrics = [
            ["AWS/Billing", "EstimatedCharges", "Currency", "USD"],
            ["AWS/S3", "BucketSizeBytes", "BucketName", aws_s3_bucket.application_storage.bucket, "StorageType", "StandardStorage"]
          ]
          view    = "timeSeries"
          stacked = false
          region  = var.aws_region
          title   = "Cost and Storage Metrics"
          period  = 86400
        }
      }
    ]
  })
}

# =============================================================================
# CloudWatch Alarms
# =============================================================================

resource "aws_cloudwatch_metric_alarm" "cluster_cpu_high" {
  alarm_name          = "${var.project_name}-cluster-cpu-high"
  comparison_operator = "GreaterThanThreshold"
  evaluation_periods  = "2"
  metric_name         = "cluster_cpu_utilization"
  namespace           = "ContainerInsights"
  period              = "300"
  statistic           = "Average"
  threshold           = "80"
  alarm_description   = "This metric monitors cluster CPU utilization"
  alarm_actions       = [aws_sns_topic.alerts.arn]

  tags = {
    Name = "${var.project_name}-cluster-cpu-high"
  }
}

resource "aws_cloudwatch_metric_alarm" "cluster_memory_high" {
  alarm_name          = "${var.project_name}-cluster-memory-high"
  comparison_operator = "GreaterThanThreshold"
  evaluation_periods  = "2"
  metric_name         = "cluster_memory_utilization"
  namespace           = "ContainerInsights"
  period              = "300"
  statistic           = "Average"
  threshold           = "80"
  alarm_description   = "This metric monitors cluster memory utilization"
  alarm_actions       = [aws_sns_topic.alerts.arn]

  tags = {
    Name = "${var.project_name}-cluster-memory-high"
  }
}

resource "aws_cloudwatch_metric_alarm" "database_cpu_high" {
  alarm_name          = "${var.project_name}-database-cpu-high"
  comparison_operator = "GreaterThanThreshold"
  evaluation_periods  = "2"
  metric_name         = "CPUUtilization"
  namespace           = "AWS/RDS"
  period              = "300"
  statistic           = "Average"
  threshold           = "80"
  alarm_description   = "This metric monitors RDS CPU utilization"

  dimensions = {
    DBInstanceIdentifier = module.rds.db_instance_identifier
  }

  alarm_actions = [aws_sns_topic.alerts.arn]

  tags = {
    Name = "${var.project_name}-database-cpu-high"
  }
}

resource "aws_cloudwatch_metric_alarm" "database_storage_low" {
  alarm_name          = "${var.project_name}-database-storage-low"
  comparison_operator = "LessThanThreshold"
  evaluation_periods  = "1"
  metric_name         = "FreeStorageSpace"
  namespace           = "AWS/RDS"
  period              = "300"
  statistic           = "Average"
  threshold           = "10737418240" # 10GB in bytes
  alarm_description   = "This metric monitors free RDS storage space"

  dimensions = {
    DBInstanceIdentifier = module.rds.db_instance_identifier
  }

  alarm_actions = [aws_sns_topic.alerts.arn]

  tags = {
    Name = "${var.project_name}-database-storage-low"
  }
}

resource "aws_cloudwatch_metric_alarm" "redis_memory_high" {
  alarm_name          = "${var.project_name}-redis-memory-high"
  comparison_operator = "GreaterThanThreshold"
  evaluation_periods  = "2"
  metric_name         = "BytesUsedForCache"
  namespace           = "AWS/ElastiCache"
  period              = "300"
  statistic           = "Average"
  threshold           = "4294967296" # 4GB in bytes
  alarm_description   = "This metric monitors Redis memory usage"

  dimensions = {
    ReplicationGroupId = module.elasticache.replication_group_id
  }

  alarm_actions = [aws_sns_topic.alerts.arn]

  tags = {
    Name = "${var.project_name}-redis-memory-high"
  }
}

# =============================================================================
# SNS Topic for Alerts
# =============================================================================

resource "aws_sns_topic" "alerts" {
  name = "${var.project_name}-alerts"

  tags = {
    Name = "${var.project_name}-alerts"
  }
}

resource "aws_sns_topic_subscription" "email" {
  count     = var.enable_alerts ? 1 : 0
  topic_arn = aws_sns_topic.alerts.arn
  protocol  = "email"
  endpoint  = var.alert_email
}

resource "aws_sns_topic_subscription" "slack" {
  count     = var.enable_alerts && var.slack_webhook_url != "" ? 1 : 0
  topic_arn = aws_sns_topic.alerts.arn
  protocol  = "https"
  endpoint  = var.slack_webhook_url
}

# =============================================================================
# X-Ray Tracing
# =============================================================================

resource "aws_xray_sampling_rule" "default" {
  count = var.enable_monitoring ? 1 : 0

  rule_name      = "${var.project_name}-default"
  resource_arn   = "*"
  priority       = 100
  fixed_rate     = 0.1
  reservoir_size = 100
  host           = "*"
  service_name   = "*"
  service_type   = "*"
  http_method    = "*"
  url_path       = "*"
  version        = 1
}

# =============================================================================
# AWS Config Rules
# =============================================================================

# NOTE: aws_config_config_rule "eks_cluster_no_public_access" removed —
# source_identifier EKS_CLUSTER_NO_PUBLIC_ACCESS is not a valid AWS managed
# rule id (apply fails). Re-add with a verified identifier from:
# https://docs.aws.amazon.com/config/latest/developerguide/managed-rules-by-aws-config.html
#
# NOTE: remaining aws_config_config_rule resources (rds_encryption_enabled,
# s3_bucket_public_read_prohibited) also removed for lean dev — AWS Config
# rules require a configuration recorder + delivery channel which this stack
# does not provision. Re-add recorder + rules together if config compliance
# is wanted.

# resource "aws_config_config_rule" "rds_encryption_enabled" { ... }
# resource "aws_config_config_rule" "s3_bucket_public_read_prohibited" { ... }