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
  backend_env = [
    { name = "ORACLE_ENV", value = var.environment },
    { name = "ORACLE_CORS_ORIGINS", value = var.cors_origins },
    { name = "ORACLE_BASE_URL", value = var.app_base_url },
    { name = "ORACLE_DEMO_TENANT_ID", value = var.demo_tenant_id },
    { name = "ORACLE_ENABLE_DEMO_LOGINS", value = "0" },
    # Operator/admin login id (platform_admin). Passphrase is the ORACLE_ADMIN_PASSPHRASE
    # secret. NOTE: this is the ONLY login path — Neoh has no self-serve signup yet.
    { name = "ORACLE_ADMIN_ID", value = "ydnop@ydnhft.com" },
    { name = "ORACLE_DB_HOST", value = aws_rds_cluster.aurora.endpoint },
    { name = "ORACLE_DB_PORT", value = "5432" },
    { name = "ORACLE_DB_NAME", value = var.db_name },
    { name = "ORACLE_DB_USER", value = "oracle_app_login" },
    { name = "ORACLE_DB_SSLMODE", value = "verify-full" },
    { name = "ORACLE_RDS_CA_BUNDLE", value = "/etc/ssl/certs/rds-global-bundle.pem" },
    { name = "AWS_REGION", value = var.aws_region },
    { name = "BEDROCK_REGION", value = var.aws_region },
    # GPU reconstruction via AWS Batch + S3 splat storage (reconstruction.tf).
    { name = "RECONSTRUCTION_PROVIDER", value = "aws_batch" },
    { name = "RECON_S3_BUCKET", value = aws_s3_bucket.recon.bucket },
    { name = "RECON_AWS_BATCH_QUEUE", value = aws_batch_job_queue.recon.name },
    { name = "RECON_AWS_BATCH_JOBDEF", value = aws_batch_job_definition.recon.name },
    { name = "ORACLE_SPLAT_STORAGE", value = "s3" },
    { name = "ORACLE_SPLAT_S3_BUCKET", value = aws_s3_bucket.recon.bucket },
    { name = "ORACLE_SPLAT_CDN_BASE", value = "https://${aws_s3_bucket.recon.bucket}.s3.${var.aws_region}.amazonaws.com" },
  ]

  # Secrets injected from Secrets Manager JSON keys (never in plaintext env/state).
  backend_secrets = [
    for k in [
      "ORACLE_SECRET_KEY",
      "ORACLE_ENCRYPTION_MASTER_KEY",
      "ORACLE_ADMIN_PASSPHRASE",
      "STRIPE_SECRET_KEY",
      "STRIPE_WEBHOOK_SECRET",
      "RENTCAST_API_KEY",
      ] : {
      name      = k
      valueFrom = "${aws_secretsmanager_secret.app.arn}:${k}::"
    }
  ]
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
