# ---------------------------------------------------------------------------------------
# Two kinds of role per task, and they are easy to confuse:
#   execution role  what ECS itself needs to START the task: pull the image, write logs
#   task role       what the application code inside the container may do
# The execution role is shared. The task roles are per service and least privilege.
# ---------------------------------------------------------------------------------------
data "aws_iam_policy_document" "ecs_tasks_trust" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["ecs-tasks.amazonaws.com"]
    }
    # Only tasks in this account may assume these roles (the confused deputy guard).
    condition {
      test     = "StringEquals"
      variable = "aws:SourceAccount"
      values   = [data.aws_caller_identity.current.account_id]
    }
  }
}

resource "aws_iam_role" "execution" {
  name               = "${local.name}-ecs-execution"
  assume_role_policy = data.aws_iam_policy_document.ecs_tasks_trust.json
}

resource "aws_iam_role_policy_attachment" "execution" {
  role       = aws_iam_role.execution.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy"
}

# --- frontend: nginx calls no AWS API, so its task role has no policy at all ------------
resource "aws_iam_role" "frontend" {
  name               = "${local.name}-frontend-task"
  assume_role_policy = data.aws_iam_policy_document.ecs_tasks_trust.json
}

# --- backend ---------------------------------------------------------------------------
resource "aws_iam_role" "backend" {
  name               = "${local.name}-backend-task"
  assume_role_policy = data.aws_iam_policy_document.ecs_tasks_trust.json
}

data "aws_iam_policy_document" "backend" {
  # A presigned POST carries the permissions of whoever signed it, so the API needs PutObject
  # on uploads/ for the browser's upload to be accepted, even though the API never uploads.
  statement {
    sid       = "SignUploadsAndVerifyThem"
    actions   = ["s3:PutObject", "s3:GetObject"]
    resources = ["${aws_s3_bucket.documents.arn}/uploads/*"]
  }
  statement {
    sid       = "ReadReports"
    actions   = ["s3:GetObject"]
    resources = ["${aws_s3_bucket.documents.arn}/reports/*"]
  }
  # Without ListBucket, S3 answers 403 rather than 404 for a key that does not exist, and the
  # upload-complete guard could not tell "not uploaded yet" from "not allowed".
  statement {
    sid       = "TellMissingFromForbidden"
    actions   = ["s3:ListBucket"]
    resources = [aws_s3_bucket.documents.arn]
  }
  statement {
    sid       = "ReadDatabasePassword"
    actions   = ["secretsmanager:GetSecretValue"]
    resources = [aws_db_instance.main.master_user_secret[0].secret_arn]
  }
}

resource "aws_iam_role_policy" "backend" {
  name   = "backend"
  role   = aws_iam_role.backend.id
  policy = data.aws_iam_policy_document.backend.json
}

# --- worker ----------------------------------------------------------------------------
resource "aws_iam_role" "worker" {
  name               = "${local.name}-worker-task"
  assume_role_policy = data.aws_iam_policy_document.ecs_tasks_trust.json
}

# In bedrock mode the inference profile decides which regions a request may be served from,
# and IAM has to allow the underlying model in every one of them. The list is read from the
# profile itself, so it cannot go stale when the model changes.
data "aws_bedrock_inference_profile" "llm" {
  count                = var.llm_provider == "bedrock" ? 1 : 0
  inference_profile_id = var.llm_model_id
}

# Created by the bootstrap stack. Looked up by name, so the two stacks share no state.
data "aws_secretsmanager_secret" "openai_api_key" {
  count = var.llm_provider == "openai" ? 1 : 0
  name  = "${var.project}/openai-api-key"
}

data "aws_iam_policy_document" "worker" {
  statement {
    sid       = "ReadOriginals"
    actions   = ["s3:GetObject"]
    resources = ["${aws_s3_bucket.documents.arn}/uploads/*"]
  }
  statement {
    sid       = "WriteReports"
    actions   = ["s3:PutObject"]
    resources = ["${aws_s3_bucket.documents.arn}/reports/*"]
  }
  statement {
    sid       = "TellMissingFromForbidden"
    actions   = ["s3:ListBucket"]
    resources = [aws_s3_bucket.documents.arn]
  }
  statement {
    sid = "ConsumeProcessingRequests"
    actions = [
      "sqs:ReceiveMessage",
      "sqs:DeleteMessage",
      "sqs:ChangeMessageVisibility",
      "sqs:GetQueueAttributes",
    ]
    resources = [aws_sqs_queue.processing.arn]
  }
  statement {
    sid       = "ReadDatabasePassword"
    actions   = ["secretsmanager:GetSecretValue"]
    resources = [aws_db_instance.main.master_user_secret[0].secret_arn]
  }

  # The two LLM modes are mutually exclusive in IAM. The worker holds Bedrock permissions or
  # access to the OpenAI key, never both.
  dynamic "statement" {
    for_each = var.llm_provider == "bedrock" ? [1] : []
    content {
      sid     = "InvokeModelThroughInferenceProfile"
      actions = ["bedrock:InvokeModel", "bedrock:InvokeModelWithResponseStream"]
      resources = concat(
        [data.aws_bedrock_inference_profile.llm[0].inference_profile_arn],
        [for m in data.aws_bedrock_inference_profile.llm[0].models : m.model_arn],
      )
    }
  }
  dynamic "statement" {
    for_each = var.llm_provider == "openai" ? [1] : []
    content {
      sid       = "ReadOpenAIKey"
      actions   = ["secretsmanager:GetSecretValue"]
      resources = [data.aws_secretsmanager_secret.openai_api_key[0].arn]
    }
  }
}

resource "aws_iam_role_policy" "worker" {
  name   = "worker"
  role   = aws_iam_role.worker.id
  policy = data.aws_iam_policy_document.worker.json
}
