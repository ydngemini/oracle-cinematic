# ── DNS ──────────────────────────────────────────────────────────────────────
# neohrs.com moved off Hostinger's share-dns nameservers on 2026-08-28. Until
# then the zone was unreachable from here, which is why the ACM certificate sat
# in PENDING_VALIDATION: the validation CNAME could only be added by hand at the
# registrar.
#
# What this file manages is the ALIAS records below and nothing else. The ACM
# certificate is still requested out-of-band and passed in as
# var.acm_certificate_arn, its validation CNAME was written into the zone by
# hand, and SES DKIM is still printed by infra/scripts/setup-ses.sh for manual
# entry. Having the zone in-account makes automating those possible; it has not
# done so yet, and an operator reading otherwise would go looking for validation
# records that do not exist.
#
# The zone was created out-of-band (the deploy was blocked on it) and is adopted
# here by an import block rather than recreated — recreating it would mint fresh
# nameservers and break the delegation that was set at the registrar.
#
# The registrar still owns the NS delegation. `terraform output route53_name_servers`
# prints what must be set at Hostinger; nothing in this file can enforce it.

import {
  to = aws_route53_zone.main
  id = "Z01948911TPWLLT18DY2W"
}

resource "aws_route53_zone" "main" {
  name    = var.domain_name
  comment = "neohrs.com — moved off Hostinger share-dns 2026-08-28 so ACM DNS validation and the ALB alias can be managed in-account"

  tags = { Name = "${local.name}-zone" }

  # aws_route53_zone.name is ForceNew, and the import above pins a specific zone.
  # variables.tf defaults domain_name to "" and terraform.tfvars.example carries
  # app.neoh.example, so a fresh clone would plan to DESTROY the imported zone and
  # create a replacement — minting new nameservers and orphaning the delegation
  # set at the registrar, which is the exact outcome the import block exists to
  # prevent. Fail the plan instead of discovering it in the apply log.
  lifecycle {
    precondition {
      condition     = var.domain_name == "neohrs.com"
      error_message = "dns.tf imports hosted zone Z01948911TPWLLT18DY2W, which serves neohrs.com. Set domain_name = \"neohrs.com\", or remove the import block and this file if deploying a different domain."
    }
  }
}

# Apex and www to the ALB. These REPLACE the parking A records that pointed at
# 200.103.216.100 — applying them is the actual cutover, and the ALB answers 503
# for the minute or two before the ECS services pass their health checks. The
# parking page is not the product, so a short 503 is the better of the two.
resource "aws_route53_record" "apex" {
  zone_id = aws_route53_zone.main.zone_id
  name    = var.domain_name
  type    = "A"

  # The parking A records are IN the zone but not in state — the zone was
  # created out-of-band and adopted by import. Route53 refuses to create a
  # record set that already exists, so without this the apply aborts with
  # "Tried to create resource record set ... but it already exists" and the
  # cutover never happens.
  allow_overwrite = true

  alias {
    name                   = aws_lb.main.dns_name
    zone_id                = aws_lb.main.zone_id
    evaluate_target_health = true
  }
}

resource "aws_route53_record" "www" {
  zone_id         = aws_route53_zone.main.zone_id
  name            = "www.${var.domain_name}"
  type            = "A"
  allow_overwrite = true

  alias {
    name                   = aws_lb.main.dns_name
    zone_id                = aws_lb.main.zone_id
    evaluate_target_health = true
  }
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

  zone_id         = aws_route53_zone.main.zone_id
  name            = var.observability_host
  type            = "A"
  allow_overwrite = true

  alias {
    name                   = aws_lb.main.dns_name
    zone_id                = aws_lb.main.zone_id
    evaluate_target_health = true
  }
}

output "route53_name_servers" {
  description = "Set these as the domain's nameservers at the registrar. DNS does not move until they are live."
  value       = aws_route53_zone.main.name_servers
}

output "route53_zone_id" {
  value = aws_route53_zone.main.zone_id
}
