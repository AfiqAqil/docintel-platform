#!/bin/sh
# Creates the bucket, the queue and the notification that wires them together.
#
# LocalStack runs every executable in /etc/localstack/init/ready.d once it is ready, so this
# is the local equivalent of what Terraform builds on AWS. The shapes match deliberately: the
# same bucket layout, the same prefix filter, the same redrive policy. A local run that
# passes against a different shape would prove nothing about the deployed one.
set -eu

# Exported, not just assigned. awslocal defaults to us-east-1 inside the container, and SQS
# queues are region scoped, so without this the queue is created in one region while the
# bucket and every client sit in another and nothing can find it.
AWS_DEFAULT_REGION="${AWS_DEFAULT_REGION:-ap-southeast-1}"
export AWS_DEFAULT_REGION
REGION="$AWS_DEFAULT_REGION"
BUCKET="${S3_BUCKET:-docintel-local}"
QUEUE="${SQS_QUEUE_NAME:-docintel-processing}"
DLQ="${QUEUE}-dlq"

awslocal s3api create-bucket --bucket "$BUCKET" \
  --create-bucket-configuration LocationConstraint="$REGION" >/dev/null

# The browser posts the file straight to S3, so the bucket needs CORS. On AWS the allowed
# origin is the load balancer; locally it is the compose frontend.
awslocal s3api put-bucket-cors --bucket "$BUCKET" --cors-configuration '{
  "CORSRules": [{
    "AllowedMethods": ["POST", "GET", "HEAD"],
    "AllowedOrigins": ["*"],
    "AllowedHeaders": ["*"],
    "ExposeHeaders": ["ETag"]
  }]
}'

# The dead letter queue has to exist before the main queue can name it in a redrive policy.
awslocal sqs create-queue --queue-name "$DLQ" >/dev/null
DLQ_ARN=$(awslocal sqs get-queue-attributes \
  --queue-url "$(awslocal sqs get-queue-url --queue-name "$DLQ" --query QueueUrl --output text)" \
  --attribute-names QueueArn --query 'Attributes.QueueArn' --output text)

# maxReceiveCount matches MAX_RECEIVE_COUNT in the consumer. The consumer has to recognise
# its own last attempt so it can record a failure rather than leaving the row in PROCESSING
# for the reaper, and it can only do that if the two numbers agree.
awslocal sqs create-queue --queue-name "$QUEUE" --attributes "{
  \"VisibilityTimeout\": \"120\",
  \"RedrivePolicy\": \"{\\\"deadLetterTargetArn\\\":\\\"${DLQ_ARN}\\\",\\\"maxReceiveCount\\\":\\\"3\\\"}\"
}" >/dev/null

QUEUE_ARN=$(awslocal sqs get-queue-attributes \
  --queue-url "$(awslocal sqs get-queue-url --queue-name "$QUEUE" --query QueueUrl --output text)" \
  --attribute-names QueueArn --query 'Attributes.QueueArn' --output text)

# The filter is the reason the worker never triggers on its own report writes: reports are
# written into reports/ in this same bucket, and without the prefix rule every report would
# enqueue a new job for the document that just produced it, forever.
awslocal s3api put-bucket-notification-configuration --bucket "$BUCKET" \
  --notification-configuration "{
    \"QueueConfigurations\": [{
      \"QueueArn\": \"${QUEUE_ARN}\",
      \"Events\": [\"s3:ObjectCreated:*\"],
      \"Filter\": {\"Key\": {\"FilterRules\": [{\"Name\": \"prefix\", \"Value\": \"uploads/\"}]}}
    }]
  }"

echo "localstack ready: bucket ${BUCKET}, queue ${QUEUE}, dlq ${DLQ}, notification on uploads/"
