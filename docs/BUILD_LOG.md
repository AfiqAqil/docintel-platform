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

---

## Phase 2: the LangGraph workflow

**Built.** The AI processing service's graph, deterministic rules and LLM layer. No AWS, no
database, no network. 44 tests.

| Layer | Directory | Contents |
|---|---|---|
| Orchestration | `worker/graph/` | `state.py`, `routers.py`, `build.py`, `nodes.py` |
| Deterministic logic | `worker/rules/` | `parse.py`, `validate.py`, `snippets.py`, `mask.py` |
| LLM assisted | `worker/llm/` | `provider.py`, `schemas.py`, `prompts.py`, `fake.py` |

**Verified.**

```
$ .venv/bin/python -m pytest tests/ -q
............................................                             [100%]
44 passed in 0.44s

$ .venv/bin/python -m ruff check . --exclude .venv
All checks passed!
```

Coverage: each of the six document types reaching its own extraction node, the retry cycle
going back to the same extractor exactly once, the retry budget holding, missing fields not
triggering a retry, unknown type and low confidence both declining rather than guessing,
terminal failure ending at `record_failure` with no model call, transient failure propagating
out of the graph, masking running after validation and before the report, a fabricated
snippet being rejected, image only input skipping verification, and the file parsing and
validation rules individually.

### Two deviations from ARCHITECTURE.md, both now written into the document

**1. Masking writes `masked_trace`, not `trace`.** Section 4 said `mask_pii` masks the trace.
It cannot: `trace` accumulates through an `operator.add` reducer, so a node returning it
appends and can never replace it. The scrubbed copy goes to a separate key, and the report
persists that one. Raw `trace` is never logged, never persisted, and there is no checkpointer
to store it, so the guarantee is unchanged and only the mechanism differs.

**2. The graph makes no AWS call at all.** Section 4 promised no database calls. The S3
`GetObject` moved into the consumer, which puts the bytes into the initial state as
`raw_bytes`, leaving `load_document` as pure parsing. Fetching is infrastructure work, which
section 4's own four layer table assigns to `consumer/`. The gain is that the entire graph
runs in tests and in the phase 4 local script against a file on disk, with no credentials and
nothing mocked.

### Decisions worth defending

**Transient errors are not a graph route.** Two failure kinds are treated completely
differently. A terminal error means the document cannot be processed however often we try, so
it becomes state and the run ends at `record_failure`. A transient error means the attempt
failed but the work is still valid, so the exception leaves the graph entirely, the consumer
does not ack, and SQS redelivers. Retrying is the queue's job. The classifier is deliberately
conservative: anything not recognisable as the document's fault is treated as transient,
because a wrong terminal decision marks a document permanently failed over a blip with no
automatic way back, while a wrong transient decision costs one redelivery.

**The retry cycle covers invalid extractions, not missing fields.** A field the document does
not contain will not appear however many times the model is asked. This is why `validate`
returns two separate lists and why `route_by_validation` reads only one of them.

**Sensitive fields are tagged on the schema, not pattern matched by name.** `mask.py` reads
`json_schema_extra={"pii": True}` back off the Pydantic model fields, so adding a sensitive
field to a schema masks it automatically rather than needing a second edit in the masking
module that would eventually be forgotten.

**Masking reveals a tail only for values containing a digit.** Found while smoke testing:
revealing the last four characters turned `Jordan Avery` into `•••••• •very`. A tail is how
people tell two policy numbers apart, and on a name or an email address it is useless for
that and leaks part of the value. The condition is a property of the value rather than of the
field name, so it cannot drift out of step with the schema.

**Line items stay a list of rows.** The arithmetic rule needs the rows intact to sum them, and
each cell carries its own evidence snippet, so a fabricated citation inside a row is caught
the same way one at the top level is.

**Test documents are real files.** The route tests build genuine DOCX and PNG bytes and drive
the graph from its actual entry point, so `load_document` really parses. An earlier draft
pre-populated the `text` key instead, which tested a path production never takes.

### Delegation

Three Sonnet agents in parallel, each in its own worktree, each given the already committed
interface files to build against and told to report conflicts rather than resolve them.

| Delegated | Model | Produced | What I verified | What I changed after review |
|---|---|---|---|---|
| `llm/schemas.py`, `llm/prompts.py` | Sonnet | 6 extraction schemas, `REQUIRED_FIELDS`, `pii` tags, 3 prompt builders | Read in full; ran the schema and prompt smoke checks myself | Nothing in the files. It correctly flagged that nested line items did not fit the flat `FieldValue` state type and left it for me; I handled that in `nodes.py` and `mask.py` |
| `rules/parse.py`, `tests/test_parse.py` | Sonnet | PDF, DOCX and image reading, page cap, downscaling, 9 tests | Read in full; re-ran the 9 tests myself | Nothing |
| `rules/validate.py`, `rules/snippets.py`, `tests/test_validate.py` | Sonnet | Snippet verification, required fields, dates, currency, invoice arithmetic, formats, 13 tests | Read in full; re-ran and then rewrote 6 tests | Two real defects, below |

**Two defects found by reviewing the agents against each other**, neither of which either
agent could have seen, because each only had half the picture:

1. `validate.py` assumed `line_items` was one field holding semicolon separated amounts,
   while `schemas.py` defines a list of rows. The list shape won. Snippet verification now
   recurses one level so nested cells are verified too, and the arithmetic rule sums the
   `amount` cell of each row.
2. The cross field date rule referenced `incident_date` and `claim_date`, while the schema
   defines `date_of_incident` and `date_filed`. The rule silently never fired. Both names are
   module constants now, because a mismatch here does not fail loudly, it just quietly stops
   checking.

I also rewrote six of the validation tests, which had been written against the assumed field
names and the semicolon shape rather than the real schema.

### Review findings addressed

An external review of the pushed branch raised three, all valid:

1. `graph.build` raised `ImportError` because `graph.nodes` was not yet pushed. Now present.
2. `pyproject.toml` declared a `consumer` package that does not exist until phase 5, breaking
   package discovery. Replaced with auto discovery rather than a list that has to be
   remembered later. Verified by building the wheel, not by reading the config.
3. `LLM_MAX_RETRIES` reached the OpenAI client and not the Bedrock one, so it silently did
   nothing in the default mode. Retries reach Bedrock through botocore, so the provider now
   passes `BotoConfig(retries={"max_attempts": llm_max_retries + 1, "mode": "adaptive"})`.
   The increment is because botocore counts total attempts while OpenAI counts retries;
   without it the same setting would mean two different things depending on the provider.
