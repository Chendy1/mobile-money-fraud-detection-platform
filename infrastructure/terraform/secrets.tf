# The secrets exist as empty containers. Their values are never in Terraform or its state:
# put them there yourself (aws secretsmanager put-secret-value), from the files `mmrisk.deploy init`
# made. There is deliberately no aws_secretsmanager_secret_version resource.

locals {
  secret_names = ["api_keys", "metrics_key", "tls_cert", "tls_key", "dashboard_hash"]
}

resource "aws_secretsmanager_secret" "this" {
  for_each = toset(local.secret_names)

  name                    = "${local.name}/${each.key}"
  description             = "mmrisk ${each.key} (value set outside Terraform)"
  recovery_window_in_days = var.secret_recovery_days
}
