# Evidence

Output captured from real runs, not written by hand. Each file is here because the claim it
supports is one a reader would otherwise have to take on trust.

| File | What it shows |
|---|---|
| `local-e2e-documents.txt` | Every sample driven through the full local stack, and the outcome each reached |
| `local-e2e-worker-logs.txt` | The worker's own structured logs for those runs, with `document_id` bound per message |
| `local-e2e-report-supporting-evidence.json` | One complete report as stored in S3, showing masking, per field verification states and the trace |
| `aws-deploy-state.txt` | The deployed environment: three ECS services stable, the frontend target healthy, the `api.docintel.internal` A record in the private hosted zone, and the three route tables showing that only the worker has a default route |
| `aws-e2e-report-claim-form.json` | A report produced on AWS, from a browser style upload through the load balancer |
| `aws-e2e-worker-logs.txt` | The worker's CloudWatch log lines for that document, and its startup: waiting for the schema, then ignoring the `s3:TestEvent` |

The local stack is docker compose: LocalStack for S3 and SQS, PostgreSQL, and the three
services built from the same Dockerfiles that get deployed. The upload goes from the browser
straight to object storage, storage emits the event, the queue delivers it, and the worker
produces the report. Nothing in that path is stubbed.
