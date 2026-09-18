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

  # Backend values are passed via -backend-config flags (variables are not
  # allowed in the backend block):
  #   terraform init \
  #     -backend-config="bucket=nous-tfstate-us-east-1" \
  #     -backend-config="key=knowledge-graph-analytics/terraform.tfstate" \
  #     -backend-config="region=us-east-1" \
  #     -backend-config="encrypt=true" \
  #     -backend-config="dynamodb_table=nous-tfstate-lock"
  backend "s3" {}
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

  # Pinned to 3 AZs: EKS rejects us-east-1e for control planes, and
  # multi-AZ NAT would triple NAT cost with the full zone list.
  azs             = ["us-east-1a", "us-east-1b", "us-east-1c"]
  private_subnets = [for i in range(3) : cidrsubnet(var.vpc_cidr, 4, i)]
  public_subnets  = [for i in range(3) : cidrsubnet(var.vpc_cidr, 8, i + 100)]

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
      # IRSA for the CSI controller (ebs-csi-irsa.tf). The live addon already
      # has this role attached (was applied out-of-band); if state drifts to
      # role="", re-read with:
      #   terraform apply -refresh-only -target=module.eks.aws_eks_addon.this["aws-ebs-csi-driver"]
      service_account_role_arn = aws_iam_role.ebs_csi.arn
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
        # {{SessionName}} resolves to the instance ID (e.g. i-0abc...) — the
        # kubelet registers under its private DNS name, so NodeAuthorizer
        # rejects it ("node i-xxx is not allowed to modify node ip-10-...").
        # {{EC2PrivateDNSName}} is the correct username template.
        rolearn  = module.eks.eks_managed_node_groups["default"].iam_role_arn
        username = "system:node:{{EC2PrivateDNSName}}"
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

      # EKS-managed launch template: custom LT rendered empty user-data (no
      # bootstrap args) → instances launched but never joined the cluster.
      # EKS-managed LT + default AMI bootstraps correctly.
      use_custom_launch_template = false
      disk_size                  = 100

      update_config = {
        max_unavailable_percentage = 33
      }

      # Module-managed node role (aws-auth maps module.eks...iam_role_arn)

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
    # NOTE: no cluster→node 10250 rule here — module's built-in
    # ingress_cluster_kubelet already covers it; a duplicate definition
    # collides with it (InvalidPermission.Duplicate).
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
  engine_version = "16.15"
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
  create_db_subnet_group = true # default false — without it RDS lands in the default VPC

  maintenance_window = "Mon:03:00-Mon:04:00"
  backup_window      = "04:00-06:00"

  backup_retention_period          = 7
  skip_final_snapshot              = var.environment == "development" ? true : false
  final_snapshot_identifier_prefix = "${var.project_name}-final-snapshot"

  deletion_protection = var.environment == "production" ? true : false

  family               = "postgres16"
  major_engine_version = "16"

  parameters = [
    {
      name         = "shared_preload_libraries"
      value        = "pg_stat_statements"
      apply_method = "pending-reboot" # static parameter — immediate not allowed
    },
    {
      name         = "log_statement"
      value        = "all"
      apply_method = "immediate"
    },
    {
      name         = "log_min_duration_statement"
      value        = "1000"
      apply_method = "immediate"
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

  vpc_id     = module.vpc.vpc_id
  subnet_ids = module.vpc.private_subnets

  # Subnet group managed by root-level aws_elasticache_subnet_group.default
  # (below) — module must not create its own duplicate.
  create_subnet_group = false
  subnet_group_name   = aws_elasticache_subnet_group.default.name
  security_group_ids  = [aws_security_group.redis.id]

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