# =============================================================================
# Variables for Knowledge Graph Analytics Dashboard
# =============================================================================

variable "project_name" {
  description = "Name of the project"
  type        = string
  default     = "knowledge-graph-analytics"
}

variable "environment" {
  description = "Environment name (development, staging, production)"
  type        = string
  default     = "production"

  validation {
    condition     = contains(["development", "staging", "production"], var.environment)
    error_message = "The environment must be one of: development, staging, production."
  }
}

variable "aws_region" {
  description = "AWS region for resources"
  type        = string
  default     = "us-west-2"
}

variable "owner_email" {
  description = "Email of the owner for resource tagging"
  type        = string
  default     = "devops@yourcompany.com"
}

variable "cluster_name" {
  description = "Name of the EKS cluster"
  type        = string
  default     = "knowledge-graph-analytics-cluster"
}

variable "eks_cluster_version" {
  description = "Kubernetes version for EKS cluster"
  type        = string
  default     = "1.31"
}

variable "vpc_cidr" {
  description = "CIDR block for VPC"
  type        = string
  default     = "10.0.0.0/16"
}

# Node Group Configuration
variable "min_nodes" {
  description = "Minimum number of nodes in the EKS cluster"
  type        = number
  default     = 3
}

variable "max_nodes" {
  description = "Maximum number of nodes in the EKS cluster"
  type        = number
  default     = 20
}

variable "desired_nodes" {
  description = "Desired number of nodes in the EKS cluster"
  type        = number
  default     = 5
}

variable "node_instance_types" {
  description = "EC2 instance types for EKS nodes"
  type        = list(string)
  default     = ["m5.large", "m5a.large", "m5d.large"]
}

variable "cluster_admin_role_arns" {
  description = "IAM role ARNs granted system:masters on the EKS cluster via aws-auth. Set to your admin role ARN(s) before apply to avoid lockout when manage_aws_auth_configmap = true."
  type        = list(string)
  default     = []
}

# Database Configuration
variable "db_instance_class" {
  description = "RDS instance class"
  type        = string
  default     = "db.m5.large"
}

variable "db_allocated_storage" {
  description = "Initial allocated storage for RDS in GB"
  type        = number
  default     = 100
}

variable "db_max_allocated_storage" {
  description = "Maximum allocated storage for RDS in GB"
  type        = number
  default     = 1000
}

variable "db_name" {
  description = "Name of the database"
  type        = string
  default     = "ragdb"
}

variable "db_username" {
  description = "Username for the database"
  type        = string
  default     = "raguser"
}

# Redis Configuration
variable "redis_node_type" {
  description = "ElastiCache node type"
  type        = string
  default     = "cache.m5.large"
}

variable "redis_number_nodes" {
  description = "Number of Redis nodes"
  type        = number
  default     = 3
}

# Terraform State Configuration
variable "terraform_state_bucket" {
  description = "S3 bucket for Terraform state"
  type        = string
  default     = "knowledge-graph-analytics-terraform-state"
}

variable "terraform_lock_table" {
  description = "DynamoDB table for Terraform state locking"
  type        = string
  default     = "knowledge-graph-analytics-terraform-locks"
}

# Monitoring Configuration
variable "enable_monitoring" {
  description = "Enable CloudWatch monitoring"
  type        = bool
  default     = true
}

variable "log_retention_days" {
  description = "Number of days to retain logs"
  type        = number
  default     = 30
}

# Backup Configuration
variable "enable_cross_region_backup" {
  description = "Enable cross-region backup replication"
  type        = bool
  default     = true
}

variable "backup_region" {
  description = "Region for cross-region backup"
  type        = string
  default     = "us-east-1"
}

# SSL/TLS Configuration
variable "enable_ssl" {
  description = "Enable SSL/TLS"
  type        = bool
  default     = true
}

variable "ssl_certificate_arn" {
  description = "ARN of SSL certificate for load balancer"
  type        = string
  default     = ""
}

# Auto Scaling Configuration
variable "enable_cluster_autoscaler" {
  description = "Enable Kubernetes cluster autoscaler"
  type        = bool
  default     = true
}

variable "cluster_autoscaler_version" {
  description = "Version of cluster autoscaler to deploy"
  type        = string
  default     = "1.28.0"
}

# Security Configuration
variable "enable_security_scan" {
  description = "Enable security scanning tools"
  type        = bool
  default     = true
}

variable "enable_network_policy" {
  description = "Enable Kubernetes network policies"
  type        = bool
  default     = true
}

variable "enable_pod_security_policy" {
  description = "Enable Kubernetes pod security policies"
  type        = bool
  default     = true
}

# Cost Optimization
variable "enable_cost_allocation_tags" {
  description = "Enable AWS cost allocation tags"
  type        = bool
  default     = true
}

variable "cost_center" {
  description = "Cost center for billing"
  type        = string
  default     = "engineering"
}

# Application Configuration
variable "frontend_replicas" {
  description = "Number of frontend replicas"
  type        = number
  default     = 3
}

variable "backend_replicas" {
  description = "Number of backend replicas"
  type        = number
  default     = 3
}

variable "worker_replicas" {
  description = "Number of worker replicas"
  type        = number
  default     = 2
}

# External Services Configuration
variable "neo4j_enabled" {
  description = "Enable Neo4j graph database"
  type        = bool
  default     = true
}

variable "neo4j_instance_type" {
  description = "Neo4j instance type"
  type        = string
  default     = "db.r5.large"
}

# Logging Configuration
variable "enable_cloudwatch_logs" {
  description = "Enable CloudWatch logging"
  type        = bool
  default     = true
}

variable "enable_fluent_bit" {
  description = "Enable Fluent Bit for log shipping"
  type        = bool
  default     = true
}

variable "log_level" {
  description = "Log level for applications"
  type        = string
  default     = "INFO"

  validation {
    condition     = contains(["DEBUG", "INFO", "WARN", "ERROR"], var.log_level)
    error_message = "Log level must be one of: DEBUG, INFO, WARN, ERROR."
  }
}

# Alerting Configuration
variable "enable_alerts" {
  description = "Enable CloudWatch alerts"
  type        = bool
  default     = true
}

variable "alert_email" {
  description = "Email for alert notifications"
  type        = string
  default     = "alerts@yourcompany.com"
}

variable "slack_webhook_url" {
  description = "Slack webhook URL for alert notifications"
  type        = string
  default     = ""
  sensitive   = true
}