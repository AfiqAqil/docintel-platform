# One independently deployed ECS service: log group, task definition, service, optional
# Cloud Map registration, optional autoscaling. Used three times by the root module.

resource "aws_cloudwatch_log_group" "this" {
  name              = "/ecs/${var.name_prefix}/${var.name}"
  retention_in_days = var.log_retention_days
}

resource "aws_ecs_task_definition" "this" {
  family                   = "${var.name_prefix}-${var.name}"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = var.cpu
  memory                   = var.memory
  execution_role_arn       = var.execution_role_arn
  task_role_arn            = var.task_role_arn

  # The images are built for arm64, natively, on an arm64 machine and an arm64 CI runner.
  # Graviton Fargate is also about 20 percent cheaper than x86 for the same size.
  runtime_platform {
    operating_system_family = "LINUX"
    cpu_architecture        = "ARM64"
  }

  container_definitions = jsonencode([
    merge(
      {
        name      = var.name
        image     = var.image
        essential = true

        environment = [for k, v in var.environment : { name = k, value = v }]

        portMappings = var.container_port == null ? [] : [
          { containerPort = var.container_port, protocol = "tcp" }
        ]

        stopTimeout = var.stop_timeout

        logConfiguration = {
          logDriver = "awslogs"
          options = {
            "awslogs-group"         = aws_cloudwatch_log_group.this.name
            "awslogs-region"        = var.region
            "awslogs-stream-prefix" = var.name
          }
        }
      },
      var.health_check_command == null ? {} : {
        healthCheck = {
          command     = var.health_check_command
          interval    = 30
          timeout     = 5
          retries     = 3
          startPeriod = var.health_check_start_period
        }
      },
    )
  ])
}

# An A record per running task in the private hosted zone Cloud Map owns. ECS registers a
# task when it starts and removes it when it stops.
resource "aws_service_discovery_service" "this" {
  count = var.discovery_namespace_id == null ? 0 : 1

  name = coalesce(var.discovery_name, var.name)

  dns_config {
    namespace_id   = var.discovery_namespace_id
    routing_policy = "MULTIVALUE"
    dns_records {
      type = "A"
      # Matches nginx's `resolver ... valid=10s`, so a replaced task is forgotten in seconds.
      ttl = 10
    }
  }

  # ECS reports task health to Cloud Map, so an unhealthy task drops out of DNS.
  health_check_custom_config {}
}

resource "aws_ecs_service" "this" {
  name            = var.name
  cluster         = var.cluster_arn
  task_definition = aws_ecs_task_definition.this.arn
  desired_count   = var.desired_count
  launch_type     = "FARGATE"

  network_configuration {
    subnets          = var.subnet_ids
    security_groups  = var.security_group_ids
    assign_public_ip = false
  }

  dynamic "load_balancer" {
    for_each = var.target_group_arn == null ? [] : [1]
    content {
      target_group_arn = var.target_group_arn
      container_name   = var.name
      container_port   = var.container_port
    }
  }

  dynamic "service_registries" {
    for_each = aws_service_discovery_service.this
    content {
      registry_arn = service_registries.value.arn
    }
  }

  # Rolling deployment: start the new task, wait for it to be healthy, then stop the old one.
  # With one task that means 200 percent briefly and never zero.
  deployment_minimum_healthy_percent = 100
  deployment_maximum_percent         = 200

  # If the new tasks never become healthy, ECS stops trying and puts the old revision back.
  deployment_circuit_breaker {
    enable   = true
    rollback = true
  }

  # Autoscaling owns the count after creation. Without this, every apply would reset it.
  lifecycle {
    ignore_changes = [desired_count]
  }
}

# ---------------------------------------------------------------------------------------
# Autoscaling. The target exists whenever the service can scale at all. The policy attached
# to it is either CPU target tracking, defined here, or something specific to one service,
# defined by the caller against the outputs of this module.
# ---------------------------------------------------------------------------------------
resource "aws_appautoscaling_target" "this" {
  count = var.max_count > var.desired_count ? 1 : 0

  service_namespace  = "ecs"
  scalable_dimension = "ecs:service:DesiredCount"
  resource_id        = "service/${var.cluster_name}/${aws_ecs_service.this.name}"
  min_capacity       = var.desired_count
  max_capacity       = var.max_count
}

resource "aws_appautoscaling_policy" "cpu" {
  count = var.cpu_target_percent != null && var.max_count > var.desired_count ? 1 : 0

  name               = "${var.name_prefix}-${var.name}-cpu"
  policy_type        = "TargetTrackingScaling"
  service_namespace  = aws_appautoscaling_target.this[0].service_namespace
  scalable_dimension = aws_appautoscaling_target.this[0].scalable_dimension
  resource_id        = aws_appautoscaling_target.this[0].resource_id

  target_tracking_scaling_policy_configuration {
    target_value = var.cpu_target_percent
    predefined_metric_specification {
      predefined_metric_type = "ECSServiceAverageCPUUtilization"
    }
    scale_in_cooldown  = 300
    scale_out_cooldown = 60
  }
}
