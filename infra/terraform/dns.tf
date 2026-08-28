# ── DNS ──────────────────────────────────────────────────────────────────────
# The domain is registered THROUGH Route53 (route53domains), so registration
# creates the hosted zone and points the registrar's NS records at it in one
# step. There is no external registrar to visit and no delegation to wait on —
# which is the whole reason for buying the name here rather than reusing
# neohrs.com, whose nameservers sat at Hostinger and could only be changed by
# hand. That single manual step blocked this deploy for a day.
#
# The zone is therefore looked up, not created: route53domains already made it,
# and a managed `aws_route53_zone` would either fight that or force-replace it
# and mint nameservers the registrar half does not know about.
#
# What this file manages is the ALIAS records and the ACM validation record.
# SES DKIM is still printed by infra/scripts/setup-ses.sh for manual entry —
# that one is not automated yet, and an operator reading otherwise would go
# looking for records that do not exist.

data "aws_route53_zone" "main" {
  name         = var.domain_name
  private_zone = false
}

# The certificate and its validation record, both managed here now. Previously
# the cert was requested by hand and its CNAME pasted at the registrar, which is
# precisely why it sat in PENDING_VALIDATION for a day. With the zone in-account
# from the moment of registration, `terraform apply` requests, validates and
# waits — no console, no copy-paste, no second party.
resource "aws_acm_certificate" "main" {
  domain_name               = var.domain_name
  subject_alternative_names = ["*.${var.domain_name}"]
  validation_method         = "DNS"

  # The ALB listener holds a reference to this cert, so a replacement has to be
  # created before the old one can go.
  lifecycle { create_before_destroy = true }

  tags = { Name = "${local.name}-cert" }
}

resource "aws_route53_record" "cert_validation" {
  # Apex and wildcard share one validation record, so the map collapses to a
  # single entry — for_each rather than count because ACM does not promise the
  # order of domain_validation_options.
  for_each = {
    for option in aws_acm_certificate.main.domain_validation_options :
    option.domain_name => option
  }

  zone_id         = data.aws_route53_zone.main.zone_id
  name            = each.value.resource_record_name
  type            = each.value.resource_record_type
  records         = [each.value.resource_record_value]
  ttl             = 60
  allow_overwrite = true
}

resource "aws_acm_certificate_validation" "main" {
  certificate_arn         = aws_acm_certificate.main.arn
  validation_record_fqdns = [for record in aws_route53_record.cert_validation : record.fqdn]
}

# www resolves to the ALB only so this rule can send it away again. The HTTPS
# default action forwards ANY host to the frontend target group, so without the
# redirect www would serve the SPA — and that SPA is built with
# VITE_API_BASE=https://neohrs.com, making every /api call cross-origin against
# a cors_origins list that contains the apex alone. cors_config.py refuses
# wildcards, so the result is not an error page but a silently dead app.
# One canonical origin also spares us a second CORS entry and duplicate content.
resource "aws_lb_listener_rule" "www_redirect" {
  listener_arn = aws_lb_listener.https.arn
  priority     = 5 # ahead of the api/portal/observability rules

  action {
    type = "redirect"
    redirect {
      host        = var.domain_name
      path        = "/#{path}"
      query       = "#{query}"
      protocol    = "HTTPS"
      port        = "443"
      status_code = "HTTP_301"
    }
  }

  condition {
    host_header {
      values = ["www.${var.domain_name}"]
    }
  }
}

# The observability dashboard is a vhost on the same ALB (see observability.tf),
# so it needs its own name resolving there or the host-condition rule never fires.
# Gated with the rest of the feature: every other observability resource is
# count-gated, and an ungated record would point obs.<domain> at an ALB whose
# default action serves the FRONTEND — publishing the main product on a hostname
# the deploy docs describe as an admin-only dashboard.
resource "aws_route53_record" "obs" {
  count = var.observability_enabled ? 1 : 0

  zone_id         = data.aws_route53_zone.main.zone_id
  name            = local.observability_host
  type            = "A"
  allow_overwrite = true

  alias {
    name                   = aws_lb.main.dns_name
    zone_id                = aws_lb.main.zone_id
    evaluate_target_health = true
  }
}

output "route53_name_servers" {
  description = "Informational. Registration through route53domains already points the registrar at these."
  value       = data.aws_route53_zone.main.name_servers
}

output "route53_zone_id" {
  value = data.aws_route53_zone.main.zone_id
}
