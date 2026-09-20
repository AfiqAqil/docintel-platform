# The API contract the SPA codes against

Generated from the running backend, not written by hand. `backend/app/schemas.py` is the
source; this file exists so the frontend work can be done without reading the Python.

The browser only ever talks to one origin, the load balancer. nginx forwards `/api` to the
Backend API over the internal domain name, so there is no CORS on the API and no
per-environment API URL baked into the build. See ARCHITECTURE section 8.

## Routes

| Method | Path | Codes |
|---|---|---|
| POST | `/api/documents` | 201, 413, 415, 422 |
| POST | `/api/documents/{id}/upload-complete` | 200, 404, 409 |
| GET | `/api/documents?limit=&offset=` | 200 |
| GET | `/api/documents/{id}` | 200, 404 |
| GET | `/api/documents/{id}/report` | 200, 404, 409 |
| GET | `/healthz` | 200 |

## POST /api/documents

Request:
```json
{ "filename": "claim.pdf", "content_type": "application/pdf", "size_bytes": 1990 }
```

Response 201:
```json
{ "document_id": "uuid", "upload_url": "https://...", "fields": { "key": "...", "policy": "..." } }
```

`415` for a content type the platform does not accept, `413` for a file over the size limit.

## The upload, which is three calls not one

1. `POST /api/documents` to get the slot.
2. `POST` the file **directly to S3** at `upload_url`, as `multipart/form-data`, with every
   entry of `fields` appended to the form **before** the `file` field. S3 requires the file
   last. The API never sees the bytes.
3. `POST /api/documents/{id}/upload-complete`, which moves the row to `QUEUED` so the UI
   updates immediately. Processing does not depend on this call: the real trigger is the S3
   event. A `409` means the object is not in the bucket.

## DocumentOut

```ts
type DocumentOut = {
  id: string
  filename: string
  content_type: string
  size_bytes: number
  status: "UPLOADING" | "QUEUED" | "PROCESSING" | "COMPLETED" | "FAILED" | "EXPIRED"
  outcome: "COMPLETE" | "INCOMPLETE" | "UNSUPPORTED" | null
  doc_type: string | null
  current_step: string | null          // the graph node currently running
  report_summary: {
    summary: string | null
    missing_information: string[]
    validation_errors: string[]
    observations: string[]
  } | null
  error_message: string | null
  attempt_count: number
  created_at: string                   // ISO 8601
  updated_at: string
  uploaded_at: string | null
  completed_at: string | null
}
```

`GET /api/documents` returns `{ items: DocumentOut[], limit: number, offset: number }`,
newest first.

## Status and outcome are two different questions

`status` answers where the document is. `outcome` answers how it went, and is only set once
`status` is `COMPLETED`. That separation is deliberate: "completed, but information is
missing" is a business result to review, not a failure, so it is `COMPLETED` plus
`INCOMPLETE` rather than `FAILED`.

`FAILED` is reserved for technical failure: an unreadable file, an encrypted PDF, model
errors that survived retries. `error_message` carries the reason.

`EXPIRED` means an upload slot was requested and never used. It is **not terminal** in the
backend, because a late S3 event can still claim the row, but the frontend treats it as
settled for polling purposes. See ARCHITECTURE section 11.

## GET /api/documents/{id}/report

The full report JSON as stored in S3. `409` while the document has not finished, `404` if
there is no report object. Shape:

```json
{
  "document_id": "...", "generated_at": "...", "document_type": "claim_form",
  "classification": { "confidence": 0.93, "notes": "..." },
  "outcome": "INCOMPLETE",
  "summary": "one paragraph",
  "extracted": { "field": { "value": "...", "snippet": "...", "verified": true } },
  "missing_information": ["..."], "validation_errors": ["..."], "observations": ["..."],
  "trace": [{ "node": "...", "status": "ok", "duration_ms": 12, "detail": null }]
}
```

`extracted` values are already masked. `verified` is `true` when the evidence snippet was
found in the source text, `false` when it was not and the field was rejected, and `null` when
the input was image only and there was no text to check against.
