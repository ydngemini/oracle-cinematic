# ─────────────────────────────────────────────────────────────────────────────
# AWS Observability dashboard — the Neoh-embedded copy (observability-platform/).
# A 2nd frontend service on the shared ALB, reached at var.observability_host.
#
# Routing: the existing alb.tf `api` rule (priority 10, NO host condition) already
# forwards /api/*, /ws, /auth/*, /billing/*, /health to the backend for EVERY host
# — so the dashboard's JWT login + AWS WebSocket work SAME-ORIGIN on obs_host with
# zero CORS. We only add one rule: obs_host's remaining paths → the dashboard SPA
# (otherwise they'd fall through to the default action = the main oracle-app FE).
#
# Whole feature gated on var.observability_enabled. Backend read grant: iam.tf
# (AwsObservabilityReadOnly); runtime flag: ecs.tf backend_env.
#
# OPERATOR PREREQS before this serves:
#   * ACM cert (var.acm_certificate_arn) must cover var.observability_host
#     (a *.<domain> wildcard covers it automatically; otherwise add a SAN).
#   * DNS: point var.observability_host at the ALB (aws_lb.main.dns_name).
# ─────────────────────────────────────────────────────────────────────────────

variable "observability_enabled" {
  description = "Deploy the Neoh-embedded AWS observability dashboard + grant the backend task role read-only AWS access."
  type        = bool
  default     = true
}

variable "observability_host" {
  description = "Host serving the dashboard (e.g. obs.neoh.app). Must be covered by acm_certificate_arn and resolve to the ALB."
  type        = string
  default     = "obs.neoh.app"
}

variable "observability_cpu" {
  description = "Fargate CPU units for the observability dashboard task."
  type        = number
  default     = 256
}

variable "observability_memory" {
  description = "Fargate memory (MiB) for the observability dashboard task."
  type        = number
  default     = 512
}

variable "observability_desired_count" {
  description = "Number of observability dashboard tasks."
  type        = number
  default     = 1
}

locals {
  observability_image = "${aws_ecr_repository.repo["observability"].repository_url}:${var.image_tag}"
}

resource "aws_cloudwatch_log_group" "observability" {
  count             = var.observability_enabled ? 1 : 0
  name              = "/ecs/${local.name}/observability"
  retention_in_days = var.log_retention_days
  kms_key_id        = aws_kms_key.main.arn
}

resource "aws_lb_target_group" "observability" {
  count       = var.observability_enabled ? 1 : 0
  name        = "${local.name}-obs"
  port        = 8080
  protocol    = "HTTP"
  vpc_id      = aws_vpc.main.id
  target_type = "ip"

  health_check {
    path                = "/healthz"
    matcher             = "200"
    interval            = 30
    timeout             = 5
    healthy_threshold   = 2
    unhealthy_threshold = 3
  }
}

resource "aws_ecs_task_definition" "observability" {
  count                    = var.observability_enabled ? 1 : 0
  family                   = "${local.name}-observability"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = var.observability_cpu
  memory                   = var.observability_memory
  execution_role_arn       = aws_iam_role.execution.arn
  task_role_arn            = aws_iam_role.task.arn

  container_definitions = jsonencode([{
    name         = "observability"
    image        = local.observability_image
    essential    = true
    portMappings = [{ containerPort = 8080, protocol = "tcp" }]
    logConfiguration = {
      logDriver = "awslogs"
      options = {
        "awslogs-group"         = aws_cloudwatch_log_group.observability[0].name
        "awslogs-region"        = var.aws_region
        "awslogs-stream-prefix" = "observability"
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

resource "aws_ecs_service" "observability" {
  count           = var.observability_enabled ? 1 : 0
  name            = "observability"
  cluster         = aws_ecs_cluster.main.id
  task_definition = aws_ecs_task_definition.observability[0].arn
  desired_count   = var.observability_desired_count
  launch_type     = "FARGATE"

  network_configuration {
    subnets          = aws_subnet.private[*].id
    security_groups  = [aws_security_group.ecs.id]
    assign_public_ip = false
  }

  load_balancer {
    target_group_arn = aws_lb_target_group.observability[0].arn
    container_name   = "observability"
    container_port   = 8080
  }

  depends_on = [aws_lb_listener.https]

  lifecycle { ignore_changes = [task_definition] } # CI updates the task def out of band
}

# obs_host's non-API paths → the dashboard SPA. Priority 20 is AFTER the api(10)
# and portal(11) rules, so /api,/auth,/ws,/billing,/health,/portal on obs_host
# still reach the backend; only the SPA routes land here.
resource "aws_lb_listener_rule" "observability_web" {
  count        = var.observability_enabled ? 1 : 0
  listener_arn = aws_lb_listener.https.arn
  priority     = 20

  action {
    type             = "forward"
    target_group_arn = aws_lb_target_group.observability[0].arn
  }

  condition {
    host_header {
      values = [var.observability_host]
    }
  }
}

output "observability_dashboard_url" {
  description = "AWS observability dashboard URL (once DNS for observability_host points at the ALB)."
  value       = var.observability_enabled ? "https://${var.observability_host}" : "(disabled)"
}
