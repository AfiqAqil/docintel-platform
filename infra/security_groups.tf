# ---------------------------------------------------------------------------------------
# Security groups
#
# Every inbound rule names exactly one source, and that source is another security group
# wherever possible, never a CIDR. Rules are separate resources so two groups can reference
# each other without a dependency cycle.
# ---------------------------------------------------------------------------------------
resource "aws_security_group" "alb" {
  name_prefix = "${local.name}-alb-"
  description = "Load balancer: allowlisted clients in, frontend out"
  vpc_id      = aws_vpc.main.id
  tags        = { Name = "${local.name}-alb" }
  lifecycle { create_before_destroy = true }
}

resource "aws_security_group" "frontend" {
  name_prefix = "${local.name}-frontend-"
  description = "Frontend tasks: load balancer in, API and AWS endpoints out"
  vpc_id      = aws_vpc.main.id
  tags        = { Name = "${local.name}-frontend" }
  lifecycle { create_before_destroy = true }
}

resource "aws_security_group" "backend" {
  name_prefix = "${local.name}-backend-"
  description = "API tasks: frontend in, database and AWS endpoints out"
  vpc_id      = aws_vpc.main.id
  tags        = { Name = "${local.name}-backend" }
  lifecycle { create_before_destroy = true }
}

resource "aws_security_group" "worker" {
  name_prefix = "${local.name}-worker-"
  description = "Worker tasks: nothing in. Database and HTTPS out"
  vpc_id      = aws_vpc.main.id
  tags        = { Name = "${local.name}-worker" }
  lifecycle { create_before_destroy = true }
}

resource "aws_security_group" "rds" {
  name_prefix = "${local.name}-rds-"
  description = "Database: API and worker in, nothing out"
  vpc_id      = aws_vpc.main.id
  tags        = { Name = "${local.name}-rds" }
  lifecycle { create_before_destroy = true }
}

resource "aws_security_group" "endpoints" {
  name_prefix = "${local.name}-endpoints-"
  description = "Interface endpoints: HTTPS in from the three task groups"
  vpc_id      = aws_vpc.main.id
  tags        = { Name = "${local.name}-endpoints" }
  lifecycle { create_before_destroy = true }
}

# --- load balancer ---------------------------------------------------------------------
resource "aws_vpc_security_group_ingress_rule" "alb_from_clients" {
  for_each = toset(var.allowed_cidrs)

  security_group_id = aws_security_group.alb.id
  description       = "HTTP from an allowlisted client"
  cidr_ipv4         = each.value
  ip_protocol       = "tcp"
  from_port         = 80
  to_port           = 80
}

resource "aws_vpc_security_group_egress_rule" "alb_to_frontend" {
  security_group_id            = aws_security_group.alb.id
  description                  = "Forward to frontend tasks"
  referenced_security_group_id = aws_security_group.frontend.id
  ip_protocol                  = "tcp"
  from_port                    = 8080
  to_port                      = 8080
}

# --- frontend --------------------------------------------------------------------------
resource "aws_vpc_security_group_ingress_rule" "frontend_from_alb" {
  security_group_id            = aws_security_group.frontend.id
  description                  = "From the load balancer"
  referenced_security_group_id = aws_security_group.alb.id
  ip_protocol                  = "tcp"
  from_port                    = 8080
  to_port                      = 8080
}

resource "aws_vpc_security_group_egress_rule" "frontend_to_backend" {
  security_group_id            = aws_security_group.frontend.id
  description                  = "Proxy /api to the Backend API"
  referenced_security_group_id = aws_security_group.backend.id
  ip_protocol                  = "tcp"
  from_port                    = 8000
  to_port                      = 8000
}

# --- backend ---------------------------------------------------------------------------
resource "aws_vpc_security_group_ingress_rule" "backend_from_frontend" {
  security_group_id            = aws_security_group.backend.id
  description                  = "From frontend tasks only"
  referenced_security_group_id = aws_security_group.frontend.id
  ip_protocol                  = "tcp"
  from_port                    = 8000
  to_port                      = 8000
}

# --- HTTPS out, for all three task groups ----------------------------------------------
# Interface endpoints and the S3 gateway endpoint are both reached on 443. The destination is
# open because a gateway endpoint is addressed by S3's public prefix list, not by a security
# group. In the app subnets there is no internet route behind this rule in any mode. In the
# worker subnets there is one only when enable_nat is true, and that is the OpenAI fallback.
resource "aws_vpc_security_group_egress_rule" "https_out" {
  for_each = {
    frontend = aws_security_group.frontend.id
    backend  = aws_security_group.backend.id
    worker   = aws_security_group.worker.id
  }

  security_group_id = each.value
  description       = "HTTPS to AWS endpoints (and the OpenAI API from the worker in fallback mode)"
  cidr_ipv4         = "0.0.0.0/0"
  ip_protocol       = "tcp"
  from_port         = 443
  to_port           = 443
}

# --- database --------------------------------------------------------------------------
resource "aws_vpc_security_group_ingress_rule" "rds_from_tasks" {
  for_each = {
    backend = aws_security_group.backend.id
    worker  = aws_security_group.worker.id
  }

  security_group_id            = aws_security_group.rds.id
  description                  = "PostgreSQL from ${each.key} tasks"
  referenced_security_group_id = each.value
  ip_protocol                  = "tcp"
  from_port                    = 5432
  to_port                      = 5432
}

resource "aws_vpc_security_group_egress_rule" "tasks_to_rds" {
  for_each = {
    backend = aws_security_group.backend.id
    worker  = aws_security_group.worker.id
  }

  security_group_id            = each.value
  description                  = "PostgreSQL"
  referenced_security_group_id = aws_security_group.rds.id
  ip_protocol                  = "tcp"
  from_port                    = 5432
  to_port                      = 5432
}

# --- interface endpoints ---------------------------------------------------------------
resource "aws_vpc_security_group_ingress_rule" "endpoints_from_tasks" {
  for_each = {
    frontend = aws_security_group.frontend.id
    backend  = aws_security_group.backend.id
    worker   = aws_security_group.worker.id
  }

  security_group_id            = aws_security_group.endpoints.id
  description                  = "HTTPS from ${each.key} tasks"
  referenced_security_group_id = each.value
  ip_protocol                  = "tcp"
  from_port                    = 443
  to_port                      = 443
}
