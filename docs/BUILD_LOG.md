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

---

## Phase 3: synthetic sample documents

**Built.** `samples/generate.py`, producing eleven documents into `samples/out/`, plus a
README describing which path each one exercises.

All three formats the assignment names are present, and the formats are not decoration: each
reaches a different branch of `rules/parse.py`.

| Sample | Format | Path exercised |
|---|---|---|
| `claim_form_complete.pdf` | PDF | Happy path, outcome `COMPLETE` |
| `claim_form_incomplete.pdf` | PDF | Missing required fields, outcome `INCOMPLETE` |
| `claim_form_bad_dates.pdf` | PDF | The cross field rule, which triggers the retry cycle |
| `invoice_repair.pdf` | PDF | Invoice extractor and the line item arithmetic |
| `policy_document.docx` | DOCX | `python-docx`, including table cells |
| `customer_correspondence.docx` | DOCX | Generic extractor on prose |
| `identity_card.png` | PNG | Image path, verification skipped, PII masking |
| `damage_photo.jpg` | JPG | Supporting evidence, a standalone image |
| `claim_form_scanned.pdf` | PDF, image only | The `pypdfium2` render path |
| `restaurant_menu.pdf` | PDF | Out of scope, routes to `UNSUPPORTED` |
| `corrupt_encrypted.pdf` | PDF | Unreadable, terminal failure, `FAILED` |

**Verified.** Every sample fed through the real `rules/parse.py`:

```
claim_form_bad_dates.pdf         text=  486 images=0 image_only=False
claim_form_complete.pdf          text=  486 images=0 image_only=False
claim_form_incomplete.pdf        text=  430 images=0 image_only=False
claim_form_scanned.pdf           text=    0 images=1 image_only=True
corrupt_encrypted.pdf            UNREADABLE: PDF is encrypted and could not be opened
customer_correspondence.docx     text=  580 images=0 image_only=False
damage_photo.jpg                 text=    0 images=1 image_only=True
identity_card.png                text=    0 images=1 image_only=True
invoice_repair.pdf               text=  579 images=0 image_only=False
policy_document.docx             text=  652 images=0 image_only=False
restaurant_menu.pdf              text=  393 images=0 image_only=False
```

And the rule sensitive ones through the real `rules/validate.py`:

```
complete   -> missing=[] errors=[]
bad_dates  -> missing=[] errors=['date_of_incident is after date_filed']
invoice    -> missing=[] errors=[]
```

Determinism checked by regenerating into a fresh directory and comparing byte for byte. Ten
of eleven are identical; see the encrypted fixture below for why one is not. The three images
were also inspected visually, which no automated check covers.

### Decisions worth defending

**The samples are built against the real rules, not to look plausible.** The identity and
policy numbers match the regexes in `validate.py`, the invoice line items sum exactly to the
stated total with one amount carrying a thousands separator to exercise the currency parser,
and the bad dates sample violates the one cross field rule and nothing else, so a failure
there points at the rule rather than at the document.

**The scanned PDF genuinely has no text layer.** It is built by rendering the claim form to
an image and embedding that, rather than by a PDF that merely declares itself scanned.

**The DOCX carries a table**, because `python-docx` skips table cells when only paragraphs
are read. Without a table the sample would not catch that bug.

**`reportlab` is in `samples/requirements.txt` only**, never in `worker/pyproject.toml`. The
generator is development tooling and is not shipped in any container image.

**The encrypted fixture uses a throwaway password**, generated per run and never returned,
logged or stored. This is the one sample whose bytes differ between runs. Keeping it
deterministic would require the password to be reproducible, which is the same as keeping it.
Raised in review, and the earlier committed constant was the wrong answer even though the
file was always unreadable to the platform, which tries only the empty password.

### Delegation

| Delegated | Model | What I verified | What I changed |
|---|---|---|---|
| The whole generator | Sonnet | Read it in full, re-ran every check myself, fed all 11 through the real parser and validator, regenerated for determinism, looked at the three images | Nothing at the time. The agent found and fixed two issues on its own review pass: a clipped expiry date on the identity card, and two business names that read as plausibly real |

Later corrected after review: the committed encryption password, the stale README, and the
damage photo described below.

---

## Phase 4: the graph against a real model

**Built.** `worker/run_local.py`, which runs the graph over every sample from files on disk.
No AWS and no database, which is only possible because the graph makes no AWS call: fetching
bytes is the consumer's job and here it is a file read.

**Verified.** All eleven documents, `gpt-4o-mini`, 62 seconds.

```
claim_form_bad_dates.pdf         claim_form                 INCOMPLETE
claim_form_complete.pdf          claim_form                 COMPLETE
claim_form_incomplete.pdf        claim_form                 INCOMPLETE
claim_form_scanned.pdf           claim_form                 COMPLETE
corrupt_encrypted.pdf            unknown                    FAILED
customer_correspondence.docx     customer_correspondence    COMPLETE
damage_photo.jpg                 supporting_evidence        COMPLETE
identity_card.png                identity_document          COMPLETE
invoice_repair.pdf               invoice                    COMPLETE
policy_document.docx             policy_document            COMPLETE
restaurant_menu.pdf              unknown                    UNSUPPORTED
```

The retry cycle fired twice without being staged: on `claim_form_bad_dates` as designed, and
on `policy_document` where the model's first extraction cited a snippet that did not verify.
Both appear in the persisted trace. The encrypted file failed terminally without a single
model call.

### Three bugs the unit tests structurally could not find

The tests drive a fake, which proves the wiring. Running a real model on real documents
proved three things the wiring tests could not:

1. **The generic extractor's `summary` field republished a masked name.** It came back
   reading "a letter from <name> about a claim" while the name field beside it was correctly
   masked. Masking a field is useless if another field quotes the value, so every value is
   now scrubbed, not only the values of fields that are themselves tagged sensitive.
2. **`incident_location` held the claimant's full home address.** Scrubbing could not catch
   it, because the address field held a longer string and the match is exact, so the field is
   now tagged sensitive in its own right. A location tied to a named person is personal data
   whatever the field is called.
