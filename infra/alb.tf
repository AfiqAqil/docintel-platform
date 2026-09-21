# ---------------------------------------------------------------------------------------
# Load balancer: the only thing in a public subnet that accepts connections
#
# It forwards to the frontend and nothing else. The Backend API has no target group and no
# listener rule, so there is no path to it from outside the VPC.
# ---------------------------------------------------------------------------------------
resource "aws_lb" "main" {
  name               = local.name
  load_balancer_type = "application"
  # ponytail: internet-facing with an IP allowlist, and HTTP only, because no domain is
  # registered and ACM needs one. Production: internal = true behind VPN or Direct Connect,
  # a Route 53 record, an ACM certificate, and a listener that redirects 80 to 443.
  internal        = false
  subnets         = aws_subnet.public[*].id
  security_groups = [aws_security_group.alb.id]

  drop_invalid_header_fields = true
  enable_deletion_protection = !var.destroyable
}

resource "aws_lb_target_group" "frontend" {
  name = "${local.name}-frontend"
  port = 8080
  # Fargate tasks have their own network interface, so the target is the task's IP.
  target_type = "ip"
  protocol    = "HTTP"
  vpc_id      = aws_vpc.main.id

  # How long a draining task keeps serving requests already in flight.
  deregistration_delay = 30

  health_check {
    # Served by nginx itself, not proxied. The frontend is healthy when the backend is down.
    path                = "/healthz"
    matcher             = "200"
    interval            = 15
    timeout             = 5
    healthy_threshold   = 2
    unhealthy_threshold = 3
  }
}

resource "aws_lb_listener" "http" {
  load_balancer_arn = aws_lb.main.arn
  port              = 80
  protocol          = "HTTP"

  default_action {
    type             = "forward"
    target_group_arn = aws_lb_target_group.frontend.arn
  }
}
