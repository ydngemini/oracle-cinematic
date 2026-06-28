variable "aws_region" {
  description = "AWS region to deploy into. Must match BEDROCK_REGION for in-region inference."
  type        = string
  default     = "us-east-1"
}

variable "project" {
  description = "Name prefix for all resources."
  type        = string
  default     = "neoh"
}

variable "environment" {
  description = "Deployment environment tag (prod/staging)."
  type        = string
  default     = "prod"
}

# ── networking ──────────────────────────────────────────────────────────────
variable "vpc_cidr" {
  description = "CIDR for the VPC."
  type        = string
  default     = "10.42.0.0/16"
}

variable "az_count" {
  description = "Number of AZs to span (>=2 for Aurora + ALB)."
  type        = number
  default     = 2
}

# ── TLS / domain ─────────────────────────────────────────────────────────────
variable "acm_certificate_arn" {
  description = "ARN of an ISSUED ACM cert in this region covering the app domain. Required for the HTTPS listener."
  type        = string
}

variable "domain_name" {
  description = "Public hostname the app serves on (for documentation/outputs; DNS is managed by you)."
  type        = string
  default     = ""
}

# ── database (Aurora PostgreSQL Serverless v2) ──────────────────────────────
variable "db_name" {
  description = "Initial database name."
  type        = string
  default     = "oracle"
}

variable "db_engine_version" {
  description = "Aurora PostgreSQL engine version (PostGIS-capable, matches dev PG16)."
  type        = string
  default     = "16.4"
}

variable "db_min_acu" {
  description = "Serverless v2 minimum Aurora Capacity Units (0.5 = scale-to-floor)."
  type        = number
  default     = 0.5
}

variable "db_max_acu" {
  description = "Serverless v2 maximum ACU ceiling."
  type        = number
  default     = 4
}

# ── containers ──────────────────────────────────────────────────────────────
variable "image_tag" {
  description = "Image tag for both backend + frontend (push this tag to ECR before apply, e.g. a git SHA)."
  type        = string
  default     = "latest"
}

variable "backend_cpu" {
  description = "Fargate CPU units for the backend task (1024 = 1 vCPU). Playwright/Chromium needs headroom."
  type        = number
  default     = 1024
}

variable "backend_memory" {
  description = "Fargate memory (MiB) for the backend task."
  type        = number
  default     = 2048
}

variable "frontend_cpu" {
  description = "Fargate CPU units for the nginx frontend task."
  type        = number
  default     = 256
}

variable "frontend_memory" {
  description = "Fargate memory (MiB) for the frontend task."
  type        = number
  default     = 512
}

variable "backend_desired_count" {
  description = "Number of backend tasks (>=2 for HA across AZs)."
  type        = number
  default     = 2
}

variable "frontend_desired_count" {
  description = "Number of frontend tasks."
  type        = number
  default     = 2
}

# ── app config (non-secret task env; secrets come from Secrets Manager) ──────
variable "cors_origins" {
  description = "ORACLE_CORS_ORIGINS — comma-separated exact origins (no wildcard)."
  type        = string
}

variable "app_base_url" {
  description = "ORACLE_BASE_URL — public base URL."
  type        = string
}

variable "demo_tenant_id" {
  description = "ORACLE_DEMO_TENANT_ID — tenant the seeded real leads live under."
  type        = string
  default     = "00000000-0000-0000-0000-000000000000"
}

variable "log_retention_days" {
  description = "CloudWatch log retention for app + pgaudit groups."
  type        = number
  default     = 90
}

variable "tags" {
  description = "Extra tags applied to every resource."
  type        = map(string)
  default     = {}
}
