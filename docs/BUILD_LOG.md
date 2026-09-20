# Build log

What was built, in which order, how it was verified, and every decision the architecture
document did not already cover. `docs/ARCHITECTURE.md` is the source of truth; anything here
that adds to it is called out, and the architecture document is updated in the same change.

---

## Conventions held throughout

- One AWS account, `277707137200`, region `ap-southeast-1`. This machine has more than one
  account configured, so every `aws` CLI call passes `--profile <project-profile>` inline
  rather than relying on the default. Terraform has no `--profile` flag, so it is invoked as
  `AWS_PROFILE=<project-profile> terraform ...`, and no profile name is hardcoded in any
  provider or backend block, which would break CI where authentication is OIDC and no profile
  exists. The guard that actually holds in both paths is
  `allowed_account_ids = ["277707137200"]` in the provider: an apply pointed at the wrong
  account fails before it changes anything.
- No credential, key or token is committed, printed, or written into Terraform state.
- Every phase is verified by running something, not by inspection, and the real output is
  recorded here.

---

## Decisions not covered by ARCHITECTURE.md

Five gaps were identified before building and approved before any code was written. The
first two are now also written into `ARCHITECTURE.md` itself, since the document referred to
both without ever defining them.

### 1. The `documents` table

Sections 3 and 7 describe statuses, outcomes, a lease and a claim, but never give the schema.
One table, one migration, owned by the backend through Alembic.

| Column | Type | Note |
|---|---|---|
| `id` | `uuid` primary key | also the S3 key suffix and the correlation id in every log line |
| `filename` | `text` | kept here rather than in the S3 key, per section 2 |
| `content_type` | `text` | |
| `size_bytes` | `bigint` | |
| `status` | `text`, CHECK constrained | `UPLOADING`, `QUEUED`, `PROCESSING`, `COMPLETED`, `FAILED`, `EXPIRED` |
| `outcome` | `text` null, CHECK constrained | `COMPLETE`, `INCOMPLETE`, `UNSUPPORTED` |
| `doc_type` | `text` null | |
| `current_step` | `text` null | the graph node currently running |
| `lease_expires_at` | `timestamptz` null | the claim and the reaper both key off this |
| `report_summary` | `jsonb` null | summary only; the full report is `reports/{id}.json` in S3 |
| `error_message` | `text` null | |
| `attempt_count` | `int` not null default 0 | |
| `created_at`, `updated_at`, `uploaded_at`, `completed_at` | `timestamptz` | |

Indexes: `(status, lease_expires_at)` serves the claim and both reaper sweeps;
`(created_at desc)` serves the history list.

The CHECK constraints are deliberate. `status` and `outcome` drive routing decisions in the
worker and rendering in the frontend, so an unexpected value should be a database error at
the point of writing, not a silently broken row discovered later.

**One source of DDL.** Nothing copies this schema. The worker's claim and reaper tests bring
up PostgreSQL in Docker and run the backend's own Alembic migration against it, so the SQL
under test runs against the real table definition. There is no second `CREATE TABLE` in the
repository. At startup the worker retries until the table exists, because ECS gives no
ordering guarantee between the API task that migrates and the worker task that reads.

### 2. Report JSON shape

Section 4 says `report` is a `dict`. The assignment lists what a report may contain. The
shape settled on:

```json
{
  "document_id": "...",
  "generated_at": "...",
  "document_type": "claim_form",
  "classification": { "confidence": 0.93, "notes": "..." },
  "outcome": "INCOMPLETE",
  "summary": "one paragraph, written by the model from the masked state",
  "extracted": { "field_name": { "value": "...", "snippet": "...", "verified": true } },
  "missing_information": ["..."],
  "validation_errors": ["..."],
  "observations": ["..."],
  "trace": [{ "node": "...", "status": "ok", "duration_ms": 12, "detail": null }]
}
```

Every extracted field carries the evidence snippet it came from and whether that snippet was
verified against the source text, which is what makes the deterministic hallucination check
in section 5 visible in the output rather than only in the logs.

### 3. `StepRecord`

Referenced in the section 4 state table, never defined:
`{node: str, status: "ok" | "error", duration_ms: int, detail: str | None}`.

### 4. Classification confidence threshold

Section 5 says the threshold is configurable but gives no number.
`CLASSIFY_CONFIDENCE_THRESHOLD` defaults to **0.60**; below it the document routes to
`UNSUPPORTED` rather than being guessed at.

### 5. Extraction retry budget

Section 4 says the validation router may send the state back for "one more attempt".
`MAX_EXTRACTION_ATTEMPTS` defaults to **2**, so exactly one retry. After that the graph
proceeds with whatever was extracted and the outcome becomes `INCOMPLETE`.

---

## Phase 1: repository skeleton

**Built.** The directory layout from architecture section 9, a `.gitignore` written before
the first commit, a README stub pointing at the architecture document, and this log.

**`.gitignore` decisions.** `.env`, `docs/*.pdf` (the assignment brief is the issuing
company's document, not ours to publish), Terraform state and `.terraform/` were already
covered. Added: `*.tfplan`, and `*.local.tfvars` plus `*.auto.tfvars` rather than a blanket
`*.tfvars`. The per environment `.tfvars` files carry no secrets and are worth committing,
because the assignment asks for enough instruction that another engineer can provision the
environment. Anything machine specific, such as an office IP allowlist, goes in a
`*.local.tfvars` that is never tracked.

**Verified.** `git status` showed no `.env`, no PDF and no state file staged before the
commit was made.

**Delegated.** Nothing. The skeleton and the shared interfaces have to exist before any
parallel work can start.
