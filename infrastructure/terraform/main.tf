# =============================================================================
# Knowledge Graph Analytics Dashboard - Terraform Configuration
# =============================================================================

terraform {
  required_version = ">= 1.5.0"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
    kubernetes = {
      source  = "hashicorp/kubernetes"
      version = "~> 2.20"
    }
    helm = {
      source  = "hashicorp/helm"
      version = "~> 2.9"
    }
    kubectl = {
      source  = "alekc/kubectl"
      version = "~> 2.0"
    }
    random = {
      source  = "hashicorp/random"
      version = "~> 3.5"
    }
    time = {
      source  = "hashicorp/time"
      version = "~> 0.9"
    }
  }

  backend "s3" {
    bucket         = var.terraform_state_bucket
    key            = "knowledge-graph-analytics/terraform.tfstate"
    region         = var.aws_region
    encrypt        = true
    dynamodb_table = var.terraform_lock_table
  }
}

# =============================================================================
# AWS Provider Configuration
# =============================================================================

provider "aws" {
  region = var.aws_region

  default_tags {
    tags = {
      Project     = "Knowledge Graph Analytics"
      Environment = var.environment
      ManagedBy   = "Terraform"
      Owner       = var.owner_email
    }
  }
}

provider "kubernetes" {
  host                   = module.eks.cluster_endpoint
  cluster_ca_certificate = base64decode(module.eks.cluster_certificate_authority_data)

  exec {
    api_version = "client.authentication.k8s.io/v1beta1"
    command     = "aws"
    args        = ["eks", "get-token", "--cluster-name", module.eks.cluster_name]
  }
}

provider "helm" {
  kubernetes {
    host                   = module.eks.cluster_endpoint
    cluster_ca_certificate = base64decode(module.eks.cluster_certificate_authority_data)

    exec {
      api_version = "client.authentication.k8s.io/v1beta1"
      command     = "aws"
      args        = ["eks", "get-token", "--cluster-name", module.eks.cluster_name]
    }
  }
}

provider "kubectl" {
  host                   = module.eks.cluster_endpoint
  cluster_ca_certificate = base64decode(module.eks.cluster_certificate_authority_data)
  load_config_file       = false

  exec {
    api_version = "client.authentication.k8s.io/v1beta1"
    command     = "aws"
    args        = ["eks", "get-token", "--cluster-name", module.eks.cluster_name]
  }
}

# =============================================================================
# Data Sources
# =============================================================================

data "aws_caller_identity" "current" {}
data "aws_availability_zones" "available" {
  state = "available"
}

data "aws_iam_policy_document" "assume_role" {
  statement {
    effect = "Allow"
    principals {
      type        = "Service"
      identifiers = ["eks.amazonaws.com"]
    }
    actions = ["sts:AssumeRole"]
  }
}

data "aws_iam_policy_document" "node_assume_role" {
  statement {
    effect = "Allow"
    principals {
      type        = "Service"
      identifiers = ["ec2.amazonaws.com"]
    }
    actions = ["sts:AssumeRole"]
  }
}

# =============================================================================
# VPC Configuration
# =============================================================================

module "vpc" {
  source  = "terraform-aws-modules/vpc/aws"
  version = "~> 5.0"

  name = "${var.project_name}-vpc"
  cidr = var.vpc_cidr

  azs             = data.aws_availability_zones.available.names
  private_subnets = [for i in range(length(data.aws_availability_zones.available.names)) : cidrsubnet(var.vpc_cidr, 4, i)]
  public_subnets  = [for i in range(length(data.aws_availability_zones.available.names)) : cidrsubnet(var.vpc_cidr, 8, i + 100)]

  enable_nat_gateway = true
  # Lean dev choice: one NAT gateway shared across AZs (~$33/mo) instead of
  # per-AZ gateways (~$100/mo). Revisit per-AZ for prod.
  single_nat_gateway     = true
  one_nat_gateway_per_az = false
  enable_dns_hostnames   = true
  enable_dns_support     = true

  public_subnet_tags = {
    # var.cluster_name instead of module.eks.cluster_name: avoids
    # vpc <-> eks dependency cycle (EKS subnets depend on VPC output)
    "kubernetes.io/cluster/${var.cluster_name}" = "shared"
    "kubernetes.io/role/elb"                    = "1"
    Type                                        = "Public"
  }

  private_subnet_tags = {
    "kubernetes.io/cluster/${var.cluster_name}" = "shared"
    "kubernetes.io/role/internal-elb"           = "1"
    Type                                        = "Private"
  }

  tags = {
    Name = "${var.project_name}-vpc"
  }
}

# =============================================================================
# EKS Cluster Configuration
# =============================================================================

module "eks" {
  source  = "terraform-aws-modules/eks/aws"
  version = "~> 19.0"

  cluster_name    = var.cluster_name
  cluster_version = var.eks_cluster_version
  vpc_id          = module.vpc.vpc_id
  subnet_ids      = module.vpc.private_subnets

  cluster_endpoint_public_access  = true
  cluster_endpoint_private_access = true

