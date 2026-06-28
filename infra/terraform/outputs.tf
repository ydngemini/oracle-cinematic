output "alb_dns_name" {
  description = "Point your app DNS (CNAME/ALIAS) at this."
  value       = aws_lb.main.dns_name
}

output "alb_zone_id" {
  description = "Hosted-zone id for a Route53 ALIAS record to the ALB."
  value       = aws_lb.main.zone_id
}

output "ecr_backend_url" {
  description = "Push the backend image here (tag = var.image_tag)."
  value       = aws_ecr_repository.repo["backend"].repository_url
}

output "ecr_frontend_url" {
  description = "Push the frontend image here (tag = var.image_tag)."
  value       = aws_ecr_repository.repo["frontend"].repository_url
}

output "db_writer_endpoint" {
  description = "Aurora writer endpoint → ORACLE_DB_HOST."
  value       = aws_rds_cluster.aurora.endpoint
}

output "db_reader_endpoint" {
  description = "Aurora reader endpoint (read replicas)."
  value       = aws_rds_cluster.aurora.reader_endpoint
}

output "db_master_secret_arn" {
  description = "RDS-managed master credential (for provisioning + running migrations)."
  value       = aws_rds_cluster.aurora.master_user_secret[0].secret_arn
}

output "app_secret_name" {
  description = "Populate this Secrets Manager secret with the real runtime secrets before the service stabilizes."
  value       = aws_secretsmanager_secret.app.name
}

output "ecs_cluster" {
  description = "ECS cluster name."
  value       = aws_ecs_cluster.main.name
}