3. **The persisted trace stopped at `validate`**, which reads like a run that died halfway.
   `mask_pii` writes the masked copy while running, so that copy cannot contain `mask_pii`.

A scan for every synthetic identifier across all eleven reports now returns nothing.

### Deviation from ARCHITECTURE.md

**The classifier's rationale is dropped, not scrubbed, on the unsupported route.** Raised in
review. The redaction set is built from the extracted sensitive fields, so on any route that
extracts nothing it is empty and scrubbing is a no-op that still looks like a control. That is
exactly the unsupported route, where the document was never understood well enough to extract
from and the model's free text about it is at its least predictable. The rule is now that
model free text is persisted only where a deterministic redaction set exists to run over it.

I considered deriving a redaction set independently by scanning the source text for
identifier-shaped patterns, and did not: a regex PII detector is incomplete by construction,
and shipping one would claim a guarantee the code cannot make. Dropping the field claims
nothing.

### The damage photo, and what it taught

`damage_photo.jpg` first came back `UNSUPPORTED`, so no sample exercised the
`supporting_evidence` type the assignment names. Asked plainly what it saw, the model
answered that the image was a stylised illustration and could not plausibly be a photograph
submitted as evidence.

**That was the classifier being right, not wrong**, and it is the reason to check which
component is at fault before changing either. No amount of prompt tuning fixes a sample that
does not depict what it claims to.

What makes a photograph supporting evidence is not the pixels, it is the submission around
them: real evidence reaching an insurer carries a claim reference, a caption and a date,
because otherwise nobody can tell which claim it belongs to. The sample now carries the same,
which also gives the generic extractor a reference number and a date to find, so the type
exercises its extraction path rather than merely reaching it. It now classifies as
`supporting_evidence` at 0.95 confidence and routes to `extract_generic`.

---

## Phase 5: Backend API and SQS consumer

**Built.** The two services either side of the queue, and the schema they share.

| Part | Files | Owner |
|---|---|---|
| The shared schema | `backend/app/models.py`, one Alembic migration | Written first, before either service |
| Backend API | `backend/app/` | Delegated, then reviewed and mutation tested |
| SQS consumer | `worker/consumer/` | Written by hand, not delegated |

`worker/consumer/` was kept off the delegation list deliberately. The claim statement, the
ack rules, the lease heartbeat and the reaper are where this design is either correct or
quietly loses work.

**Verified.** Real PostgreSQL in Docker, schema created by running the backend's own Alembic
migration, so the SQL under test runs against the real table rather than a copy that can
drift from it.

```
worker:   90 passed
backend:  14 passed
ruff:     All checks passed
mypy:     Success: no issues found in 34 source files
```

### Decisions worth defending

**The claim is one conditional UPDATE**, so two workers racing cannot both win: the loser's
`WHERE` no longer matches once the winner commits. Claimable statuses are named explicitly
rather than inferred from an expired lease, because a finished document's lease is expired
too, and without the list every `COMPLETED` document would be reclaimable forever on any
redelivery.

**`attempt_count` is a fencing token.** Checking that the row is `PROCESSING` with a live
lease says a lease is held; it does not say who holds it. A worker whose lease lapsed, whose
document was reclaimed, and which then woke up and beat, would push the lease out again and
pass that check while another worker owned the document. Reproduced end to end against a real
database: A's write landed on B's row. The claim increments and returns the count, so a
reclaim invalidates the previous holder's token permanently.

**Acking is the only irreversible thing the consumer does**, since deleting a message
destroys the only copy of that work. A message is deleted when the document is provably
finished, `COMPLETED` or `FAILED` and nothing else, or when redelivery provably cannot help:
an unparseable body, or an object with no row. Anything merely unclear, including a live
lease held by another worker, is left to time out.

**One heartbeat extends the lease and the visibility timeout together.** Extending them
separately lets them disagree, and both directions are bugs: a visibility timeout outlasting
the lease lets a second worker claim a document nobody will redeliver; a lease outlasting the
visibility timeout hands the message to a worker who cannot claim it and spins to the redrive
limit.

**The report is published only after the database write succeeds.** Raised in review.
Publishing first meant a worker that had already lost the lease still overwrote the winner's
report and only then discovered its own write was refused, leaving a row describing one run
beside a stored report from another, with no error raised anywhere. The remaining failure, a
status recorded with no report, is visible: the report route returns 404. A missing answer
everyone can see beats a wrong one nobody can.

**The reaper's two sweeps are asymmetric on purpose.** A row stuck in `PROCESSING` is failed
only once its lease is expired by more than the whole redrive window, so a worker with a
briefly late heartbeat is reclaimed by redelivery rather than declared dead. An abandoned
upload gets `EXPIRED`, not `FAILED`, because it is a guess a later event can disprove.

**The object key carries no filename.** S3 URL encodes keys in notifications and encodes
spaces as `+`, so a filename with a space would arrive as a key that does not exist.

**The health check touches nothing.** It is a container health check; one that fails when the
database blips would have ECS killing tasks that are fine.

### Mutation testing

A passing test proves nothing until it has been watched to fail. Each of these breaks one
rule and fails exactly the test written to catch it:

| Mutation | Test that caught it |
|---|---|
| Expired-lease clause no longer scoped to `PROCESSING` | `test_finished_documents_are_never_reclaimed` |
| The fencing token removed from all three statements | `test_a_superseded_worker_cannot_write_after_the_document_is_reclaimed` |
| Completion write no longer checks the lease | `test_a_worker_that_lost_its_lease_cannot_write_a_result` |
| Reaper loses its grace period | `test_reaper_leaves_a_recently_expired_lease_alone` |
| Ack anything not claimable, not just terminal | `test_a_document_held_by_another_worker_returns_the_message` |
| Mark failed on every fetch failure, not only the last | `test_a_fetch_failure_returns_the_message_when_attempts_remain` |
| Publish the report before the ownership check | `test_a_worker_that_lost_its_lease_writes_nothing_at_all` |
| Drop the `status='UPLOADING'` condition | `test_upload_complete_does_not_move_processing_backwards` |
| Drop the HeadObject check | `test_upload_complete_without_object_is_409...` |
| Put the filename back into the S3 key | `test_presigned_key_is_uploads_prefix_document_id_with_no_filename` |

