# One VPC, one private subnet, no internet gateway, no NAT gateway, no public IPs.
# Tasks reach AWS services only through endpoints (option B in docs/aws-design.md section 4).

locals {
  name = var.project

  interface_endpoints = concat(
    ["ecr.api", "ecr.dkr", "secretsmanager", "logs"],
    var.enable_execute_command ? ["ssmmessages"] : []
  )
}

resource "aws_vpc" "main" {
  cidr_block           = var.vpc_cidr
  enable_dns_support   = true
  enable_dns_hostnames = true

  tags = { Name = "${local.name}-vpc" }
}

resource "aws_subnet" "private" {
  vpc_id                  = aws_vpc.main.id
  cidr_block              = var.subnet_cidr
  availability_zone       = var.availability_zone
  map_public_ip_on_launch = false

  tags = { Name = "${local.name}-private" }
}

resource "aws_route_table" "private" {
  vpc_id = aws_vpc.main.id

  tags = { Name = "${local.name}-private" }
}

resource "aws_route_table_association" "private" {
  subnet_id      = aws_subnet.private.id
  route_table_id = aws_route_table.private.id
}

# Tasks: no inbound rule at all. Outbound only to the endpoints and to S3.
resource "aws_security_group" "tasks" {
  name        = "${local.name}-tasks"
  description = "ECS tasks: no inbound; outbound to the VPC endpoints and S3 only"
  vpc_id      = aws_vpc.main.id

  tags = { Name = "${local.name}-tasks" }
}

resource "aws_security_group" "endpoints" {
  name        = "${local.name}-endpoints"
  description = "Interface endpoints: HTTPS from the tasks only"
  vpc_id      = aws_vpc.main.id

  tags = { Name = "${local.name}-endpoints" }
}

resource "aws_vpc_security_group_ingress_rule" "endpoints_from_tasks" {
  security_group_id            = aws_security_group.endpoints.id
  referenced_security_group_id = aws_security_group.tasks.id
  ip_protocol                  = "tcp"
  from_port                    = 443
  to_port                      = 443
  description                  = "HTTPS from the tasks"
}

resource "aws_vpc_security_group_egress_rule" "tasks_to_endpoints" {
  security_group_id            = aws_security_group.tasks.id
  referenced_security_group_id = aws_security_group.endpoints.id
  ip_protocol                  = "tcp"
  from_port                    = 443
  to_port                      = 443
  description                  = "HTTPS to the interface endpoints"
}

resource "aws_vpc_security_group_egress_rule" "tasks_to_s3" {
  security_group_id = aws_security_group.tasks.id
  prefix_list_id    = aws_vpc_endpoint.s3.prefix_list_id
  ip_protocol       = "tcp"
  from_port         = 443
  to_port           = 443
  description       = "HTTPS to S3 through the gateway endpoint"
}

resource "aws_vpc_endpoint" "s3" {
  vpc_id            = aws_vpc.main.id
  service_name      = "com.amazonaws.${var.region}.s3"
  vpc_endpoint_type = "Gateway"
  route_table_ids   = [aws_route_table.private.id]

  tags = { Name = "${local.name}-s3" }
}

resource "aws_vpc_endpoint" "interface" {
  for_each = toset(local.interface_endpoints)

  vpc_id              = aws_vpc.main.id
  service_name        = "com.amazonaws.${var.region}.${each.key}"
  vpc_endpoint_type   = "Interface"
  subnet_ids          = [aws_subnet.private.id]
  security_group_ids  = [aws_security_group.endpoints.id]
  private_dns_enabled = true

  tags = { Name = "${local.name}-${each.key}" }
}