  cluster_addons = {
    coredns = {
      most_recent = true
    }
    kube-proxy = {
      most_recent = true
    }
    vpc-cni = {
      most_recent = true
    }
    aws-ebs-csi-driver = {
      most_recent = true
    }
  }

  create_cluster_security_group = true
  create_node_security_group    = true

  manage_aws_auth_configmap = true
  # Node role grants bootstrap/nodes; cluster admin role(s) from
  # var.cluster_admin_role_arns get system:masters so the cluster creator is
  # not locked out when the aws-auth ConfigMap is terraform-managed.
  aws_auth_roles = concat(
    [
      {
        rolearn  = module.eks.eks_managed_node_groups["default"].iam_role_arn
        username = "system:node:{{SessionName}}"
        groups   = ["system:bootstrappers", "system:nodes"]
      }
    ],
    [
      for arn in var.cluster_admin_role_arns : {
        rolearn  = arn
        username = "admin"
        groups   = ["system:masters"]
      }
    ]
  )

  eks_managed_node_groups = {
    default = {
      instance_types = var.node_instance_types

      min_size     = var.min_nodes
      max_size     = var.max_nodes
      desired_size = var.desired_nodes

      # Lean dev sizing: single SPOT node group (t3.large via var.node_instance_types)
      capacity_type = "SPOT"

      update_config = {
        max_unavailable_percentage = 33
      }

      iam_role_arn = aws_iam_role.nodes.arn

      block_device_mappings = {
        xvda = {
          device_name = "/dev/xvda"
          ebs = {
            volume_size = 100
            volume_type = "gp3"
            iops        = 3000
            throughput  = 125
          }
        }
      }

      tags = {
        Name = "${var.project_name}-node-group"
        Type = "EKS Managed Node Group"

        # Cluster Autoscaler discovery tags (ASG autodiscovery)
        "k8s.io/cluster-autoscaler/enabled"             = "true"
        "k8s.io/cluster-autoscaler/${var.cluster_name}" = "owned"
      }
    }
  }

  cluster_security_group_additional_rules = {
    ingress_nodes_443 = {
      description                = "Node groups to API server communication"
      protocol                   = "tcp"
      from_port                  = 443
      to_port                    = 443
      type                       = "ingress"
      source_node_security_group = true
    }
  }

  node_security_group_additional_rules = {
    ingress_self_all = {
      description = "Node to node all ports/protocols"
      protocol    = "-1"
      from_port   = 0
      to_port     = 0
      type        = "ingress"
      self        = true
    }
    ingress_cluster_kublet_api = {
      description                   = "Cluster API to Node kubelet API"
      protocol                      = "tcp"
      from_port                     = 10250
      to_port                       = 10250
      type                          = "ingress"
      source_cluster_security_group = true
    }
  }

  tags = {
    Name = "${var.project_name}-eks-cluster"
  }
}

# =============================================================================
# IAM Roles and Policies
# =============================================================================

resource "aws_iam_role" "cluster" {
  name = "${var.project_name}-eks-cluster-role"

  assume_role_policy = data.aws_iam_policy_document.assume_role.json
}

resource "aws_iam_role_policy_attachment" "cluster_AmazonEKSClusterPolicy" {
  policy_arn = "arn:aws:iam::aws:policy/AmazonEKSClusterPolicy"
  role       = aws_iam_role.cluster.name
}

resource "aws_iam_role_policy_attachment" "cluster_AmazonEKSVPCResourceController" {
  policy_arn = "arn:aws:iam::aws:policy/AmazonEKSVPCResourceController"
  role       = aws_iam_role.cluster.name
}

resource "aws_iam_role" "nodes" {
  name = "${var.project_name}-eks-node-role"

  assume_role_policy = data.aws_iam_policy_document.node_assume_role.json
}

resource "aws_iam_role_policy_attachment" "nodes_AmazonEKSWorkerNodePolicy" {
  policy_arn = "arn:aws:iam::aws:policy/AmazonEKSWorkerNodePolicy"
  role       = aws_iam_role.nodes.name
}

resource "aws_iam_role_policy_attachment" "nodes_AmazonEKS_CNI_Policy" {
  policy_arn = "arn:aws:iam::aws:policy/AmazonEKS_CNI_Policy"
  role       = aws_iam_role.nodes.name
}

resource "aws_iam_role_policy_attachment" "nodes_AmazonEC2ContainerRegistryReadOnly" {
  policy_arn = "arn:aws:iam::aws:policy/AmazonEC2ContainerRegistryReadOnly"
  role       = aws_iam_role.nodes.name
}

# Additional IAM policies for application requirements
resource "aws_iam_policy" "eks_additional_policy" {
  name        = "${var.project_name}-eks-additional-policy"
  description = "Additional permissions for EKS nodes"

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect = "Allow"
        Action = [
          "s3:GetObject",
          "s3:PutObject",
          "s3:DeleteObject",
          "s3:ListBucket"
        ]
        Resource = [
          "${aws_s3_bucket.application_storage.arn}",
          "${aws_s3_bucket.application_storage.arn}/*"
        ]
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
          "ecr:GetAuthorizationToken",
          "ecr:BatchCheckLayerAvailability",
          "ecr:GetDownloadUrlForLayer",
          "ecr:BatchGetImage"
        ]
        Resource = "*"
      }
    ]
  })
}

