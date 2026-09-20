# Evidence

Output captured from real runs, not written by hand. Each file is here because the claim it
supports is one a reader would otherwise have to take on trust.

| File | What it shows |
|---|---|
| `local-e2e-documents.txt` | Every sample driven through the full local stack, and the outcome each reached |
| `local-e2e-worker-logs.txt` | The worker's own structured logs for those runs, with `document_id` bound per message |
| `local-e2e-report-supporting-evidence.json` | One complete report as stored in S3, showing masking, per field verification states and the trace |

The local stack is docker compose: LocalStack for S3 and SQS, PostgreSQL, and the three
services built from the same Dockerfiles that get deployed. The upload goes from the browser
straight to object storage, storage emits the event, the queue delivers it, and the worker
produces the report. Nothing in that path is stubbed.
