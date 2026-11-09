# Task definitions mirror docker-compose.prod.yml: non-root, read-only root filesystem, no
# capabilities, fixed size, TLS health check. Services start at desired_count = 0.
#
# KNOWN GAP (docs/aws-design.md section 5): the images do not yet contain /app/aws-entrypoint.sh,
# which must (1) write the Secrets Manager values to files under /scratch/secrets and (2) copy the
# registry and lake from S3 to /scratch. Until it exists a started task fails loudly instead of
# running without its secrets. That is intended; do not raise desired_count before it is built.

locals {
  scratch_mounts = [
    { sourceVolume = "scratch", containerPath = "/scratch", readOnly = false },
    { sourceVolume = "tmp", containerPath = "/tmp", readOnly = false },
  ]

  api_health = "python -c \"import ssl,urllib.request as u; u.urlopen('https://127.0.0.1:8443/ready', context=ssl.create_default_context(cafile='/scratch/secrets/cert.pem'), timeout=3)\""
}

resource "aws_ecs_cluster" "main" {
  name = "${local.name}-cluster"

  setting {
    name  = "containerInsights"
    value = "disabled"
  }
}

resource "aws_ecs_task_definition" "api" {
  family                   = "${local.name}-api"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = var.api_cpu
  memory                   = var.api_memory
  execution_role_arn       = aws_iam_role.execution.arn
  task_role_arn            = aws_iam_role.task.arn

  runtime_platform {
    operating_system_family = "LINUX"
    cpu_architecture        = "X86_64"
  }

  volume {
    name = "scratch"
  }

  volume {
    name = "tmp"
  }

  container_definitions = jsonencode([{
    name                   = "api"
    image                  = "${aws_ecr_repository.this["api"].repository_url}:${var.api_image_tag}"
    essential              = true
    user                   = "10001"
    readonlyRootFilesystem = true
    entryPoint             = ["/app/aws-entrypoint.sh"]
    command = [
      "python", "-m", "mmrisk.api", "serve",
      "--host", "0.0.0.0", "--port", "8443",
      "--model-dir", "/scratch/registry", "--lake", "/scratch/lake",
      "--auth-keys", "/scratch/secrets/api_keys.json",
      "--tls-cert", "/scratch/secrets/cert.pem", "--tls-key", "/scratch/secrets/key.pem",
      "--audit-log", "/scratch/audit/audit.jsonl", "--allow-external",
    ]
    portMappings = [{ containerPort = 8443, protocol = "tcp" }]
    linuxParameters = {
      initProcessEnabled = true
      capabilities       = { drop = ["ALL"] }
    }
    mountPoints = local.scratch_mounts
    environment = [
      { name = "MMRISK_REGISTRY_BUCKET", value = aws_s3_bucket.this["registry"].id },
      { name = "MMRISK_LAKE_BUCKET", value = aws_s3_bucket.this["lake"].id },
    ]
    secrets = [
      { name = "MMRISK_SECRET_API_KEYS", valueFrom = aws_secretsmanager_secret.this["api_keys"].arn },
      { name = "MMRISK_SECRET_TLS_CERT", valueFrom = aws_secretsmanager_secret.this["tls_cert"].arn },
      { name = "MMRISK_SECRET_TLS_KEY", valueFrom = aws_secretsmanager_secret.this["tls_key"].arn },
    ]
    healthCheck = {
      command     = ["CMD-SHELL", local.api_health]
      interval    = 30
      timeout     = 5
      retries     = 3
      startPeriod = 180
    }
    logConfiguration = {
      logDriver = "awslogs"
      options = {
        "awslogs-group"         = aws_cloudwatch_log_group.this["api"].name
        "awslogs-region"        = var.region
        "awslogs-stream-prefix" = "api"
      }
    }
  }])
}

resource "aws_ecs_task_definition" "dashboard" {
  family                   = "${local.name}-dashboard"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = var.dashboard_cpu
  memory                   = var.dashboard_memory
  execution_role_arn       = aws_iam_role.execution.arn
  task_role_arn            = aws_iam_role.task.arn

  runtime_platform {
    operating_system_family = "LINUX"
    cpu_architecture        = "X86_64"
  }

  volume {
    name = "scratch"
  }

  volume {
    name = "tmp"
  }

  container_definitions = jsonencode([{
    name                   = "dashboard"
    image                  = "${aws_ecr_repository.this["dashboard"].repository_url}:${var.dashboard_image_tag}"
    essential              = true
    user                   = "10001"
    readonlyRootFilesystem = true
    entryPoint             = ["/app/aws-entrypoint.sh"]
    command                = ["python", "-m", "mmrisk.dashboard", "--password-file", "/scratch/secrets/dashboard.hash"]
    portMappings           = [{ containerPort = 8501, protocol = "tcp" }]
    linuxParameters = {
      initProcessEnabled = true
      capabilities       = { drop = ["ALL"] }
    }
    mountPoints = local.scratch_mounts
    environment = [
      { name = "MMRISK_REGISTRY_BUCKET", value = aws_s3_bucket.this["registry"].id },
      { name = "MMRISK_LAKE_BUCKET", value = aws_s3_bucket.this["lake"].id },
    ]
    secrets = [
      { name = "MMRISK_SECRET_DASHBOARD_HASH", valueFrom = aws_secretsmanager_secret.this["dashboard_hash"].arn },
    ]
    logConfiguration = {
      logDriver = "awslogs"
      options = {
        "awslogs-group"         = aws_cloudwatch_log_group.this["dashboard"].name
        "awslogs-region"        = var.region
        "awslogs-stream-prefix" = "dashboard"
      }
    }
  }])
}

resource "aws_ecs_service" "api" {
  name                   = "${local.name}-api"
  cluster                = aws_ecs_cluster.main.id
  task_definition        = aws_ecs_task_definition.api.arn
  desired_count          = var.desired_count
  launch_type            = "FARGATE"
  enable_execute_command = var.enable_execute_command

  network_configuration {
    subnets          = [aws_subnet.private.id]
    security_groups  = [aws_security_group.tasks.id]
    assign_public_ip = false
  }

  deployment_circuit_breaker {
    enable   = true
    rollback = true
  }
}

resource "aws_ecs_service" "dashboard" {
  name                   = "${local.name}-dashboard"
  cluster                = aws_ecs_cluster.main.id
  task_definition        = aws_ecs_task_definition.dashboard.arn
  desired_count          = var.desired_count
  launch_type            = "FARGATE"
  enable_execute_command = var.enable_execute_command

  network_configuration {
    subnets          = [aws_subnet.private.id]
    security_groups  = [aws_security_group.tasks.id]
    assign_public_ip = false
  }

  deployment_circuit_breaker {
    enable   = true
    rollback = true
  }
}
