# ---------------------------------------------------------------------------------------
# S3: original documents under uploads/, generated reports under reports/
# ---------------------------------------------------------------------------------------
resource "aws_s3_bucket" "documents" {
  bucket        = "${local.name}-documents-${data.aws_caller_identity.current.account_id}"
  force_destroy = var.destroyable
}

resource "aws_s3_bucket_versioning" "documents" {
  bucket = aws_s3_bucket.documents.id
  versioning_configuration {
    status = "Enabled"
  }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "documents" {
  bucket = aws_s3_bucket.documents.id
  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

# The bucket is never public. The browser uploads with a presigned POST, which is an
# authenticated request scoped to one key, one content type, a size limit and five minutes.
resource "aws_s3_bucket_public_access_block" "documents" {
  bucket                  = aws_s3_bucket.documents.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

data "aws_iam_policy_document" "documents_tls_only" {
  statement {
    sid       = "DenyInsecureTransport"
    effect    = "Deny"
    actions   = ["s3:*"]
    resources = [aws_s3_bucket.documents.arn, "${aws_s3_bucket.documents.arn}/*"]
    principals {
      type        = "*"
      identifiers = ["*"]
    }
    condition {
      test     = "Bool"
      variable = "aws:SecureTransport"
      values   = ["false"]
    }
  }
}

resource "aws_s3_bucket_policy" "documents" {
  bucket     = aws_s3_bucket.documents.id
  policy     = data.aws_iam_policy_document.documents_tls_only.json
  depends_on = [aws_s3_bucket_public_access_block.documents]
}

# The API itself is same origin with the frontend and needs no CORS. The bucket does, because
# the browser posts the file straight to it. Only POST, and only from the load balancer.
resource "aws_s3_bucket_cors_configuration" "documents" {
  bucket = aws_s3_bucket.documents.id

  cors_rule {
    allowed_methods = ["POST"]
    allowed_origins = ["http://${aws_lb.main.dns_name}"]
    allowed_headers = ["*"]
    max_age_seconds = 3000
  }
}

# Successful storage IS the processing request: S3 emits the event, so there is no second
# write that could fail after the first succeeded. The prefix filter matters as much as the
# event: reports are written into this same bucket, and without it the worker would trigger
# on its own output.
resource "aws_s3_bucket_notification" "documents" {
  bucket = aws_s3_bucket.documents.id

  queue {
    queue_arn     = aws_sqs_queue.processing.arn
    events        = ["s3:ObjectCreated:*"]
    filter_prefix = "uploads/"
  }

  depends_on = [aws_sqs_queue_policy.processing]
}

# ---------------------------------------------------------------------------------------
# SQS: processing requests, and the dead letter queue behind them
# ---------------------------------------------------------------------------------------
resource "aws_sqs_queue" "dlq" {
  name                      = "${local.name}-processing-dlq"
  message_retention_seconds = 1209600 # 14 days, the maximum: time to notice and redrive
  sqs_managed_sse_enabled   = true
}

resource "aws_sqs_queue" "processing" {
  name = "${local.name}-processing"
  # Must outlast the worker's lease. One heartbeat extends both while a document is in flight.
  visibility_timeout_seconds = var.sqs_visibility_timeout_seconds
  receive_wait_time_seconds  = 20
  message_retention_seconds  = 345600 # 4 days
  sqs_managed_sse_enabled    = true

  redrive_policy = jsonencode({
    deadLetterTargetArn = aws_sqs_queue.dlq.arn
    maxReceiveCount     = var.sqs_max_receive_count
  })
}

resource "aws_sqs_queue_redrive_allow_policy" "dlq" {
  queue_url = aws_sqs_queue.dlq.id
  redrive_allow_policy = jsonencode({
    redrivePermission = "byQueue"
    sourceQueueArns   = [aws_sqs_queue.processing.arn]
  })
}

# Only this bucket, in this account, may send. Without the two conditions any S3 bucket in
# any account could put messages on the queue.
data "aws_iam_policy_document" "processing_queue" {
  statement {
    sid       = "AllowDocumentsBucket"
    actions   = ["sqs:SendMessage"]
    resources = [aws_sqs_queue.processing.arn]
    principals {
      type        = "Service"
      identifiers = ["s3.amazonaws.com"]
    }
    condition {
      test     = "ArnEquals"
      variable = "aws:SourceArn"
      values   = [aws_s3_bucket.documents.arn]
    }
    condition {
      test     = "StringEquals"
      variable = "aws:SourceAccount"
      values   = [data.aws_caller_identity.current.account_id]
    }
  }
}

resource "aws_sqs_queue_policy" "processing" {
  queue_url = aws_sqs_queue.processing.id
  policy    = data.aws_iam_policy_document.processing_queue.json
}

# Anything in the dead letter queue is a document nobody processed. One message is an alarm.
resource "aws_cloudwatch_metric_alarm" "dlq_not_empty" {
  alarm_name          = "${local.name}-dlq-not-empty"
  alarm_description   = "A processing request exhausted its deliveries. Inspect the message, fix the cause, then redrive with StartMessageMoveTask."
  namespace           = "AWS/SQS"
  metric_name         = "ApproximateNumberOfMessagesVisible"
  dimensions          = { QueueName = aws_sqs_queue.dlq.name }
  statistic           = "Maximum"
  period              = 60
  evaluation_periods  = 1
  comparison_operator = "GreaterThanThreshold"
  threshold           = 0
  treat_missing_data  = "notBreaching"
}

# ---------------------------------------------------------------------------------------
# RDS PostgreSQL: status, history, metadata, report summaries
# ---------------------------------------------------------------------------------------
resource "aws_db_subnet_group" "main" {
  name       = local.name
  subnet_ids = aws_subnet.app[*].id
}

resource "aws_db_instance" "main" {
  identifier     = local.name
  engine         = "postgres"
  engine_version = "16"
  instance_class = var.db_instance_class

  allocated_storage = 20
  storage_type      = "gp3"
  storage_encrypted = true

  db_name  = "docintel"
  username = "docintel"
  # RDS generates the password, stores it in Secrets Manager and rotates it. It never appears
  # in this code, in a tfvars file, or in Terraform state. The services read it with boto3
  # when they open a connection, and refetch once on an authentication error, because a
  # rotation would otherwise strand a long-running task with a stale password.
  manage_master_user_password = true

  db_subnet_group_name   = aws_db_subnet_group.main.name
  vpc_security_group_ids = [aws_security_group.rds.id]
  publicly_accessible    = false
  multi_az               = var.db_multi_az

  backup_retention_period    = var.destroyable ? 1 : 14
  deletion_protection        = !var.destroyable
  skip_final_snapshot        = var.destroyable
  final_snapshot_identifier  = var.destroyable ? null : "${local.name}-final"
  auto_minor_version_upgrade = true
  apply_immediately          = var.destroyable
}
