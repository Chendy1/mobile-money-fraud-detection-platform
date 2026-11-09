resource "aws_cloudwatch_log_group" "this" {
  for_each = toset(["api", "dashboard"])

  name              = "/${local.name}/${each.key}"
  retention_in_days = var.log_retention_days
}
