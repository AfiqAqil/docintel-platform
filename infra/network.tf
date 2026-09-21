# ---------------------------------------------------------------------------------------
# VPC and subnets
#
# Three tiers across two AZs:
#   public  load balancer, and the NAT gateway when enable_nat is true
#   app     frontend, API, database, interface endpoints. Never an internet route
#   worker  the AI processing tasks. A default route only when enable_nat is true
#
# The worker sits in its own subnets with its own route table so that turning on egress for
# the OpenAI fallback can never give the frontend, the API or the database an internet route.
# ---------------------------------------------------------------------------------------
resource "aws_vpc" "main" {
  cidr_block = var.vpc_cidr
  # Both are required for interface endpoint private DNS and for Cloud Map to resolve.
  enable_dns_support   = true
  enable_dns_hostnames = true

  tags = { Name = local.name }
}

resource "aws_internet_gateway" "main" {
  vpc_id = aws_vpc.main.id
  tags   = { Name = local.name }
}

resource "aws_subnet" "public" {
  count = 2

  vpc_id            = aws_vpc.main.id
  availability_zone = local.azs[count.index]
  cidr_block        = cidrsubnet(var.vpc_cidr, 8, count.index)

  tags = { Name = "${local.name}-public-${count.index}", Tier = "public" }
}

resource "aws_subnet" "app" {
  count = 2

  vpc_id            = aws_vpc.main.id
  availability_zone = local.azs[count.index]
  cidr_block        = cidrsubnet(var.vpc_cidr, 8, 10 + count.index)

  tags = { Name = "${local.name}-app-${count.index}", Tier = "app" }
}

resource "aws_subnet" "worker" {
  count = 2

  vpc_id            = aws_vpc.main.id
  availability_zone = local.azs[count.index]
  cidr_block        = cidrsubnet(var.vpc_cidr, 8, 20 + count.index)

  tags = { Name = "${local.name}-worker-${count.index}", Tier = "worker" }
}

# ---------------------------------------------------------------------------------------
# Routing
# ---------------------------------------------------------------------------------------
resource "aws_route_table" "public" {
  vpc_id = aws_vpc.main.id
  tags   = { Name = "${local.name}-public" }
}

resource "aws_route" "public_internet" {
  route_table_id         = aws_route_table.public.id
  destination_cidr_block = "0.0.0.0/0"
  gateway_id             = aws_internet_gateway.main.id
}

resource "aws_route_table_association" "public" {
  count          = 2
  subnet_id      = aws_subnet.public[count.index].id
  route_table_id = aws_route_table.public.id
}

# No default route, in any mode. Only the local route and the S3 gateway endpoint.
resource "aws_route_table" "app" {
  vpc_id = aws_vpc.main.id
  tags   = { Name = "${local.name}-app" }
}

resource "aws_route_table_association" "app" {
  count          = 2
  subnet_id      = aws_subnet.app[count.index].id
  route_table_id = aws_route_table.app.id
}

resource "aws_route_table" "worker" {
  vpc_id = aws_vpc.main.id
  tags   = { Name = "${local.name}-worker" }
}

resource "aws_route_table_association" "worker" {
  count          = 2
  subnet_id      = aws_subnet.worker[count.index].id
  route_table_id = aws_route_table.worker.id
}

# ---------------------------------------------------------------------------------------
# NAT gateway: OpenAI fallback only
#
# A NAT gateway has to sit in a public subnet, because it needs the internet gateway route
# itself. One gateway, one AZ: losing that AZ stops model calls in the fallback mode, which
# is acceptable for a fallback and would be one per AZ otherwise.
# ---------------------------------------------------------------------------------------
resource "aws_eip" "nat" {
  count  = var.enable_nat ? 1 : 0
  domain = "vpc"
  tags   = { Name = "${local.name}-nat" }
}

resource "aws_nat_gateway" "main" {
  count = var.enable_nat ? 1 : 0

  allocation_id = aws_eip.nat[0].id
  subnet_id     = aws_subnet.public[0].id
  tags          = { Name = local.name }

  depends_on = [aws_internet_gateway.main]
}

resource "aws_route" "worker_internet" {
  count = var.enable_nat ? 1 : 0

  route_table_id         = aws_route_table.worker.id
  destination_cidr_block = "0.0.0.0/0"
  nat_gateway_id         = aws_nat_gateway.main[0].id
}

# ---------------------------------------------------------------------------------------
# VPC endpoints
#
# With no NAT in the default mode, this is how tasks reach AWS APIs at all. In the fallback
# mode they stay: AWS traffic keeps using them and only calls to the OpenAI API cross the NAT.
# ---------------------------------------------------------------------------------------
locals {
  interface_endpoints = toset([
    "ecr.api",         # image manifest and auth
    "ecr.dkr",         # image layers' registry endpoint
    "logs",            # the awslogs driver
    "sqs",             # the worker's long poll
    "secretsmanager",  # database password, and the OpenAI key in fallback mode
    "bedrock-runtime", # model calls in the default mode
  ])
}

resource "aws_vpc_endpoint" "interface" {
  for_each = local.interface_endpoints

  vpc_id            = aws_vpc.main.id
  service_name      = "com.amazonaws.${var.region}.${each.key}"
  vpc_endpoint_type = "Interface"
  # The regional DNS name resolves to these addresses for the whole VPC, so tasks in the
  # other AZ, and in the worker subnets, reach them over the local route.
  subnet_ids          = slice(aws_subnet.app[*].id, 0, var.interface_endpoint_az_count)
  security_group_ids  = [aws_security_group.endpoints.id]
  private_dns_enabled = true

  tags = { Name = "${local.name}-${each.key}" }
}

# A gateway endpoint is a route table entry, not something inside a subnet. It is associated
# with BOTH private route tables, so the worker's separate routing does not cost it private
# access to S3. ECR image layers are also served from S3, so every task needs this to start.
resource "aws_vpc_endpoint" "s3" {
  vpc_id            = aws_vpc.main.id
  service_name      = "com.amazonaws.${var.region}.s3"
  vpc_endpoint_type = "Gateway"
  route_table_ids   = [aws_route_table.app.id, aws_route_table.worker.id]

  tags = { Name = "${local.name}-s3" }
}

# ---------------------------------------------------------------------------------------
# Private DNS
#
# Cloud Map creates and owns the Route 53 private hosted zone behind this namespace and
# associates it with the VPC. There is deliberately no aws_route53_zone in this stack.
# ---------------------------------------------------------------------------------------
resource "aws_service_discovery_private_dns_namespace" "main" {
  name        = var.private_dns_namespace
  description = "Private service discovery for ${local.name}"
  vpc         = aws_vpc.main.id
}