### Delegation

| Delegated | Model | What I verified | What I changed |
|---|---|---|---|
| The whole Backend API: routers, config, S3 presigning, schemas, tests | Sonnet | Read every file, ran its tests myself, mutation tested its three guards | Nothing in its files. It correctly flagged a `StrEnum` lint issue in my own `models.py` and correctly refused to touch it |

Split into one agent rather than two: the routers, schemas and tests are one overlapping file
set, and two agents would have been editing the same files.

---

## Phase 6: frontend

**Built.** React and Vite behind `nginx-unprivileged` on 8080, plus the LocalStack init
script the next phase needs.

**Verified.**

```
$ npm run build            # tsc --noEmit && vite build
  38 modules transformed, built in 214ms

$ docker run ... nginxinc/nginx-unprivileged:1.27-alpine nginx -t
  20-envsubst-on-templates.sh: Running envsubst on .../default.conf.template
  nginx: configuration file /etc/nginx/nginx.conf test is successful
```

Rendered with the AWS values through the image's real entrypoint, not a hand rolled
substitute.

### Decisions worth defending

**The six polling rules**, all in `src/usePolling.ts`, because section 11 chooses polling over
SSE and the choice is only defensible if the rules are exact: 3 seconds while anything is in
flight, 15 once nothing is, refetch on window focus, a toast only on the transition into a
terminal state rather than on every poll that still sees one, no toast on the first poll, and
`EXPIRED` treated as settled. The last one matters: it is not terminal in the backend, since
a late S3 event can still claim the row, but an abandoned upload would otherwise keep the
fast poll running forever.

**A failed poll is not retried specially.** The next tick is the retry and it returns the
full current state, so there is nothing to reconcile and no backoff to tune. That is the
whole reason polling was chosen: the failure mode is a delayed update, never a wrong one.

**Each poll takes a ticket.** Raised in review. A focus event could start a poll while one
was in flight; both then updated state, so an older response could overwrite newer statuses,
and both reached the `setTimeout`, so the single loop became two and every subsequent focus
event doubled it again. Only the holder of the newest ticket may touch state or schedule the
next tick.

**The nginx upstream is held in a variable.** nginx resolves a plain hostname once and caches
it for the life of the process, and ECS task addresses change on every deploy. A variable in
`proxy_pass` is what forces re-resolution; without it the `resolver` directive has no effect
on that upstream at all. `proxy_next_upstream` covers the ten seconds of staleness that
remain during a rolling deploy.

**The resolver address is an environment variable**, because it is `169.254.169.253` on AWS
and `127.0.0.11` under compose. The config ships as a template and the image's own entrypoint
runs `envsubst` over it, so there is no custom entrypoint script.

**`/healthz` is served by nginx itself**, not proxied. The frontend is still serving the
application correctly when the backend is down, and a target group that failed then would
take out a service that is working.

### The LocalStack pin

`4.14`, the last release before `2026.03.0` merged the community and pro images into one that
refuses to start without a `LOCALSTACK_AUTH_TOKEN`. Verified against the Docker Hub tag API
and then by starting it: no token, no account, no signup.

The S3 to SQS notification was verified by hand rather than assumed, because that event is
the actual trigger in this design and a local stack that faked it would prove nothing:

```
after writing reports/aaa.json      -> queue depth 0
after writing thumbnails/x.png      -> queue depth 0
after writing uploads/1111...1111   -> queue depth 1
```

That prefix filter is what stops the worker triggering on its own report writes into the same
bucket, which would otherwise loop forever.

**One bug found in my own work:** the init script created the queue in `us-east-1` while the
bucket and every client sat in `ap-southeast-1`, because `awslocal` defaults to `us-east-1`
inside the container and SQS queues are region scoped. Nothing could find the queue.

### Delegation

| Delegated | Model | What I verified | What I changed |
|---|---|---|---|
| React pages, polling hook, toasts, report view | Sonnet | Read the polling hook line by line against section 11, read the S3 field ordering | The polling ticket, after review found the focus race |
| `package.json`, Vite and TypeScript config, `index.html`, the nginx template | Sonnet | Re-ran the build and `nginx -t` myself, read the rendered config | Nothing |

Both agents' output built together on the first attempt. The API contract they coded against
was generated from the running backend and committed first, so neither had to guess at the
other side's shape.

## Phases 7 and 8: the local end to end stack, and the Dockerfiles

**Built, together and on purpose.** `docker-compose.yml` runs the whole platform locally:
LocalStack for S3 and SQS, PostgreSQL, and the three services. The Dockerfiles were pulled
forward from phase 8 so that compose builds the same images that get deployed. A compose file
that ran the services some other way would have proved something other than what ships.

| Image | Base | Notes |
|---|---|---|
| `worker` | `python:3.12-slim` | No entrypoint wrapper, so the process is PID 1 and SIGTERM from ECS reaches it. The health check reads a timestamp the poll loop writes, because a liveness check passes for a wedged loop |
| `backend` | `python:3.12-slim` | Runs Alembic at startup under an advisory lock. `curl` is present only for the container health check, since the API has no target group |
| `frontend` | `nginx-unprivileged:1.27-alpine` | Listens on 8080. `/healthz` is served by nginx itself, so it does not fail when the backend is down |

All three are multi stage, arm64, run as a non root user, and install dependencies in their
own layer before application code is copied.

**Verified.** Every sample was driven through the running stack from the browser upload to
the stored report, with nothing in the path stubbed. The output is committed under
`docs/evidence/`: the outcome of each document, the worker's structured logs with
`document_id` bound per message, and one complete report as stored in S3.

**Two fixes the real run surfaced, which no unit test could have found.**

1. `current_step` was written by the heartbeat, which beats every 30 seconds. Documents
   finish well inside that, so the progress the frontend promises never appeared. It is now
   written as each node completes, through its own fenced statement, so that reporting
   progress cannot renew a lease as a side effect.
2. The report view labelled every unverified field as image only input. A field with no value
   was never extracted, so there was nothing to check. The two cases are now told apart.

---

