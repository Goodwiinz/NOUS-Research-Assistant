# =============================================================================
# ACM Certificate for *.gen-text.app (Cloudflare DNS, validation out-of-band)
# =============================================================================

resource "aws_acm_certificate" "cluster" {
  domain_name       = "*.gen-text.app"
  validation_method = "DNS"

  lifecycle {
    create_before_destroy = true
  }

  tags = {
    Project     = "Knowledge Graph Analytics"
    Environment = var.environment
    ManagedBy   = "Terraform"
    Owner       = var.owner_email
  }
}

# NOTE: this resource will remain in "pending" state until a CNAME record is
# created MANUALLY in Cloudflare (DNS is NOT Route53 here — see the
# acm_validation_record output for the record to add). Do NOT `terraform
# apply -target` this resource before the CNAME exists: the apply would block
# waiting for the certificate to reach ISSUED. After the CNAME is in place
# and the certificate is ISSUED, apply this resource normally (or just run a
# full apply).
resource "aws_acm_certificate_validation" "cluster" {
  certificate_arn = aws_acm_certificate.cluster.arn
}

output "acm_validation_record" {
  description = "Create this CNAME manually in Cloudflare, then wait for ISSUED"
  value = {
    for dvo in aws_acm_certificate.cluster.domain_validation_options : dvo.domain_name => {
      name  = dvo.resource_record_name
      type  = dvo.resource_record_type
      value = dvo.resource_record_value
    }
  }
}

output "acm_certificate_arn" {
  description = "ARN of the *.gen-text.app certificate (for ALB ingress annotations)"
  value       = aws_acm_certificate.cluster.arn
}
