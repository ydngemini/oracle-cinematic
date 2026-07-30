resource "aws_ecs_cluster" "main" {
  name = local.name
  setting {
    name  = "containerInsights"
    value = "enabled"
  }
}

resource "aws_ecs_cluster_capacity_providers" "main" {
  cluster_name       = aws_ecs_cluster.main.name
  capacity_providers = ["FARGATE", "FARGATE_SPOT"]
  default_capacity_provider_strategy {
    capacity_provider = "FARGATE"
    weight            = 1
  }
}

locals {
  backend_image  = "${aws_ecr_repository.repo["backend"].repository_url}:${var.image_tag}"
  frontend_image = "${aws_ecr_repository.repo["frontend"].repository_url}:${var.image_tag}"

  # Non-secret runtime env (plain task environment).
  backend_env = concat([
    { name = "ORACLE_ENV", value = var.environment },
    { name = "ORACLE_CORS_ORIGINS", value = var.cors_origins },
    { name = "ORACLE_BASE_URL", value = var.app_base_url },
    { name = "ORACLE_PUBLIC_BASE_URL", value = var.app_base_url },
    { name = "GOOGLE_OAUTH_REDIRECT_URI", value = "${var.app_base_url}/api/commands/providers/google/oauth/callback" },
    { name = "ORACLE_SES_FROM_EMAIL", value = var.ses_from_email },
    { name = "ORACLE_TWILIO_STATUS_CALLBACK", value = "${var.app_base_url}/api/commands/webhooks/twilio" },
    { name = "ORACLE_DEMO_TENANT_ID", value = var.demo_tenant_id },
    { name = "ORACLE_ENABLE_DEMO_LOGINS", value = "0" },
    # Real lead ingestion: FREE open-data firehose (51 state portals, no API key) — NOT
    # RentCast. Master switch + ingest tenant + the heavy parcel harvest. distress_scrape
    # (keyless) also runs once the master is on. See data_integrations/periodic.py.
    # Keep the durable producer alive for retention/source-health maintenance;
    # the municipal jobs themselves remain independently feature-gated below.
    { name = "ORACLE_SCHEDULER_ENABLED", value = "1" },
    { name = "ORACLE_INGEST_TENANT_ID", value = "00000000-0000-0000-0000-000000000000" },
    { name = "ORACLE_PARCEL_HARVEST_ENABLED", value = var.feature_municipal_harvests ? "1" : "0" },
    { name = "ORACLE_FEATURE_AUTOMATION", value = tostring(var.feature_automation) },
    { name = "ORACLE_FEATURE_MUNICIPAL_HARVESTS", value = tostring(var.feature_municipal_harvests) },
    { name = "ORACLE_FEATURE_PREDICTIVE_INTELLIGENCE", value = tostring(var.feature_predictive_intelligence) },
    { name = "ORACLE_FEATURE_MARKETPLACE", value = tostring(var.feature_marketplace) },
    { name = "ORACLE_FEATURE_LOCAL_MODELS", value = tostring(var.feature_local_models) },
    { name = "ORACLE_FEATURE_SPATIAL_TOURS", value = tostring(var.feature_spatial_tours) },
    { name = "ORACLE_FEATURE_CONTRACTS", value = tostring(var.feature_contracts) },
    { name = "ORACLE_FEATURE_AI_CHAT", value = tostring(var.feature_ai_chat) },
    { name = "ORACLE_RAW_SOURCE_RETENTION_DAYS", value = tostring(var.raw_source_retention_days) },
    { name = "ORACLE_CALL_TRANSCRIPT_RETENTION_DAYS", value = tostring(var.call_transcript_retention_days) },
    # Operator/admin login id (platform_admin). Passphrase is the ORACLE_ADMIN_PASSPHRASE
    # secret. NOTE: this is the ONLY login path — Neoh has no self-serve signup yet.
    { name = "ORACLE_ADMIN_ID", value = "ydnop@ydnhft.com" },
    # Stripe price for the $299/mo Swarm License (public id, not a secret). Without
    # it billing.py falls back to price_REPLACE_ME → "No such price" at checkout.
    { name = "STRIPE_PRICE_ID", value = "price_1TftozEDjW1NbBU5FaAQGPiH" },
    { name = "ORACLE_DB_HOST", value = aws_rds_cluster.aurora.endpoint },
    { name = "ORACLE_DB_PORT", value = "5432" },
    { name = "ORACLE_DB_NAME", value = var.db_name },
    { name = "ORACLE_DB_USER", value = "oracle_app_login" },
    { name = "ORACLE_DB_SSLMODE", value = "verify-full" },
    { name = "ORACLE_RDS_CA_BUNDLE", value = "/etc/ssl/certs/rds-global-bundle.pem" },
    { name = "AWS_REGION", value = var.aws_region },
    { name = "BEDROCK_REGION", value = var.aws_region },
    # Private legal contract vault (contract_vault.tf). Contracts are stored in
    # S3 with SSE-S3 and delivered through one-hour presigned URLs only.
    { name = "CONTRACT_VAULT_BUCKET", value = aws_s3_bucket.contract_vault.bucket },
    # AWS observability broadcaster (aws_observability.py). Opt-in; the loop only
    # calls AWS while a dashboard client is connected. Task role read grant: iam.tf.
    { name = "AWS_OBSERVABILITY_ENABLED", value = var.observability_enabled ? "1" : "0" },
    # GPU reconstruction + S3 splat storage (reconstruction.tf). AWS Batch is the
    # default; RunPod is an opt-in no-EC2-GPU-quota path.
    { name = "RECONSTRUCTION_PROVIDER", value = var.reconstruction_provider },
    { name = "RECON_S3_BUCKET", value = aws_s3_bucket.recon.bucket },
    ],
    var.reconstruction_provider == "aws_batch" ? [
      { name = "RECON_AWS_BATCH_QUEUE", value = aws_batch_job_queue.recon.name },
      { name = "RECON_AWS_BATCH_JOBDEF", value = aws_batch_job_definition.recon.name },
    ] : [],
    var.reconstruction_provider == "runpod" ? [
      { name = "RUNPOD_ENDPOINT_ID", value = var.runpod_endpoint_id },
      { name = "RECON_RUNPOD_TIMEOUT", value = tostring(var.recon_runpod_timeout) },
    ] : [],
    [
      { name = "ORACLE_SPLAT_STORAGE", value = "s3" },
      { name = "ORACLE_SPLAT_S3_BUCKET", value = aws_s3_bucket.recon.bucket },
      { name = "ORACLE_SPLAT_CDN_BASE", value = "https://${aws_s3_bucket.recon.bucket}.s3.${var.aws_region}.amazonaws.com" },
  ])

  # Secrets injected from Secrets Manager JSON keys, never embedded in the task
  # definition or Terraform state.
  backend_secrets = concat([
    for k in [
      "ORACLE_SECRET_KEY",
      "ORACLE_ENCRYPTION_MASTER_KEY",
      "ORACLE_ADMIN_PASSPHRASE",
      "STRIPE_SECRET_KEY",
      "STRIPE_WEBHOOK_SECRET",
      "RENTCAST_API_KEY",
      "GOOGLE_CLIENT_ID",
      "GOOGLE_CLIENT_SECRET",
      "TWILIO_ACCOUNT_SID",
      "TWILIO_AUTH_TOKEN",
      "TWILIO_FROM_NUMBER",
      "ORACLE_TWILIO_TWIML_URL",
      "ACS_CONNECTION_STRING",
      "ACS_FROM_NUMBER",
      "ORACLE_ACS_WEBHOOK_SECRET",
      "REDIS_URL",
      ] : {
      name      = k
      valueFrom = "${aws_secretsmanager_secret.app.arn}:${k}::"
    }
    ], var.reconstruction_provider == "runpod" ? [{
      name      = "RUNPOD_API_KEY"
      valueFrom = "${aws_secretsmanager_secret.app.arn}:RUNPOD_API_KEY::"
  }] : [])
}