## Review fix: the report and the status are one step

A review on PR 5 found the last unguarded gap in the at-least-once story, and it was a real
one. `_record` committed `COMPLETED`, then published the report, then swallowed any failure
of that publication and acked the message.

The part that makes it unrecoverable is the ack rule two functions away. A returned message
would be redelivered, the redelivery would find the document terminal, and the terminal
branch deletes the message without reaching the publish at all. So nothing anywhere retried:
one transient S3 error meant the report route returned 404 for the life of that document, and
the worker logged it and moved on.

Reproduced first, as a failing test, before anything was changed:

```
assert status_of(conn, document_id) == "PROCESSING"
E   AssertionError: a document whose report was never stored must not be left terminal,
E   because nothing would ever retry it
E   assert 'COMPLETED' == 'PROCESSING'
```

### The fix

The status write is now held open until the report is durable. `mark_completed` and
`mark_failed` take `commit=False`, the consumer stores the report, and only then commits.
A failed put rolls the transaction back, so the document stays `PROCESSING` with its lease
running and the message is returned for SQS to redeliver the whole run.

What makes that safe is the row lock the uncommitted `UPDATE` holds. Nobody else can take the
document while the report is being written.

**The first version of that lock test failed, and the failure was worth more than the test.**
It set up a live lease, and no lock was ever contended:

```
>       with pytest.raises(psycopg.errors.LockNotAvailable):
E       Failed: DID NOT RAISE LockNotAvailable
```

The reason is that `CLAIM_SQL` never matches a row whose lease is live, so a competing claim
is refused outright and never reaches the lock. The lock only matters in the narrow window
where the lease lapses after the status write and before the put finishes. The test now
claims with a one second lease and sleeps past it, which is the real case.

### A second finding, mine

The fix only recovers if the returned message actually comes back to a claimable document.
`lease_seconds` and the queue's `VisibilityTimeout` are both 120, deliberately, so that the
two can never disagree. But the heartbeat set the visibility timeout first and the lease
second, so the lease always landed a few milliseconds further out. A returned message became
visible fractionally before its own lease died, the redelivery refused its own claim, and one
of three attempts was spent doing nothing. The two calls are now the other way round.

### Verification

100 tests, up from 91. Six mutations, each failing exactly its own test and nothing else:

| Mutation | Test that failed |
|---|---|
| `commit=True` on the status write | `assert 'COMPLETED' == 'PROCESSING'` |
| the `conn.commit()` after a successful put removed | `assert 'PROCESSING' == 'COMPLETED'` |
| visibility extended before the lease | `assert ['visibility', 'lease'] == ['lease', 'visibility']` |
| the visibility timeout extended after the put rather than before it | `assert ['put', 'visibility:120'] == ['visibility:120', 'put']` |
| the extension moved back outside the rollback | `RuntimeError: Throttled: rate exceeded` escapes `_record` |
| the poll loop guard removed | `RuntimeError: the database went away` escapes `_handle` |

```
100 passed in 3.56s
Success: no issues found in 35 source files
All checks passed!
```

The second mutation is the one the earlier tests could not have caught: both existing publish
tests replace `mark_completed`, so neither would have noticed a status write held open and
never committed, which would strand every document in `PROCESSING`. That test reads the row
back on a second connection so an open transaction on the first cannot hide the result.

**Separately, not fixed here:** `ruff format --check` reports 12 files on `main` and 11 here.
Formatting has never been part of the gate, only `ruff check`. CI in phase 10 should either
not run `ruff format --check` or land a formatting pass of its own, rather than mixing an
unrelated reformat into this change.

### A third review finding, on the window the heartbeat no longer covers

The review pointed out that the heartbeat context closes before `_publish_and_commit`, so
nothing extends the visibility timeout across the two writes that follow. A `put_object` that
botocore retires through its own retries can outlast whatever the last beat bought, and the
message is then redelivered while this transaction still holds the row lock.

The window is real. The stated consequence, a delete failing on a stale receipt handle, is
not what happens: `DeleteMessage` with a superseded receipt handle is documented as possibly
not deleting the message rather than as an error, and nothing is lost either way. The
redelivered worker blocks on the row lock, waits for the commit, then finds the document
terminal and deletes the message with its own valid receipt.

What it actually costs is a delivery attempt, and three of those send a perfectly finished
document to the dead letter queue and fire the alarm on it. That is worth closing, so the
visibility timeout is now pushed out to a full window immediately before the report is
written.

The lease is deliberately not extended with it, which breaks the "one number for both" rule
on purpose and for one bounded stretch: the row lock, not the lease, is what protects the
document between the status write and the commit, and on a rollback a lease that lapses
sooner is exactly what lets the redelivery reclaim the document instead of refusing itself.

### A fourth finding, and the shape behind it

The review then found that the new `extend_visibility` call sat outside the rollback. It is
right, and the consequence is the one it names: an SQS throttle there escapes every frame up
to the poll loop and ends the worker, with an uncommitted terminal status still open. The
document itself survives, because the dying connection rolls back, but a throttle should not
cost a task restart. The call is inside the try now, so a failed extension rolls back and
returns the document.

**The same shape was in four other places, and only one of them was reported.** `connect()`,
`claim()`, `mark_completed()` and `self._delete()` can all raise on a transient fault, and
each one ended the poll loop. So the fix is not only the one line: `_handle` now catches
around `_process`, logs, and leaves the message alone. `with connect() as conn` has already
rolled back any open transaction by the time the handler sees the exception, and the message
is never deleted, so it returns after the visibility timeout.

**The accepted cost, stated because it is a real trade and not a free win.** During a
sustained outage every delivery now fails, so messages reach the dead letter queue after the
redrive limit instead of waiting in the queue for a restarted task. That is still the better
trade: the DLQ alarm makes the outage visible and `StartMessageMoveTask` redrives the
messages in one call, whereas a crash loop is silent until someone reads the service events.

---

## Review fix: reproducible images, and four smaller findings

An external review of phases 1 to 8 on `main` re-ran every check independently (100 worker
tests, 14 backend tests, lint, types, the frontend build, and a real upload through the
running stack) and found the following. All are fixed in one change.

