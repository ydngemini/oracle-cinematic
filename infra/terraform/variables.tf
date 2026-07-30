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

variable "ses_from_email" {
  description = "Verified SES sender used for approved outbound email commands."
  type        = string
  default     = ""
}

variable "demo_tenant_id" {
  description = "ORACLE_DEMO_TENANT_ID — tenant the seeded real leads live under."
  type        = string
  default     = "00000000-0000-0000-0000-000000000000"
}

# ── staged platform rollout -------------------------------------------------
# High-risk capabilities ship dark in production. Enable them independently
# only after migrations, smoke tests, and the corresponding provider/licensing
# review are green. Local compose explicitly enables the development surface.
variable "feature_automation" {
  description = "Enable EMAIL/CALL/CALENDAR command drafting and approved execution."
  type        = bool
  default     = false
}

variable "feature_municipal_harvests" {
  description = "Enable durable municipal/parcel harvest scheduling and controls."
  type        = bool
  default     = false
}

variable "feature_predictive_intelligence" {
  description = "Enable predictive, title, zoning, and underwriting intelligence APIs."
  type        = bool
  default     = false
}

variable "feature_marketplace" {
  description = "Enable internal disposition marketplace workflows."
  type        = bool
  default     = false
}

variable "feature_local_models" {
  description = "Enable opt-in local model training and hot-swap controls."
  type        = bool
  default     = false
}

variable "feature_spatial_tours" {
  description = "Enable generative spatial tour jobs and variants."
  type        = bool
  default     = false
}

variable "feature_contracts" {
  description = "Enable approved-template legal document generation and vaulting."
  type        = bool
  default     = false
}

variable "feature_ai_chat" {
  description = "Enable the tenant-isolated Personal AI chat rail and durable response worker."
  type        = bool
  default     = false
}

variable "qwen_realtime_enabled" {
  description = "Route ACS calls through Alibaba Qwen3.5 Omni Flash Realtime."
  type        = bool
  default     = false
}

variable "twilio_qwen_realtime_enabled" {
  description = "Route approved Twilio calls through bidirectional Media Streams and Qwen realtime."
  type        = bool
  default     = false
}

variable "acs_resource_id" {
  description = "Full Azure Communication Services resource ID used as the signed media WebSocket JWT audience."
  type        = string
  default     = ""
}

variable "qwen_realtime_workspace_id" {
  description = "Alibaba Model Studio workspace ID (Singapore or Beijing)."
  type        = string
  default     = ""
}

variable "qwen_realtime_region" {
  description = "Alibaba Model Studio region: intl (Singapore) or cn (Beijing)."
  type        = string
  default     = "intl"

  validation {
    condition     = contains(["intl", "cn"], var.qwen_realtime_region)
    error_message = "qwen_realtime_region must be intl or cn."
  }
}

variable "qwen_realtime_model" {
  description = "Qwen realtime model ID used by the ACS and Twilio media bridges."
  type        = string
  default     = "qwen3.5-omni-flash-realtime"
}

variable "qwen_realtime_voice" {
  description = "Qwen realtime voice name."
  type        = string
  default     = "Ethan"
}

variable "raw_source_retention_days" {
  description = "Maximum retention for raw licensed/public source payloads before redaction."
  type        = number
  default     = 730
  validation {
    condition     = var.raw_source_retention_days >= 1 && var.raw_source_retention_days <= 3650
    error_message = "raw_source_retention_days must be between 1 and 3650."
  }
}

variable "call_transcript_retention_days" {
  description = "Days to retain consented transcript excerpts before automated redaction."
  type        = number
  default     = 365
  validation {
    condition     = var.call_transcript_retention_days >= 1 && var.call_transcript_retention_days <= 3650
    error_message = "call_transcript_retention_days must be between 1 and 3650."
  }
}

variable "log_retention_days" {
  description = "CloudWatch log retention for app + pgaudit groups."
  type        = number
  default     = 90
}

# ── reconstruction backend ──────────────────────────────────────────────────
variable "reconstruction_provider" {
  description = "GPU reconstruction backend for walkable tours: aws_batch (default) or runpod."
  type        = string
  default     = "aws_batch"

  validation {
    condition     = contains(["aws_batch", "runpod"], var.reconstruction_provider)
    error_message = "reconstruction_provider must be one of: aws_batch, runpod."
  }
}

variable "runpod_endpoint_id" {
  description = "RunPod Serverless endpoint ID when reconstruction_provider = runpod."
  type        = string
  default     = ""

  validation {
    condition = (
      var.runpod_endpoint_id == "" ||
      can(regex("^[A-Za-z0-9_-]{3,128}$", var.runpod_endpoint_id))
    )
    error_message = "runpod_endpoint_id must be 3-128 letters, digits, underscores, or hyphens."
  }
}

variable "recon_runpod_timeout" {
  description = "RunPod reconstruction timeout in seconds."
  type        = number
  default     = 3600

  validation {
    condition = (
      var.recon_runpod_timeout == floor(var.recon_runpod_timeout) &&
      var.recon_runpod_timeout >= 60 &&
      var.recon_runpod_timeout <= 7200
    )
    error_message = "recon_runpod_timeout must be a whole number from 60 through 7200."
  }
}

variable "tags" {
  description = "Extra tags applied to every resource."
  type        = map(string)
  default     = {}
}
