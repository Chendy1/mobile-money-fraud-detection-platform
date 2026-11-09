output "ecr_repositories" {
  description = "Where to push the images."
  value       = { for k, r in aws_ecr_repository.this : k => r.repository_url }
}

output "bucket_names" {
  value = { for k, b in aws_s3_bucket.this : k => b.id }
}

output "secret_names" {
  description = "Put each value here yourself; Terraform never holds them."
  value       = { for k, s in aws_secretsmanager_secret.this : k => s.name }
}

output "cluster_name" {
  value = aws_ecs_cluster.main.name
}

output "service_names" {
  value = { api = aws_ecs_service.api.name, dashboard = aws_ecs_service.dashboard.name }
}
