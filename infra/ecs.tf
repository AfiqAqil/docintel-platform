resource "aws_ecs_cluster" "main" {
  name = local.name

  setting {
    name = "containerInsights"
    # ponytail: off, to keep a throwaway environment cheap. Production: "enabled".
    value = var.destroyable ? "disabled" : "enabled"
  }
}

data "aws_ecr_repository" "service" {
  for_each = toset(["frontend", "backend", "worker"])
  name     = "${var.project}/${each.key}"
}

locals {
  image = {
    for name, repo in data.aws_ecr_repository.service :
    name => "${repo.repository_url}:${var.image_tag}"
  }

  # The same database settings go to both services that use it. The password is not among
  # them: each service reads it from Secrets Manager using the ARN below.
  database_env = {
    DB_HOST       = aws_db_instance.main.address
    DB_PORT       = tostring(aws_db_instance.main.port)
    DB_NAME       = aws_db_instance.main.db_name
    DB_USER       = aws_db_instance.main.username
    DB_SECRET_ARN = aws_db_instance.main.master_user_secret[0].secret_arn
  }
}

# ---------------------------------------------------------------------------------------
# Frontend: behind the load balancer, proxies /api to the Backend API by its private name
# ---------------------------------------------------------------------------------------
module "frontend" {
  source = "./modules/ecs_service"

  name         = "frontend"
  name_prefix  = local.name
  cluster_arn  = aws_ecs_cluster.main.arn
  cluster_name = aws_ecs_cluster.main.name
  image        = local.image["frontend"]

  cpu            = var.services["frontend"].cpu
  memory         = var.services["frontend"].memory
  desired_count  = var.services["frontend"].desired_count
  max_count      = var.services["frontend"].max_count
  container_port = 8080

  environment = {
    # The VPC's own resolver, at a fixed link-local address in every VPC.
    DNS_RESOLVER = "169.254.169.253"
    # A name, never an IP. Resolved inside the VPC only, through Cloud Map.
    API_UPSTREAM = "api.${var.private_dns_namespace}:8000"
  }

  health_check_command = ["CMD-SHELL", "wget -qO- http://127.0.0.1:8080/healthz >/dev/null || exit 1"]

  execution_role_arn = aws_iam_role.execution.arn
  task_role_arn      = aws_iam_role.frontend.arn
  subnet_ids         = aws_subnet.app[*].id
  security_group_ids = [aws_security_group.frontend.id]
  target_group_arn   = aws_lb_target_group.frontend.arn

  region             = var.region
  log_retention_days = var.log_retention_days

  # The target group has to be attached to a listener before a service can use it.
  depends_on = [aws_lb_listener.http]
}

# ---------------------------------------------------------------------------------------
# Backend API: no target group, no public path. Reachable only from the frontend, by name
# ---------------------------------------------------------------------------------------
module "backend" {
  source = "./modules/ecs_service"

  name         = "backend"
  name_prefix  = local.name
  cluster_arn  = aws_ecs_cluster.main.arn
  cluster_name = aws_ecs_cluster.main.name
  image        = local.image["backend"]

  cpu                = var.services["backend"].cpu
  memory             = var.services["backend"].memory
  desired_count      = var.services["backend"].desired_count
  max_count          = var.services["backend"].max_count
  cpu_target_percent = 60
  container_port     = 8000

  environment = merge(local.database_env, {
    AWS_REGION = var.region
    S3_BUCKET  = aws_s3_bucket.documents.bucket
    # The presigned POST must name the REGIONAL endpoint. The global one answers a newly
    # created bucket with a redirect for up to a day, and a browser will not follow a
    # redirect on a cross-origin POST.
    S3_PUBLIC_ENDPOINT_URL = "https://s3.${var.region}.amazonaws.com"
  })

  # It has no target group, so nothing outside the task can health check it.
  health_check_command      = ["CMD-SHELL", "curl -fsS http://127.0.0.1:8000/healthz || exit 1"]
  health_check_start_period = 40

  execution_role_arn     = aws_iam_role.execution.arn
  task_role_arn          = aws_iam_role.backend.arn
  subnet_ids             = aws_subnet.app[*].id
  security_group_ids     = [aws_security_group.backend.id]
  discovery_namespace_id = aws_service_discovery_private_dns_namespace.main.id
  # Registered as api.<namespace>, the name the architecture document uses throughout.
  discovery_name = "api"

  region             = var.region
  log_retention_days = var.log_retention_days

  # The endpoints are how the task pulls its image and writes logs. Without them the first
  # tasks fail to start, and the circuit breaker fails the apply.
  depends_on = [aws_vpc_endpoint.interface, aws_vpc_endpoint.s3]
}

# ---------------------------------------------------------------------------------------
# AI Processing: no port, no target group, no DNS name. Nothing can connect to it
# ---------------------------------------------------------------------------------------
module "worker" {
  source = "./modules/ecs_service"

  name         = "worker"
  name_prefix  = local.name
  cluster_arn  = aws_ecs_cluster.main.arn
  cluster_name = aws_ecs_cluster.main.name
  image        = local.image["worker"]