**1. Python dependencies were not locked.** Both Dockerfiles ran `pip install .` against the
`>=` ranges in `pyproject.toml`, and `uv.lock` was gitignored, so two builds of one commit
could ship different library versions. The assignment asks for appropriate container build
practices and reproducible deployment, and this was the one place phases 1 to 8 fell short.
Both lock files are now committed, and the builder stage installs with
`uv sync --frozen --no-dev --no-install-project`. `--frozen` fails the build if the lock is
out of step with `pyproject.toml`. `uv` is copied into the builder only and is absent from
the runtime image, which was checked rather than assumed. The application is no longer
installed as a package, because the runtime stage already runs it from source in `/app`.
Side effect: the images shrank, worker 606 MB to 508 MB and backend 427 MB to 368 MB as
reported by `docker images`.

`.terraform.lock.hcl` was also gitignored. It pins provider versions and checksums and is
meant to be committed, so it is no longer ignored ahead of phase 9.

**2. The report view rendered a repeating group as one blank row.** An invoice's
`line_items` is a list of rows whose cells each carry their own snippet. The table showed an
empty value and the label "image only input" for a text PDF, which is wrong twice. The view
now flattens a repeating group into one row per cell, named like `line_items[1].amount`.
Verified in a browser against a real report: 25 rows, every line item cell `Verified`.

**3. Log timestamps carried no zone.** Worker lines read `2026-09-20 23:21:28` with no
offset, and backend lines had no timestamp at all. Both now emit ISO 8601 with an explicit
offset.

**4. Three backend tests had no type annotations**, which failed `mypy` and would have failed
the phase 10 CI job. Annotated. `mypy` is clean on both services.

**5. This log had no entry for phases 7 and 8.** Added above.

**Verified after the change.**

```
backend:  14 passed   ruff clean   mypy clean
worker:   100 passed  ruff clean   mypy clean
frontend: tsc --noEmit && vite build, clean
images:   worker and backend build from uv.lock, import their code, alembic present,
          uv absent from the runtime stage
```

---

## Phase 9: Terraform, and the first deployment

**Built.** Two stacks, as `ARCHITECTURE.md` section 9 lays out.

| Stack | State | Contents |
|---|---|---|
| `infra/bootstrap/` | Local, applied once by hand | State bucket, three ECR repositories, the empty OpenAI key secret, the GitHub OIDC provider, a read only plan role, and a deploy role assumable only from the reviewer gated GitHub environment |
| `infra/` | S3 backend with native locking | VPC with public, app and worker tiers, five interface endpoints plus the S3 gateway endpoint, a NAT gateway on the worker route table only, six security groups, S3 with the `uploads/` event filter, SQS with a DLQ and an alarm, RDS, IAM, the load balancer, and one reusable `ecs_service` module used three times |

**Applied.** Bootstrap: 17 resources. Main stack: 91 resources in about six minutes, then one
more for an allowlist change. Images were built for arm64 and pushed under the git SHA before
the main stack was applied, which is the ordering the bootstrap stack exists to make possible.

**Verified on AWS**, from outside the VPC, through the load balancer. The output is committed
under `docs/evidence/`.

```
frontend   desired 1  running 1  rollout COMPLETED
backend    desired 1  running 1  rollout COMPLETED
worker     desired 1  running 1  rollout COMPLETED
target 10.0.11.47:8080  healthy

api.docintel.internal.  A  ttl 10  10.0.10.35      (private hosted zone, owned by Cloud Map)

route table docintel-dev-app      default route: none
route table docintel-dev-worker   default route: nat gateway
route table docintel-dev-public   default route: internet gateway

POST /api/documents                -> 201, upload url https://s3.ap-southeast-1.amazonaws.com/...
POST to S3 with an Origin header   -> 204, Access-Control-Allow-Origin: the load balancer
POST /upload-complete              -> QUEUED
12 seconds later                   -> COMPLETED, COMPLETE, claim_form
```

The worker's own startup log shows three design decisions holding on real infrastructure: it
waited for the schema while the API ran the migration, it polled SQS through the VPC endpoint,
and it ignored the `s3:TestEvent` S3 sends when a notification is created.

The allowlist was proven by accident. The machine moved to a different network between the
plan and the test, so its public address changed, and the load balancer stopped answering: the connection timed out rather than
returning an error, which is what a security group does. Adding the new address was one rule.

### Four things the real deployment surfaced

None of these could have been found by `terraform validate`, which passed throughout.

1. **`count` cannot depend on a value unknown until apply.** The module decided whether to
   create the Cloud Map service by testing `discovery_namespace_id` for null. The id does not
   exist while planning, so the first plan failed with `Invalid count argument`. Two plain
   booleans, `register_in_dns` and `attach_to_load_balancer`, decide it now.
2. **An empty `health_check_custom_config {}` block is not persisted by the provider.** Every
   later plan wanted to replace the Cloud Map service, which would have dropped the API's DNS
   record during the replacement. The block is removed. ECS adds and removes a task's address
   as the task starts and stops, and a task failing its container health check is stopped by
   ECS, so DNS follows task health without it.
3. **The presigned POST has to be SigV4 and has to name the regional endpoint.** boto3 signs a
   presigned POST with the legacy scheme by default, and the global S3 endpoint answers a newly
   created bucket with a redirect that a browser will not follow on a cross origin POST. The
   API now sets `signature_version="s3v4"` and Terraform sets
   `S3_PUBLIC_ENDPOINT_URL=https://s3.ap-southeast-1.amazonaws.com`.
4. **The API cannot scale on request count.** `ARCHITECTURE.md` said CPU and request count.
   Request count is a load balancer metric, and the API deliberately has no target group. It
   scales on CPU, and the document now says why.

### Two deliberate deviations, both written into ARCHITECTURE.md

- **The Bedrock interface endpoint exists only in bedrock mode.** Nothing calls Bedrock in the
  OpenAI fallback, and an idle interface endpoint still bills by the hour.
- **Security group rules live in their own file**, `security_groups.tf`, one resource per rule,
  so two groups can reference each other without a dependency cycle.

### Decisions worth defending

