# Neoh GPU reconstruction — AWS Batch (SPOT g5, scales to zero) runs the
# COLMAP + 3DGS pipeline per job and writes the .splat to S3; the app submits +
# polls via the AwsBatchProvider. One bucket holds capture inputs, job outputs,
# and the canonical served splats (splats/* is public-read).

variable "recon_image_tag" {
  description = "Tag of the reconstruction GPU image pushed to the recon ECR repo."
  type        = string
  default     = "v1"
}

variable "recon_max_vcpus" {
  description = "Max vCPUs the Batch SPOT GPU compute environment may scale to."
  type        = number
  default     = 32
}

# ── S3 bucket (inputs + outputs + served splats) ────────────────────────────
resource "aws_s3_bucket" "recon" {
  bucket = "${local.name}-recon-${local.account_id}"
}

resource "aws_s3_bucket_public_access_block" "recon" {
  bucket                  = aws_s3_bucket.recon.id
  block_public_acls       = true
  ignore_public_acls      = true
  block_public_policy     = false # allow the splats/* public-read policy below
  restrict_public_buckets = false
}

# Only the served splats are public; capture inputs + job outputs stay private.
data "aws_iam_policy_document" "recon_bucket" {
  statement {
    sid       = "PublicReadSplats"
    effect    = "Allow"
    actions   = ["s3:GetObject"]
    resources = ["${aws_s3_bucket.recon.arn}/splats/*"]
    principals {
      type        = "*"
      identifiers = ["*"]
    }
  }
}

resource "aws_s3_bucket_policy" "recon" {
  bucket     = aws_s3_bucket.recon.id
  policy     = data.aws_iam_policy_document.recon_bucket.json
  depends_on = [aws_s3_bucket_public_access_block.recon]
}

# Capture inputs + intermediate outputs are disposable — expire them.
resource "aws_s3_bucket_lifecycle_configuration" "recon" {
  bucket = aws_s3_bucket.recon.id
  rule {
    id     = "expire-inputs"
    status = "Enabled"
    filter { prefix = "recon-inputs/" }
    expiration { days = 7 }
  }
  rule {
    id     = "expire-outputs"
    status = "Enabled"
    filter { prefix = "recon-outputs/" }
    expiration { days = 7 }
  }
}

# ── ECR repo for the GPU job image ──────────────────────────────────────────
resource "aws_ecr_repository" "recon" {
  name                 = "${var.project}/reconstruction"
  image_tag_mutability = "MUTABLE"
  image_scanning_configuration { scan_on_push = true }
}

resource "aws_ecr_lifecycle_policy" "recon" {
  repository = aws_ecr_repository.recon.name
  policy = jsonencode({
    rules = [{
      rulePriority = 1
      description  = "Keep last 5 images"
      selection    = { tagStatus = "any", countType = "imageCountMoreThan", countNumber = 5 }
      action       = { type = "expire" }
    }]
  })
}

# ── CloudWatch log group for job output ─────────────────────────────────────
resource "aws_cloudwatch_log_group" "recon" {
  name              = "/batch/${local.name}-recon"
  retention_in_days = var.log_retention_days
}

# ── IAM ─────────────────────────────────────────────────────────────────────
# Batch service role.
data "aws_iam_policy_document" "batch_assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["batch.amazonaws.com"]
    }
  }
}
resource "aws_iam_role" "batch_service" {
  name               = "${local.name}-batch-service"
  assume_role_policy = data.aws_iam_policy_document.batch_assume.json
}
resource "aws_iam_role_policy_attachment" "batch_service" {
  role       = aws_iam_role.batch_service.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AWSBatchServiceRole"
}

# Spot fleet role (for SPOT compute resources).
data "aws_iam_policy_document" "spotfleet_assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["spotfleet.amazonaws.com"]
    }
  }
}
resource "aws_iam_role" "recon_spot_fleet" {
  name               = "${local.name}-recon-spotfleet"
  assume_role_policy = data.aws_iam_policy_document.spotfleet_assume.json
}
resource "aws_iam_role_policy_attachment" "recon_spot_fleet" {
  role       = aws_iam_role.recon_spot_fleet.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AmazonEC2SpotFleetTaggingRole"
}

# ECS instance role for the Batch compute-environment EC2 instances.
data "aws_iam_policy_document" "ec2_assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["ec2.amazonaws.com"]
    }
  }
}
resource "aws_iam_role" "recon_instance" {
  name               = "${local.name}-recon-instance"
  assume_role_policy = data.aws_iam_policy_document.ec2_assume.json
}
resource "aws_iam_role_policy_attachment" "recon_instance_ecs" {
  role       = aws_iam_role.recon_instance.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AmazonEC2ContainerServiceforEC2Role"
}
resource "aws_iam_instance_profile" "recon_instance" {
  name = "${local.name}-recon-instance"
  role = aws_iam_role.recon_instance.name
}

# Job execution role — ECR pull + log writes (used by ECS to start the container).
resource "aws_iam_role" "recon_exec" {
  name               = "${local.name}-recon-exec"
  assume_role_policy = data.aws_iam_policy_document.ecs_assume.json # reused from iam.tf
}
resource "aws_iam_role_policy_attachment" "recon_exec" {
  role       = aws_iam_role.recon_exec.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy"
}

