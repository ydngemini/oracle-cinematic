provider "aws" {
  region = var.aws_region

  default_tags {
    tags = merge({
      Project     = var.project
      Environment = var.environment
      ManagedBy   = "terraform"
    }, var.tags)
  }
}

data "aws_availability_zones" "available" {
  state = "available"
}

data "aws_caller_identity" "current" {}
data "aws_region" "current" {}

locals {
  name = "${var.project}-${var.environment}"

  azs = slice(data.aws_availability_zones.available.names, 0, var.az_count)

  # /20 public + /20 private per AZ carved from the VPC /16.
  public_subnets  = [for i in range(var.az_count) : cidrsubnet(var.vpc_cidr, 4, i)]
  private_subnets = [for i in range(var.az_count) : cidrsubnet(var.vpc_cidr, 4, i + 8)]

  account_id = data.aws_caller_identity.current.account_id

  # Defaults derived from the one domain, so a rename is a single tfvars edit
  # rather than five that must agree. The wildcard certificate covers any host
  # under it, so obs needs no separate certificate consideration.
  observability_host = coalesce(
    var.observability_host != "" ? var.observability_host : null,
    "obs.${var.domain_name}",
  )
}