**Three subnet tiers, not two.** The worker has its own subnets and route table so that
`enable_nat` can only ever give the worker an internet route. With a shared private tier, the
fallback would have given the API and the database's subnets a default route as well.

**`llm_provider = "openai"` with `enable_nat = false` fails at plan time**, through a variable
validation. The alternative is a worker that deploys cleanly and then cannot reach its model.

**`allowed_cidrs` rejects `0.0.0.0/0` and an empty list.** The platform has no end user
authentication, so that list is the access control, and behind it is a model bill.

**The two stacks share no state.** The main stack finds the ECR repositories and the OpenAI
secret by name through data sources, so bootstrap can stay on local state and nothing reads
another stack's state file.

**The CI deploy role is AdministratorAccess, and the control is who can assume it.** Its trust
policy accepts only the subject claim of the reviewer gated GitHub environment, so a push to
main cannot assume it. A least privilege policy for a stack that creates IAM roles, a VPC, RDS
and ECS is its own project, and is named as the production approach in the code.

**Cost.** About 6 USD a day while it runs, of which the NAT gateway is 1.55 and exists only
because Bedrock is blocked. The environment is meant to be applied, verified and destroyed,
and `destroyable = true` in `dev.tfvars` is what makes destroy and recreate work: forced
bucket and repository deletion, no final RDS snapshot, and a zero day secret recovery window.

**Delegated.** Nothing. The plan kept networking and IAM off the delegation list, and the rest
of the stack was small enough that splitting it would have cost more in interface agreement
than it saved.

---

## Phase 10: CI/CD

**Built.** Two workflows, as `ARCHITECTURE.md` section 12 lays out.

| Workflow | Trigger | AWS role it can assume | What it does |
|---|---|---|---|
| `ci.yml` | every pull request | `docintel-ci-plan`, read only | `ruff`, `mypy` and `pytest` for the backend and the worker against a real PostgreSQL, the frontend build, three arm64 image builds with no push, and `terraform fmt`, `validate` and `plan`, with the plan posted on the pull request |
| `deploy.yml` | `workflow_dispatch` only | `docintel-ci-deploy`, from the `dev` environment only | Publishes the three images under the git SHA, applies Terraform with that tag, waits for the services to be stable, checks each service runs the published image, and checks the target is healthy |

**Verified.** The pull request that adds the workflows ran them on itself.

```
success  python (backend)     success  image (frontend)
success  python (worker)      success  image (backend)
success  frontend             success  image (worker)
success  terraform

Terraform plan (dev), posted on the pull request by the read only role:
No changes. Your infrastructure matches the configuration.
```

That last line is worth more than it looks. It is CI, with a role that cannot write,
confirming that what is deployed is exactly what the code describes.

### What the first run surfaced

**The OIDC subject claim carries immutable ids.** The plan job failed with
`Not authorized to perform sts:AssumeRoleWithWebIdentity`. A step that prints the subject
claim (an identifier, never the token) showed why:

```
repo:AfiqAqil@152358148/docintel-platform@1378307019:pull_request
```

The trust policies were written against the older `repo:<owner>/<repo>:<context>` form, which
matches nothing. Both roles now build the subject from the owner and repository ids. This is
also the safer form: a repository name can be released and registered again by someone else,
and an id cannot. The debugging step stays in the workflow, because the next person to see
"not authorized" should get a string comparison and not a guess.

**Local bootstrap state is a hazard with worktrees.** Planning that fix from a fresh worktree
proposed creating all 17 bootstrap resources again, because the state file lived only in the
worktree that first applied it. It was not applied. The state was copied across, the plan
became two in place updates, and the stale copy was renamed so it cannot be applied by
mistake. The file holds no secret values.

**So the bootstrap state was moved into the bucket it creates.** `ARCHITECTURE.md` said
"local state, run once", and that turned out to be a liability rather than a simplification:
the file was gitignored, lived in one worktree, and worktrees are deleted after a merge.
Losing it would have broken nothing in AWS and would have made the CI roles, the registries
and the final teardown unmanageable without importing 17 resources by hand. It is now
`bootstrap/terraform.tfstate` in the versioned, encrypted state bucket.

```
$ terraform init -migrate-state -force-copy -backend-config=backend.hcl
  Successfully configured the backend "s3"!
$ aws s3api head-object ... bootstrap/terraform.tfstate   -> 32382 bytes, AES256, versioned
$ terraform state list | wc -l                            -> 17, read from S3
$ terraform plan -detailed-exitcode                       -> exit 0, no changes
```

The circularity is real and happens exactly once. The first apply in a new account has to run
on local state, because the bucket does not exist yet, and a full teardown has to move the
state back out first, because the bucket cannot hold the state of its own destruction. Both
procedures are written at the top of `infra/bootstrap/backend.tf`, where someone will be
looking when they need them.

### The deploy run

`deploy.yml` can only be dispatched once it is on `main`, so it was run once after the merge.
The merge itself started nothing, which is the first gate doing its job.

```
event       workflow_dispatch        branch  main        commit  f5bf8f6a8e37
success  build and publish images    three arm64 images pushed as :f5bf8f6a8e37
success  terraform apply and verify  Apply complete! Resources: 3 added, 3 changed, 3 destroyed.
           image  ...:834e970af72a -> ...:f5bf8f6a8e37   (frontend, backend, worker)
           Wait for the three services to be stable
           Every service runs the image that was just published
           The load balancer target is healthy           10.0.10.61  healthy
about 5 minutes, start to finish
```

Three added, three changed, three destroyed is exactly an application deploy: three new task
definition revisions, three services pointed at them, three old revisions retired. Nothing
else in the stack moved, which is what "the application is deployed by changing one variable"
is supposed to look like.

**Checked independently, because a green step is not evidence.** This log has already had to
correct one false green, so the run was not taken at its word. Read straight from AWS
afterwards: all three services on task definition revision 2, one task running each, rollout
`COMPLETED`, image tag `f5bf8f6a8e37`. Then an invoice uploaded through the load balancer
against the newly deployed images reached `COMPLETED` / `COMPLETE` in about 18 seconds, and
the document uploaded before the deploy was still listed after it. The full record is
`docs/evidence/ci-deploy-run.txt`.