resource "aws_ecs_task_definition" "backend" {
  family                   = "${local.name}-backend"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = var.backend_cpu
  memory                   = var.backend_memory
  execution_role_arn       = aws_iam_role.execution.arn
  task_role_arn            = aws_iam_role.task.arn

  container_definitions = jsonencode([{
    name         = "backend"
    image        = local.backend_image
    essential    = true
    environment  = local.backend_env
    secrets      = local.backend_secrets
    portMappings = [{ containerPort = 8000, protocol = "tcp" }]
    logConfiguration = {
      logDriver = "awslogs"
      options = {
        "awslogs-group"         = aws_cloudwatch_log_group.backend.name
        "awslogs-region"        = var.aws_region
        "awslogs-stream-prefix" = "backend"
      }
    }
    healthCheck = {
      command     = ["CMD-SHELL", "python -c \"import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/health',timeout=4).status==200 else 1)\""]
      interval    = 30
      timeout     = 5
      retries     = 3
      startPeriod = 60
    }
  }])

  lifecycle {
    precondition {
      condition = (
        var.reconstruction_provider != "runpod" ||
        can(regex("^[A-Za-z0-9_-]{3,128}$", var.runpod_endpoint_id))
      )
      error_message = "runpod_endpoint_id is required when reconstruction_provider is runpod."
    }
  }
}

