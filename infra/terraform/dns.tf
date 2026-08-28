# ── DNS ──────────────────────────────────────────────────────────────────────
# neohrs.com moved off Hostinger's share-dns nameservers on 2026-08-28. Until
# then the zone was unreachable from here, which is why the ACM certificate sat
# in PENDING_VALIDATION: the validation CNAME could only be added by hand at the
# registrar. With the zone in-account, validation records, the ALB alias and SES
# DKIM are all just resources.
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
}

# Apex and www to the ALB. These REPLACE the parking A records that pointed at
# 200.103.216.100 — applying them is the actual cutover, and the ALB answers 503
# for the minute or two before the ECS services pass their health checks. The
# parking page is not the product, so a short 503 is the better of the two.
resource "aws_route53_record" "apex" {
  zone_id = aws_route53_zone.main.zone_id
  name    = var.domain_name
  type    = "A"

  alias {
    name                   = aws_lb.main.dns_name
    zone_id                = aws_lb.main.zone_id
    evaluate_target_health = true
  }
}

resource "aws_route53_record" "www" {
  zone_id = aws_route53_zone.main.zone_id
  name    = "www.${var.domain_name}"
  type    = "A"

  alias {
    name                   = aws_lb.main.dns_name
    zone_id                = aws_lb.main.zone_id
    evaluate_target_health = true
  }
}

# The observability dashboard is a vhost on the same ALB (see observability.tf),
# so it needs its own name resolving there or the host-condition rule never fires.
resource "aws_route53_record" "obs" {
  zone_id = aws_route53_zone.main.zone_id
  name    = var.observability_host
  type    = "A"

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