resource "aws_iam_role_policy_attachment" "nodes_additional_policy" {
  policy_arn = aws_iam_policy.eks_additional_policy.arn
  role       = aws_iam_role.nodes.name
}

# =============================================================================
# Application Storage
# =============================================================================

resource "aws_s3_bucket" "application_storage" {
  bucket = "${var.project_name}-${var.environment}-storage-${random_string.bucket_suffix.result}"
}

resource "random_string" "bucket_suffix" {
  length  = 8
  special = false
  upper   = false
}

resource "aws_s3_bucket_versioning" "application_storage" {
  bucket = aws_s3_bucket.application_storage.id
  versioning_configuration {
    status = "Enabled"
  }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "application_storage" {
  bucket = aws_s3_bucket.application_storage.id

  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

resource "aws_s3_bucket_public_access_block" "application_storage" {
  bucket = aws_s3_bucket.application_storage.id

  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

# =============================================================================
# RDS Configuration (for PostgreSQL)
# =============================================================================

module "rds" {
  source  = "terraform-aws-modules/rds/aws"
  version = "~> 6.0"

  identifier = "${var.project_name}-postgres"

  engine         = "postgres"
  engine_version = "15.4"
  instance_class = var.db_instance_class

  allocated_storage     = var.db_allocated_storage
  max_allocated_storage = var.db_max_allocated_storage
  storage_encrypted     = true
  storage_type          = "gp3"

  db_name  = var.db_name
  username = var.db_username
  password = random_password.db_password.result

  port                   = 5432
  vpc_security_group_ids = [aws_security_group.rds.id]
  subnet_ids             = module.vpc.private_subnets

  maintenance_window = "Mon:03:00-Mon:04:00"
  backup_window      = "04:00-06:00"

  backup_retention_period          = 7
  skip_final_snapshot              = var.environment == "development" ? true : false
  final_snapshot_identifier_prefix = "${var.project_name}-final-snapshot"

  deletion_protection = var.environment == "production" ? true : false

  family               = "postgres15"
  major_engine_version = "15"

  parameters = [
    {
      name  = "shared_preload_libraries"
      value = "pg_stat_statements"
    },
    {
      name  = "log_statement"
      value = "all"
    },
    {
      name  = "log_min_duration_statement"
      value = "1000"
    }
  ]

  tags = {
    Name = "${var.project_name}-postgres"
  }
}

resource "random_password" "db_password" {
  length  = 32
  special = true
}

resource "aws_security_group" "rds" {
  name_prefix = "${var.project_name}-rds-"
  vpc_id      = module.vpc.vpc_id

  ingress {
    description     = "PostgreSQL from EKS nodes"
    from_port       = 5432
    to_port         = 5432
    protocol        = "tcp"
    security_groups = [module.eks.cluster_security_group_id]
  }

  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }

  tags = {
    Name = "${var.project_name}-rds-sg"
  }
}

# =============================================================================
# ElastiCache Configuration (for Redis)
# =============================================================================

module "elasticache" {
  source  = "terraform-aws-modules/elasticache/aws"
  version = "~> 1.0"

  create_replication_group = true
  replication_group_id     = "${var.project_name}-redis"
  description              = "Redis cluster for ${var.project_name}"

  node_type                  = var.redis_node_type
  num_cache_clusters         = var.redis_number_nodes
  port                       = 6379
  parameter_group_name       = "default.redis7"
  auth_token                 = random_password.redis_auth_token.result
  transit_encryption_enabled = true
  at_rest_encryption_enabled = true

  subnet_group_name  = aws_elasticache_subnet_group.default.name
  security_group_ids = [aws_security_group.redis.id]

  # Single-node lean dev cache: ElastiCache requires >= 2 nodes for automatic
  # failover / multi-AZ, so both must be disabled when redis_number_nodes == 1.
  automatic_failover_enabled = false
  multi_az_enabled           = false

  snapshot_retention_limit = 7
  snapshot_window          = "03:00-05:00"
  maintenance_window       = "sun:05:00-sun:06:00"

  tags = {
    Name = "${var.project_name}-redis"
  }
}

resource "random_password" "redis_auth_token" {
  length  = 64
  special = false
}

resource "aws_elasticache_subnet_group" "default" {
  name       = "${var.project_name}-subnet-group"
  subnet_ids = module.vpc.private_subnets
}

resource "aws_security_group" "redis" {
  name_prefix = "${var.project_name}-redis-"
  vpc_id      = module.vpc.vpc_id

  ingress {
    description     = "Redis from EKS nodes"
    from_port       = 6379
    to_port         = 6379
    protocol        = "tcp"
    security_groups = [module.eks.cluster_security_group_id]
  }

  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }

  tags = {
    Name = "${var.project_name}-redis-sg"
  }
}

# =============================================================================
# Outputs are defined in outputs.tf
# =============================================================================