# =============================================================================
# Outputs for Knowledge Graph Analytics Dashboard
# =============================================================================

# =============================================================================
# EKS Cluster Outputs
# =============================================================================

output "cluster_name" {
  description = "Kubernetes cluster name"
  value       = module.eks.cluster_name
}

output "cluster_endpoint" {
  description = "Endpoint for EKS control plane"
  value       = module.eks.cluster_endpoint
}

output "cluster_certificate_authority_data" {
  description = "Cluster certificate authority data"
  value       = module.eks.cluster_certificate_authority_data
}

output "cluster_security_group_id" {
  description = "Security group ID attached to the EKS cluster control plane"
  value       = module.eks.cluster_security_group_id
}

output "cluster_iam_role_arn" {
  description = "IAM role ARN of the EKS cluster"
  value       = module.eks.cluster_iam_role_arn
}

output "eks_node_group_role_arn" {
  description = "IAM role ARN for EKS node groups"
  value       = module.eks.eks_managed_node_groups["default"].iam_role_arn
}

output "node_groups" {
  description = "Map of EKS managed node group names and their attributes"
  value       = module.eks.eks_managed_node_groups
}

# =============================================================================
# VPC Outputs
# =============================================================================

output "vpc_id" {
  description = "VPC ID"
  value       = module.vpc.vpc_id
}

output "vpc_cidr_block" {
  description = "VPC CIDR block"
  value       = module.vpc.vpc_cidr_block
}

output "private_subnets" {
  description = "List of private subnet IDs"
  value       = module.vpc.private_subnets
}

output "private_subnet_cidrs" {
  description = "List of private subnet CIDRs"
  value       = module.vpc.private_subnets_cidr_blocks
}

output "public_subnets" {
  description = "List of public subnet IDs"
  value       = module.vpc.public_subnets
}

output "public_subnet_cidrs" {
  description = "List of public subnet CIDRs"
  value       = module.vpc.public_subnets_cidr_blocks
}

output "nat_gateway_ids" {
  description = "List of NAT Gateway IDs"
  value       = module.vpc.natgw_ids
}

output "nat_gateway_public_ips" {
  description = "List of NAT Gateway public IPs"
  value       = module.vpc.nat_public_ips
}

# =============================================================================
# Database Outputs
# =============================================================================

output "database_endpoint" {
  description = "RDS instance endpoint"
  value       = module.rds.db_instance_endpoint
}

output "database_hosted_zone_id" {
  description = "RDS instance hosted zone ID"
  value       = module.rds.db_instance_hosted_zone_id
}

output "database_port" {
  description = "RDS instance port"
  value       = module.rds.db_instance_port
}

output "database_name" {
  description = "Database name"
  value       = module.rds.db_instance_name
  sensitive   = true
}

output "database_username" {
  description = "Database username"
  value       = module.rds.db_instance_username
  sensitive   = true
}

output "database_resource_id" {
  description = "RDS instance resource ID"
  value       = module.rds.db_instance_resource_id
}

output "database_status" {
  description = "RDS instance status"
  value       = module.rds.db_instance_status
}

output "database_engine" {
  description = "Database engine"
  value       = module.rds.db_instance_engine
}

output "database_engine_version" {
  description = "Database engine version"
  value       = module.rds.db_instance_engine_version_actual
}

# =============================================================================
# Redis Outputs
# =============================================================================

output "redis_endpoint" {
  description = "Redis replication group primary endpoint"
  value       = module.elasticache.replication_group_primary_endpoint_address
}

output "redis_reader_endpoint" {
  description = "Redis replication group reader endpoint"
  value       = module.elasticache.replication_group_reader_endpoint_address
}

output "redis_port" {
  description = "Redis port"
  value       = module.elasticache.replication_group_port
}

output "redis_members" {
  description = "List of Redis cluster members"
  value       = module.elasticache.replication_group_member_clusters
}