# Job role — the container's own creds: read inputs, write outputs + served splats.
resource "aws_iam_role" "recon_job" {
  name               = "${local.name}-recon-job"
  assume_role_policy = data.aws_iam_policy_document.ecs_assume.json
}
data "aws_iam_policy_document" "recon_job" {
  statement {
    actions   = ["s3:GetObject", "s3:PutObject", "s3:ListBucket"]
    resources = [aws_s3_bucket.recon.arn, "${aws_s3_bucket.recon.arn}/*"]
  }
}
resource "aws_iam_role_policy" "recon_job" {
  name   = "${local.name}-recon-job"
  role   = aws_iam_role.recon_job.id
  policy = data.aws_iam_policy_document.recon_job.json
}

# ── Compute environment (SPOT GPU, scale-to-zero) + queue + job def ─────────
resource "aws_security_group" "recon" {
  name_prefix = "${local.name}-recon-"
  description = "Batch GPU reconstruction instances"
  vpc_id      = aws_vpc.main.id
  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }
  lifecycle { create_before_destroy = true }
  tags = { Name = "${local.name}-recon" }
}

resource "aws_batch_compute_environment" "recon" {
  compute_environment_name = "${local.name}-recon"
  type                     = "MANAGED"
  service_role             = aws_iam_role.batch_service.arn

  compute_resources {
    type                = "SPOT"
    allocation_strategy = "SPOT_CAPACITY_OPTIMIZED"
    min_vcpus           = 0
    max_vcpus           = var.recon_max_vcpus
    instance_type       = ["g5.xlarge", "g4dn.xlarge"]
    subnets             = aws_subnet.private[*].id
    security_group_ids  = [aws_security_group.recon.id]
    instance_role       = aws_iam_instance_profile.recon_instance.arn
    spot_iam_fleet_role = aws_iam_role.recon_spot_fleet.arn
    ec2_configuration {
      # AWS Batch rejected ECS_AL2_NVIDIA outright on 2026-08-28:
      # "Amazon Linux 2 is end-of-life." Same shape as Aurora withdrawing 16.4 —
      # infra pinned to an AWS default that AWS has since retired, which only
      # surfaces on a fresh create, never on an existing stack.
      image_type = "ECS_AL2023_NVIDIA" # GPU-optimized ECS AMI (AL2 is EOL)
    }
    tags = { Name = "${local.name}-recon" }
  }

  depends_on = [aws_iam_role_policy_attachment.batch_service]
}

resource "aws_batch_job_queue" "recon" {
  name     = "${local.name}-recon"
  state    = "ENABLED"
  priority = 1
  compute_environment_order {
    order               = 1
    compute_environment = aws_batch_compute_environment.recon.arn
  }
}

resource "aws_batch_job_definition" "recon" {
  name                  = "${local.name}-recon"
  type                  = "container"
  platform_capabilities = ["EC2"]

  container_properties = jsonencode({
    image            = "${aws_ecr_repository.recon.repository_url}:${var.recon_image_tag}"
    jobRoleArn       = aws_iam_role.recon_job.arn
    executionRoleArn = aws_iam_role.recon_exec.arn
    resourceRequirements = [
      { type = "GPU", value = "1" },
      { type = "VCPU", value = "4" },
      { type = "MEMORY", value = "16384" },
    ]
    environment = [
      { name = "RECON_ITERS", value = "7000" },
    ]
    logConfiguration = {
      logDriver = "awslogs"
      options = {
        "awslogs-group"         = aws_cloudwatch_log_group.recon.name
        "awslogs-region"        = var.aws_region
        "awslogs-stream-prefix" = "recon"
      }
    }
  })

  timeout {
    attempt_duration_seconds = 5400 # 90 min hard cap
  }
  retry_strategy {
    attempts = 1
  }
}

# ── Let the app (ECS task role from iam.tf) submit jobs + use the bucket ─────
data "aws_iam_policy_document" "app_recon" {
  statement {
    sid       = "SubmitReconJobs"
    actions   = ["batch:SubmitJob"]
    resources = [aws_batch_job_queue.recon.arn, aws_batch_job_definition.recon.arn]
  }
  statement {
    sid       = "DescribeReconJobs"
    actions   = ["batch:DescribeJobs", "batch:TerminateJob"]
    resources = ["*"] # these actions don't support resource-level scoping
  }
  statement {
    sid       = "ReconBucketRW"
    actions   = ["s3:GetObject", "s3:PutObject", "s3:ListBucket"]
    resources = [aws_s3_bucket.recon.arn, "${aws_s3_bucket.recon.arn}/*"]
  }
}
resource "aws_iam_role_policy" "app_recon" {
  name   = "${local.name}-app-recon"
  role   = aws_iam_role.task.id # defined in iam.tf
  policy = data.aws_iam_policy_document.app_recon.json
}

# ── Outputs → map straight onto the backend task env (ecs.tf) ───────────────
output "recon_s3_bucket" {
  description = "RECON_S3_BUCKET + ORACLE_SPLAT_S3_BUCKET"
  value       = aws_s3_bucket.recon.bucket
}
output "recon_batch_queue" {
  description = "RECON_AWS_BATCH_QUEUE"
  value       = aws_batch_job_queue.recon.name
}
output "recon_batch_jobdef" {
  description = "RECON_AWS_BATCH_JOBDEF"
  value       = aws_batch_job_definition.recon.name
}
output "recon_ecr_url" {
  description = "Push the reconstruction GPU image here (tag = var.recon_image_tag)."
  value       = aws_ecr_repository.recon.repository_url
}
output "recon_splat_cdn_base" {
  description = "ORACLE_SPLAT_CDN_BASE — public base for served splats (front with CloudFront for prod)."
  value       = "https://${aws_s3_bucket.recon.bucket}.s3.${var.aws_region}.amazonaws.com"
}
