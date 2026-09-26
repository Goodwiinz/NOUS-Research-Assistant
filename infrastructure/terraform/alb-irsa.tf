# =============================================================================
# IRSA role for the AWS Load Balancer Controller
# (system:serviceaccount:kube-system:aws-load-balancer-controller)
#
# OIDC provider is created by the EKS module (module.eks, enable_irsa).
# The controller Deployment itself is installed out-of-band via helm —
# see infrastructure/kubernetes/aws-addons.md for the exact commands.
# =============================================================================

data "aws_iam_policy_document" "alb_controller_assume_role" {
  statement {
    effect  = "Allow"
    actions = ["sts:AssumeRoleWithWebIdentity"]

    principals {
      type        = "Federated"
      identifiers = [module.eks.oidc_provider_arn]
    }

    condition {
      test     = "StringEquals"
      variable = "${module.eks.oidc_provider}:sub"
      values   = ["system:serviceaccount:kube-system:aws-load-balancer-controller"]
    }
  }
}

resource "aws_iam_role" "alb_controller" {
  name               = "${var.project_name}-${var.environment}-alb-controller"
  assume_role_policy = data.aws_iam_policy_document.alb_controller_assume_role.json
}

# Official v2.x IAM policy from
# https://github.com/kubernetes-sigs/aws-load-balancer-controller/blob/v2.13.4/docs/install/iam_policy.json
resource "aws_iam_policy" "alb_controller" {
  name        = "${var.project_name}-${var.environment}-alb-controller-iam-policy"
  description = "IAM policy for the AWS Load Balancer Controller (v2.13.x)"

  policy = file("${path.module}/alb-controller-iam-policy.json")
}

resource "aws_iam_role_policy_attachment" "alb_controller" {
  role       = aws_iam_role.alb_controller.name
  policy_arn = aws_iam_policy.alb_controller.arn
}

output "alb_controller_role_arn" {
  description = "IRSA role ARN to annotate the aws-load-balancer-controller service account with"
  value       = aws_iam_role.alb_controller.arn
}