output "redis_engine_version" {
  description = "Redis engine version"
  value       = module.elasticache.replication_group_engine_version_actual
}

# =============================================================================
# Storage Outputs
# =============================================================================

output "storage_bucket_name" {
  description = "S3 bucket name for application storage"
  value       = aws_s3_bucket.application_storage.bucket
}

output "storage_bucket_arn" {
  description = "S3 bucket ARN for application storage"
  value       = aws_s3_bucket.application_storage.arn
}

output "storage_bucket_domain_name" {
  description = "S3 bucket domain name"
  value       = aws_s3_bucket.application_storage.bucket_domain_name
}

output "storage_bucket_regional_domain_name" {
  description = "S3 bucket regional domain name"
  value       = aws_s3_bucket.application_storage.bucket_regional_domain_name
}

# =============================================================================
# Security Outputs
# =============================================================================

output "cluster_oidc_issuer_url" {
  description = "The OIDC issuer URL of the EKS cluster"
  value       = module.eks.cluster_oidc_issuer_url
}

output "cluster_oidc_provider_arn" {
  description = "The OIDC provider ARN of the EKS cluster"
  value       = module.eks.oidc_provider_arn
}

output "rds_security_group_id" {
  description = "Security group ID for RDS"
  value       = aws_security_group.rds.id
}

output "redis_security_group_id" {
  description = "Security group ID for Redis"
  value       = aws_security_group.redis.id
}

# =============================================================================
# Configuration Outputs
# =============================================================================

output "aws_region" {
  description = "AWS region"
  value       = var.aws_region
}

output "environment" {
  description = "Environment name"
  value       = var.environment
}

output "project_name" {
  description = "Project name"
  value       = var.project_name
}

output "kubeconfig" {
  description = "kubectl configuration file"
  value       = <<-EOT
    apiVersion: v1
    clusters:
    - cluster:
        server: ${module.eks.cluster_endpoint}
        certificate-authority-data: ${module.eks.cluster_certificate_authority_data}
      name: ${module.eks.cluster_name}
    contexts:
    - context:
        cluster: ${module.eks.cluster_name}
        user: ${module.eks.cluster_name}
      name: ${module.eks.cluster_name}
    current-context: ${module.eks.cluster_name}
    kind: Config
    preferences: {}
    users:
    - name: ${module.eks.cluster_name}
      user:
        exec:
          apiVersion: client.authentication.k8s.io/v1beta1
          command: aws
          args:
            - "eks"
            - "get-token"
            - "--cluster-name"
            - "${module.eks.cluster_name}"
            - "--region"
            - "${var.aws_region}"
  EOT
  sensitive   = true
}

# =============================================================================
# Additional Useful Outputs
# =============================================================================

output "configure_kubectl" {
  description = "Command to configure kubectl to connect to the cluster"
  value       = "aws eks update-kubeconfig --name ${module.eks.cluster_name} --region ${var.aws_region}"
}

output "helm_release_command" {
  description = "Command to deploy the application using Helm"
  value       = "helm upgrade --install knowledge-graph-analytics ./infrastructure/helm/knowledge-graph-analytics --namespace knowledge-graph-analytics --create-namespace"
}

output "cluster_addons" {
  description = "List of cluster addons deployed"
  value = {
    coredns            = module.eks.cluster_addons["coredns"]
    kube_proxy         = module.eks.cluster_addons["kube-proxy"]
    vpc_cni            = module.eks.cluster_addons["vpc-cni"]
    aws_ebs_csi_driver = module.eks.cluster_addons["aws-ebs-csi-driver"]
  }
}

output "availability_zones" {
  description = "List of availability zones"
  value       = data.aws_availability_zones.available.names
}

output "caller_identity" {
  description = "AWS caller identity information"
  value = {
    account_id = data.aws_caller_identity.current.account_id
    arn        = data.aws_caller_identity.current.arn
    user_id    = data.aws_caller_identity.current.user_id
  }
}