resource "aws_ecs_task_definition" "frontend" {
  family                   = "${local.name}-frontend"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = var.frontend_cpu
  memory                   = var.frontend_memory
  execution_role_arn       = aws_iam_role.execution.arn
  task_role_arn            = aws_iam_role.task.arn

  container_definitions = jsonencode([{
    name         = "frontend"
    image        = local.frontend_image
    essential    = true
    portMappings = [{ containerPort = 8080, protocol = "tcp" }]
    logConfiguration = {
      logDriver = "awslogs"
      options = {
        "awslogs-group"         = aws_cloudwatch_log_group.frontend.name
        "awslogs-region"        = var.aws_region
        "awslogs-stream-prefix" = "frontend"
      }
    }
    healthCheck = {
      command     = ["CMD-SHELL", "wget -qO- http://127.0.0.1:8080/healthz >/dev/null 2>&1 || exit 1"]
      interval    = 30
      timeout     = 5
      retries     = 3
      startPeriod = 15
    }
  }])
}

resource "aws_ecs_service" "backend" {
  name            = "backend"
  cluster         = aws_ecs_cluster.main.id
  task_definition = aws_ecs_task_definition.backend.arn
  desired_count   = var.backend_desired_count
  launch_type     = "FARGATE"

  # Give migrations time + the IAM login role to exist before health flaps.
  health_check_grace_period_seconds = 120

  deployment_circuit_breaker {
    enable   = true
    rollback = true
  }

  network_configuration {
    subnets          = aws_subnet.private[*].id
    security_groups  = [aws_security_group.ecs.id]
    assign_public_ip = false
  }

  load_balancer {
    target_group_arn = aws_lb_target_group.backend.arn
    container_name   = "backend"
    container_port   = 8000
  }

  depends_on = [aws_lb_listener.https]

  lifecycle { ignore_changes = [task_definition] } # CI updates the task def out of band
}

resource "aws_ecs_service" "frontend" {
  name            = "frontend"
  cluster         = aws_ecs_cluster.main.id
  task_definition = aws_ecs_task_definition.frontend.arn
  desired_count   = var.frontend_desired_count
  launch_type     = "FARGATE"

  deployment_circuit_breaker {
    enable   = true
    rollback = true
  }

  network_configuration {
    subnets          = aws_subnet.private[*].id
    security_groups  = [aws_security_group.ecs.id]
    assign_public_ip = false
  }

  load_balancer {
    target_group_arn = aws_lb_target_group.frontend.arn
    container_name   = "frontend"
    container_port   = 8080
  }

  depends_on = [aws_lb_listener.https]

  lifecycle { ignore_changes = [task_definition] }
}