It was also the first time the administrator deploy role was assumed. Its trust policy had
only been proven by analogy with the read only role until then.

### An external review found two real defects, and one of them had already bitten

**1. A failing test run could go green, and it had.** With no `shell` named, GitHub runs a
step as `bash -e {0}`, where a pipeline's exit status is that of its last command. The step
was `pytest | tee pytest.out`, so it succeeded whenever `tee` did. Checking the run this log
had just recorded as green showed the worker job's real result:

```
84 passed, 16 errors
```

So the "success python (worker)" line in the verification block above was false, and it is
left there on purpose, next to this correction. Both workflows now set
`defaults.run.shell: bash`, which GitHub expands to `bash -eo pipefail`. The same flaw sat in
two places the review did not name, `terraform plan | tee` and the target health check, and
they are covered by the same line. Reproduced locally before fixing:

```
$ bash -e -c 'false | tee /dev/null; echo continued'            -> continued, exit 0
$ bash -eo pipefail -c 'false | tee /dev/null; echo continued'  -> exit 1
```

**The 16 errors were a real test isolation bug.** Only `test_claim_sql.py` ran the backend's
migration, through a fixture local to that module. `test_ack_rules.py` assumed the table
already existed. Locally it always did, because the backend's tests had run against the same
database first. In CI each service gets its own empty database, and the ack rule tests, the
ones covering what the consumer deletes and what it leaves, never ran. Reproduced on a brand
new database (84 passed, 16 errors), then fixed by moving the migration into one session
fixture in `conftest.py` that both modules' `conn` fixtures depend on. On a new database:
100 passed, and `test_ack_rules.py` alone passes too.

**2. The deploy could run unreviewed code under an administrator role.** The workflow took a
free form `ref` input and both privileged jobs checked it out. The `dev` environment admits
only runs dispatched from `main`, but that governs which workflow file runs, not which commit
it checks out. Anyone able to press the button could have deployed any reachable branch, its
Dockerfiles and its Terraform, under `AdministratorAccess`, which made the main only
restriction decorative. The input is removed. A run deploys the commit it was dispatched
from, a guard step refuses any ref but `refs/heads/main`, and a rollback is a revert on
`main`, reviewed like any other change.

### Decisions worth defending

**The deploy workflow has no `push` trigger.** Merging to `main` starts nothing. This
repository deploys into one real AWS account, and an apply on merge would let a documentation
commit change running infrastructure, and would rebuild the stack after a deliberate destroy.

**The control on the deploy role is its trust policy, not the workflow file.** It accepts only
the `dev` environment's subject, and the environment is restricted to `main`. A workflow on
another branch, or a pull request from a fork, cannot mint that subject whatever its YAML says.

**A required reviewer was attempted and refused.** GitHub offers that rule for private
repositories only on paid plans, and the API returned 422. It is listed as a simplification in
`ARCHITECTURE.md` section 15 rather than claimed.

**A skipped test fails the build.** The worker's database suite skips itself when the
backend's virtualenv is missing, since it creates its schema with the backend's Alembic
migration. In CI a silent skip would be a green build that never ran the claim and reaper
tests, so the job greps for skips and fails.

**The target health check compares the whole field.** The first version tested whether the
line ended in `healthy`, and `unhealthy` does. Caught before the first run, and both cases
were tested.

**The pull request plan uses the running image tag.** It reads the tag from the live task
definition, so the plan shows the infrastructure change in the pull request and not a
redeploy of the application.

**arm64 runners, natively.** `ubuntu-24.04-arm` for every job. The images and the Fargate
tasks are arm64, so nothing is built under emulation, and a wheel that builds in CI is the
wheel that ships.

**The allowlist is a repository variable, not a secret and not a committed file.** It is not
a credential, it is machine specific, and CI needs it to plan.

**Delegated.** Nothing. The plan kept the deploy workflow and the OIDC trust policy off the
delegation list, and the pull request workflow was small enough to write alongside them.

---

## Phase 11: the README, and a last pass over the documents

**Built.** `README.md`, written for an engineer who has never seen this repository: run it
locally, run the tests, provision it on AWS in six numbered steps, deploy through the
pipeline, switch LLM mode, and tear it down, including the bootstrap stack and the one extra
step its state needs. It lists the values that are specific to an AWS account and a GitHub
repository, so nobody has to find them by failing. It also maps every documentation item the
assignment lists to the section that covers it.

**Verified by running it, not by reading it.** The test section was executed exactly as
written, against the compose PostgreSQL, from a fresh checkout:

```
backend   14 passed    ruff clean   mypy clean
worker    100 passed   ruff clean   mypy clean
frontend  built
```

The provisioning steps are the commands that were actually used to stand the environment up
in phases 9 and 10, with the account specific values replaced by placeholders. The bootstrap
first run and teardown procedures are the ones written at the top of
`infra/bootstrap/backend.tf`.

**An external review found that the provisioning steps could not be followed as written.**
Step 1 changed into `infra/bootstrap` and never came back, while steps 2 to 4 assumed the
repository root, so `./.env`, `./frontend` and `infra/envs/...` all resolved to paths that do
not exist. The teardown had the same flaw, ending in `infra` and then changing into
`infra/bootstrap` from there. The test section had been run verbatim and the provisioning
section had not, because its commands were lifted from a session that happened to be in the
right directory each time, which is exactly the assumption a reader cannot share.

Every command now runs from the repository root, using `terraform -chdir`, and the README
says so once at the top of the section. Then the read only commands were run as written,
from the root, in a worktree where Terraform had never been initialised:

```
terraform -chdir=infra/bootstrap init -backend-config=backend.hcl   initialized
terraform -chdir=infra/bootstrap plan                               exit 0, no changes
terraform -chdir=infra init -backend-config=envs/dev.backend.hcl    initialized
terraform -chdir=infra plan -var-file=envs/... -out=dev.tfplan      No changes. infra/dev.tfplan written
terraform -chdir=infra output app_url                               the load balancer URL
describe-target-health with the target group ARN from output        healthy
terraform -chdir=infra/bootstrap output -raw ci_plan_role_arn       the role ARN
```