  cpu           = var.services["worker"].cpu
  memory        = var.services["worker"].memory
  desired_count = var.services["worker"].desired_count
  max_count     = var.services["worker"].max_count

  environment = merge(
    local.database_env,
    {
      AWS_REGION        = var.region
      S3_BUCKET         = aws_s3_bucket.documents.bucket
      SQS_QUEUE_URL     = aws_sqs_queue.processing.url
      MAX_RECEIVE_COUNT = tostring(var.sqs_max_receive_count)
      # The lease must not outlast the visibility timeout, or a redelivered message reaches
      # a worker that cannot claim it. Both come from the same variable so they cannot drift.
      LEASE_SECONDS = tostring(var.sqs_visibility_timeout_seconds)
      LLM_PROVIDER  = var.llm_provider
      LLM_MODEL_ID  = var.llm_model_id
    },
    var.llm_provider == "openai" ? {
      OPENAI_SECRET_ARN = data.aws_secretsmanager_secret.openai_api_key[0].arn
    } : {},
  )

  # A process liveness check passes for a wedged poll loop. This one reads the timestamp the
  # loop writes on every iteration, so it fails when the loop stops turning.
  health_check_command = [
    "CMD-SHELL",
    "python -c \"import os,sys,time; sys.exit(0 if time.time()-float(open(os.environ.get('HEARTBEAT_FILE','/tmp/worker-heartbeat')).read()) < 120 else 1)\"",
  ]

  # The worker traps SIGTERM, stops polling and finishes the message in flight.
  stop_timeout = 120

  execution_role_arn = aws_iam_role.execution.arn
  task_role_arn      = aws_iam_role.worker.arn
  subnet_ids         = aws_subnet.worker[*].id
  security_group_ids = [aws_security_group.worker.id]

  region             = var.region
  log_retention_days = var.log_retention_days

  depends_on = [aws_vpc_endpoint.interface, aws_vpc_endpoint.s3, aws_route.worker_internet]
}

# ---------------------------------------------------------------------------------------
# Worker scaling: step scaling on queue depth
#
# Not target tracking, because raw queue depth is not proportional to capacity, which target
# tracking assumes. Add a task when a backlog builds, remove one when the queue stays empty.
# The minimum is one task, so the reaper always has a poll loop to run in.
# ---------------------------------------------------------------------------------------
resource "aws_appautoscaling_policy" "worker_out" {
  count = module.worker.autoscaling_resource_id == null ? 0 : 1

  name               = "${local.name}-worker-scale-out"
  policy_type        = "StepScaling"
  service_namespace  = "ecs"
  scalable_dimension = "ecs:service:DesiredCount"
  resource_id        = module.worker.autoscaling_resource_id

  step_scaling_policy_configuration {
    adjustment_type = "ChangeInCapacity"
    cooldown        = 60
    step_adjustment {
      metric_interval_lower_bound = 0
      scaling_adjustment          = 1
    }
  }
}

resource "aws_appautoscaling_policy" "worker_in" {
  count = module.worker.autoscaling_resource_id == null ? 0 : 1

  name               = "${local.name}-worker-scale-in"
  policy_type        = "StepScaling"
  service_namespace  = "ecs"
  scalable_dimension = "ecs:service:DesiredCount"
  resource_id        = module.worker.autoscaling_resource_id

  step_scaling_policy_configuration {
    adjustment_type = "ChangeInCapacity"
    cooldown        = 300
    step_adjustment {
      metric_interval_upper_bound = 0
      scaling_adjustment          = -1
    }
  }
}

resource "aws_cloudwatch_metric_alarm" "worker_backlog" {
  count = module.worker.autoscaling_resource_id == null ? 0 : 1

  alarm_name          = "${local.name}-worker-backlog"
  alarm_description   = "Processing requests are queueing. Adds one worker task."
  namespace           = "AWS/SQS"
  metric_name         = "ApproximateNumberOfMessagesVisible"
  dimensions          = { QueueName = aws_sqs_queue.processing.name }
  statistic           = "Maximum"
  period              = 60
  evaluation_periods  = 1
  comparison_operator = "GreaterThanOrEqualToThreshold"
  threshold           = var.worker_scale_out_backlog
  alarm_actions       = [aws_appautoscaling_policy.worker_out[0].arn]
}

resource "aws_cloudwatch_metric_alarm" "worker_idle" {
  count = module.worker.autoscaling_resource_id == null ? 0 : 1

  alarm_name          = "${local.name}-worker-idle"
  alarm_description   = "The queue has been empty for five minutes. Removes one worker task, down to the minimum."
  namespace           = "AWS/SQS"
  metric_name         = "ApproximateNumberOfMessagesVisible"
  dimensions          = { QueueName = aws_sqs_queue.processing.name }
  statistic           = "Maximum"
  period              = 60
  evaluation_periods  = 5
  comparison_operator = "LessThanOrEqualToThreshold"
  threshold           = 0
  alarm_actions       = [aws_appautoscaling_policy.worker_in[0].arn]
}