That confirms the detail the rewrite depends on: with `-chdir`, the paths given to
`-var-file`, `-backend-config` and `-out` are relative to the stack directory. The apply,
migrate and destroy commands were not run, since they change real infrastructure, and they
differ from the ones above only in the verb.

**One stale claim corrected.** `ARCHITECTURE.md` section 9 listed five outputs. The main stack
has twelve and the bootstrap stack five, several of which exist because the deploy pipeline's
checks read them, so the row now says what is really there.

**Every deliverable the assignment lists is present**: the three applications, the LangGraph
implementation, the Terraform, three Dockerfiles, both workflows, eleven synthetic documents
with a real report for each, the architecture diagrams, the documentation, and this README.

---

## Documents the platform had never seen, and the two defects they found

Every sample so far came from this repository's own generator, which means every sample was
shaped by the same assumptions as the code reading it. Six documents were produced by a
separate tool from a plain description, with no knowledge of the prompts or the rules, and
run through the deployed environment and then locally. All six were classified correctly.
Two results were wrong, and both were real defects.

### 1. A valid invoice came back INCOMPLETE, two times in three

The invoice's line items were a real table. In extracted text a table's header row and its
cells are far apart, and the model cited evidence as `"Amount\n425.00"`, joining a column
header to a cell. That span exists nowhere in the source, so snippet verification rejected
it, correctly, cleared all four amounts, and the invoice failed its arithmetic rule. The
repository's own invoice sample never exercised this, because its generator writes inline
labels (`Qty: 1`) and not a table.

Reproduced locally before changing anything, three runs of the same file:

```
run 1  INCOMPLETE  12 errors   run 2  INCOMPLETE  4 errors   run 3  COMPLETE  0 errors
```

Three runs, two outcomes, which was the second finding: no caller set a temperature, so
extraction ran at the provider's default randomness.

**Fixed in two places.** The shared snippet instruction now says a snippet is one contiguous
span copied character for character, and for a table cell, the cell's own text and nothing
else. `get_chat_model` defaults `temperature` to 0, in one place. Five runs after:

```
runs 1 to 5  COMPLETE  0 errors  1 extraction pass  amounts 425.00 780.00 165.00 120.00
```

### 2. A home address was published unmasked

The leak check that every set of reports goes through found a claimant's address in a report.
The field's value was masked. Its evidence snippet was not:

```
"value":   "•• ••••••••• ••••, ••••••, •••••••"
"snippet": "Address\n62 Mossglass Lane, Varrow,\nElandor"
```

The PDF wrapped the address onto a second line, so the snippet held a line break where the
value held a space. Snippet verification normalises whitespace and case, so the snippet
verified. Masking found values by exact text match, so it found nothing to redact. The two
rules disagreed about what "the same text" means, and the gap between them was exactly wide
enough for a wrapped address. The same gap existed for capitalisation: a name printed in
capitals would have escaped a value returned in title case.

A failing test came first, `tests/test_mask.py`, using the snippet exactly as `pypdf` returns
it. Two of its four cases failed before the fix. Masking now matches any run of whitespace
against any run of whitespace, ignores case, and masks the text it actually found, so the
line breaks survive in the mask:

```
"snippet": "Address / •• ••••••••• ••••, ••••••, / •••••••"
```

**Verified.** 104 worker tests pass, up from 100. All 17 documents were run again with a real
model, and the leak check was tightened to compare whitespace and case insensitively, which
is how it should have worked from the start: 20 personal values across 17 reports, 0 leaks.
No outcome regressed on the original eleven samples. One changed for the better:
`damage_photo.jpg` is now consistently `supporting_evidence`, where the committed report still
showed an older `UNSUPPORTED` from before temperature was fixed.

### What was deliberately left alone

The identity card came back `INCOMPLETE` because its number, `ELD-260921-74`, does not match
this platform's own convention of one letter and eight digits. That is the format rule doing
what `rules/validate.py` documents, on a document that was never told the rule. A real
deployment would carry per country formats. Loosening the rule to make one specimen pass
would have been tuning the platform to the test.

The six documents and their reports are committed under `samples/external/`.

---

## The Bedrock quota block: what is known, and what was decided

Recorded here because it decides how the platform is deployed, and because the facts are
easy to misremember.

**Facts, each one checked rather than assumed.**

- Every Amazon Bedrock Converse call on account `277707137200` fails with
  `ThrottlingException: Too many tokens per day`. Reproduced with Amazon Nova Pro in
  `ap-southeast-1`, `ap-northeast-1` and `us-west-2`, most recently on 2026-09-21.
- Service Quotas shows `0` for "Model invocation max tokens per day for Amazon Nova Pro" in
  `ap-southeast-1`, and marks that quota as not adjustable, so it cannot be raised through
  self service. Two other regions list a large default and reject calls all the same, so the
  listed value is not what is applied to the account.
- The account is not in an AWS Organization, so no organization policy is involved, and a
  valid payment method is on file.
- One call, once, returned "Your account is currently being verified". It did not recur, and
  AWS has not confirmed that verification is the cause. It is recorded as an observation and
  not as the explanation.

**AWS Support case 178990624000702**, opened 2026-09-20 on Basic Support.

| When (Malaysia time) | What happened |
|---|---|
| 2026-09-20 20:10 | Case opened, contact method Web |
| 2026-09-21 14:31 | Still `Unassigned` with no reply after about 18 hours. Follow up posted |
| 2026-09-21 14:35 | Live chat requested on the same case |
| 2026-09-21 14:39 to 14:45 | Agent confirmed the symptoms, asked whether the account has an Account Manager (it does not), and escalated to the Bedrock service team. Stated response time: usually 24 to 48 hours |

**Decision.** The deployment does not wait for it. The platform deploys with
`llm_provider = "openai"` and `enable_nat = true`, the documented fallback in
`ARCHITECTURE.md` sections 6, 8 and 15. Bedrock stays the default in Terraform and stays
fully implemented. If the quota is granted, switching back is two variable changes and no
code change, preceded by the one Nova Pro test call the README lists as a prerequisite.

