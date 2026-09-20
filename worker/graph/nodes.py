"""The graph's nodes.

Every node follows the same shape: it returns only the keys it changed, and it appends one
StepRecord to the trace. The `@node` decorator handles the timing, the trace entry and the
error classification, so each node body stays about its own job.

Error handling is the part worth reading carefully. Two kinds of failure are treated
completely differently:

- A TERMINAL error means this document cannot be processed however often we try. An
  encrypted PDF stays encrypted. The node records it in state, the routers send the run to
  `record_failure`, and the graph finishes normally so the consumer can write FAILED and ack
  the message.

- A TRANSIENT error means the attempt failed but the work is still valid: a throttled model
  call, a timeout. The decorator re-raises it so the exception leaves the graph entirely.
  The consumer then does not ack, SQS redelivers, and the document is *not* marked failed,
  because it is not finished. Retrying is the queue's job, not the graph's, which is why
  there is no transient error edge in the diagram.
"""

from __future__ import annotations

import functools
import time
from collections.abc import Callable
from typing import Any, cast

from pydantic import BaseModel

from config import CONFIG
from graph.routers import outcome_for
from graph.state import (
    DocType,
    ErrorKind,
    FieldValue,
    GraphError,
    Outcome,
    State,
    StepRecord,
)
from llm import prompts
from llm.provider import get_chat_model
from llm.schemas import SCHEMA_BY_TYPE, Classification
from rules import mask
from rules import validate as validate_rules
from rules.parse import UnreadableDocument
from rules.parse import load_document as parse_document


class TerminalError(Exception):
    """Raised by a node when the document itself is the problem."""


def _is_transient(exc: BaseException) -> bool:
    """Classify an unexpected exception.

    Deliberately conservative: anything not recognised as the document's fault is treated as
    transient. Getting this wrong in the transient direction costs a redelivery and a few
    model calls. Getting it wrong in the terminal direction marks a document permanently
    failed because of a blip, and there is no automatic way back from that.
    """
    if isinstance(exc, TerminalError | UnreadableDocument):
        return False
    name = type(exc).__name__
    return any(
        marker in name
        for marker in ("Throttl", "Timeout", "Connection", "ServiceUnavailable", "RateLimit")
    ) or not isinstance(exc, ValueError | TypeError | KeyError)


NodeFn = Callable[[State], dict[str, Any]]


def node(name: str) -> Callable[[NodeFn], NodeFn]:
    """Wrap a node body with timing, tracing and error classification."""

    def decorate(fn: NodeFn) -> NodeFn:
        @functools.wraps(fn)
        def wrapper(state: State) -> dict[str, Any]:
            started = time.monotonic()
            try:
                result = fn(state)
            except Exception as exc:
                if _is_transient(exc):
                    # Leave the graph. SQS redelivers; nothing is marked failed.
                    raise
                elapsed = int((time.monotonic() - started) * 1000)
                return {
                    "error": GraphError(node=name, kind=ErrorKind.TERMINAL, message=str(exc)),
                    "trace": [
                        StepRecord(
                            node=name, status="error", duration_ms=elapsed, detail=str(exc)
                        )
                    ],
                }
            elapsed = int((time.monotonic() - started) * 1000)
            result.setdefault("trace", [])
            result["trace"] = [
                *result["trace"],
                StepRecord(node=name, status="ok", duration_ms=elapsed, detail=None),
            ]
            return result

        return wrapper

    return decorate


# --------------------------------------------------------------------------------------
# load_document: deterministic
# --------------------------------------------------------------------------------------


@node("load_document")
def load_document(state: State) -> dict[str, Any]:
    """Turn the original bytes into text, or into page images when there is no text layer.

    The bytes are already in state; the consumer fetched them from S3. This node makes no
    network call, which is what lets the whole graph run against a local file in tests.
    """
    parsed = parse_document(
        state["raw_bytes"],
        state.get("content_type", ""),
        state.get("filename", ""),
    )
    return {
        "text": parsed.text,
        "page_images": parsed.page_images,
        "image_only": parsed.image_only,
        "observations": list(parsed.observations),
    }


# --------------------------------------------------------------------------------------
# classify: LLM
# --------------------------------------------------------------------------------------


@node("classify")
def classify(state: State) -> dict[str, Any]:
    """One structured model call returning a type, a confidence and a short rationale.

    The confidence is used for one thing only: the routing threshold in `route_by_type`. It
    is not treated as a calibrated probability, and it is not used as a quality signal on the
    extraction, because a model's self reported confidence cannot be defended as one.
    Snippet verification is the check that actually catches bad extraction.
    """
    model = get_chat_model().with_structured_output(Classification)
    messages = prompts.classification_messages(
        state.get("text") or None,
        state.get("page_images") if state.get("image_only") else None,
    )
    # with_structured_output is typed as returning a dict or a model, because a caller can
    # ask for either. We always ask for the model, so this narrows what the type cannot.
    result = cast(Classification, model.invoke(messages))

    return {
        "doc_type": result.doc_type,
        "confidence": result.confidence,
        "classification_notes": result.notes,
    }


# --------------------------------------------------------------------------------------
# extraction: LLM, one node per path
# --------------------------------------------------------------------------------------


def _extract(state: State, doc_type: DocType) -> dict[str, Any]:
    """Shared extraction body.

    On the retry pass, `validation_errors` from the previous attempt are appended to the
    prompt so the model is told exactly what was wrong, rather than being asked again blind.
    """
    schema = SCHEMA_BY_TYPE[doc_type]
    attempt = state.get("extraction_attempts", 0)
    previous_errors = state.get("validation_errors") if attempt > 0 else None

    model = get_chat_model().with_structured_output(schema)
    messages = prompts.extraction_messages(
        doc_type,
        state.get("text") or None,
        state.get("page_images") if state.get("image_only") else None,
        previous_errors,
    )
    result = cast(BaseModel, model.invoke(messages))

    extracted: dict[str, Any] = {}
    for field_name in type(result).model_fields:
        field = getattr(result, field_name, None)
        if field is None:
            continue
        if isinstance(field, list):
            # A nested structure, which today means invoice line items. It is carried
            # through rather than flattened into a FieldValue, because the arithmetic rule
            # needs the rows intact to sum them. Dumped to plain dicts here so that nothing
            # downstream, including the persisted report, has to know about Pydantic.
            extracted[field_name] = [
                item.model_dump() if isinstance(item, BaseModel) else item for item in field
            ]
            continue

        value = getattr(field, "value", None)
        snippet = getattr(field, "snippet", None)
        extracted[field_name] = FieldValue(value=value, snippet=snippet, verified=None)

    observations = (
        ["Extraction was retried once with the previous validation errors appended."]
        if previous_errors
        else []
    )

    return {
        "extracted": extracted,
        "extraction_attempts": attempt + 1,
        # Cleared so the retry's own validation starts from a clean slate rather than
        # inheriting the errors that triggered it.
        "validation_errors": [],
        "observations": observations,
    }


@node("extract_claim")
def extract_claim(state: State) -> dict[str, Any]:
    return _extract(state, DocType.CLAIM_FORM)


@node("extract_invoice")
def extract_invoice(state: State) -> dict[str, Any]:
    return _extract(state, DocType.INVOICE)


@node("extract_identity")
def extract_identity(state: State) -> dict[str, Any]:
    return _extract(state, DocType.IDENTITY_DOCUMENT)


@node("extract_generic")
def extract_generic(state: State) -> dict[str, Any]:
    """Policy documents, correspondence and supporting evidence.

    One node rather than three, because these three types want the same fields. They stay
    distinct in the classification enum so the report names the right thing and so a future
    type specific extractor is a routing change rather than a redesign.
    """
    return _extract(state, state.get("doc_type", DocType.SUPPORTING_EVIDENCE))


# --------------------------------------------------------------------------------------
# mark_unsupported: deterministic
# --------------------------------------------------------------------------------------


@node("mark_unsupported")
def mark_unsupported(state: State) -> dict[str, Any]:
    """An out of scope type, or a classification the model was not confident enough about.

    This is not a failure. The document processed fine and the answer is that a human should
    look at it, so it still gets a report saying so.
    """
    confidence = state.get("confidence", 0.0)
    doc_type = state.get("doc_type", DocType.UNKNOWN)

    if doc_type == DocType.UNKNOWN:
        reason = "The document type could not be determined."
    else:
        reason = (
            f"Classified as {doc_type} with confidence {confidence:.2f}, below the "
            f"{CONFIG.classify_confidence_threshold:.2f} threshold required to extract."
        )

    return {
        "outcome": Outcome.UNSUPPORTED,
        "extracted": {},
        "missing_fields": [],
        "validation_errors": [],
        "observations": [f"{reason} Routed for manual review."],
    }


# --------------------------------------------------------------------------------------
# validate: deterministic
# --------------------------------------------------------------------------------------


@node("validate")
def validate(state: State) -> dict[str, Any]:
    """Required fields, formats, cross field rules, arithmetic, and snippet verification.

    Deliberately not a model call. These are rules with exact answers, and a model would
    make them non deterministic and slower for no gain.
    """
    result = validate_rules.validate(
        state.get("doc_type", DocType.UNKNOWN),
        state.get("extracted") or {},
        state.get("text") or "",
        bool(state.get("image_only")),
    )

    observations: list[str] = []
    if state.get("image_only"):
        observations.append(
            "Input was image only, so evidence snippets could not be verified against "
            "source text. Extraction is unverified."
        )

    return {
        "extracted": result.extracted,
        "missing_fields": result.missing_fields,
        "validation_errors": result.validation_errors,
        "observations": observations,
    }


# --------------------------------------------------------------------------------------
# mask_pii: deterministic
# --------------------------------------------------------------------------------------


@node("mask_pii")
def mask_pii(state: State) -> dict[str, Any]:
    """Mask every sensitive value, once, after the last validation pass.

    Ordering is the whole point of this node existing separately. Validation needs real
    values to check ID formats and cross field rules. The report and its summary must never
    contain them. So this runs between the two, and everything downstream sees masked state.
    """
    schema = SCHEMA_BY_TYPE.get(state.get("doc_type", DocType.UNKNOWN))
    return mask.mask_state(state, schema)


# --------------------------------------------------------------------------------------
# generate_report: LLM summary plus deterministic assembly
# --------------------------------------------------------------------------------------


@node("generate_report")
def generate_report(state: State) -> dict[str, Any]:
    """Assemble the report.

    Only the one paragraph summary comes from the model, and it is written from masked
    state. Everything else is carried through from state, so the report cannot disagree with
    what validation actually found.
    """
    outcome = outcome_for(state)
    extracted = state.get("extracted") or {}

    summary_input = {
        "document_type": state.get("doc_type", DocType.UNKNOWN),
        "outcome": outcome,
        "extracted": {
            name: field.get("value") if isinstance(field, dict) else field
            for name, field in extracted.items()
        },
        "missing_information": state.get("missing_fields") or [],
        "observations": state.get("observations") or [],
    }

    model = get_chat_model()
    summary = str(model.invoke(prompts.summary_messages(summary_input)).content).strip()

    report = {
        "document_id": state.get("document_id"),
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "document_type": str(state.get("doc_type", DocType.UNKNOWN)),
        "classification": {
            "confidence": state.get("confidence"),
            "notes": state.get("classification_notes"),
        },
        "outcome": str(outcome),
        "summary": summary,
        "extracted": extracted,
        "missing_information": state.get("missing_fields") or [],
        "validation_errors": state.get("validation_errors") or [],
        # Deduplicated while preserving order: validate runs twice on the retry path, so
        # a note it adds would otherwise appear twice for no reason.
        "observations": list(dict.fromkeys(state.get("observations") or [])),
        # masked_trace, not trace: trace has an append only reducer and still holds
        # unmasked detail strings. Only the masked copy is ever persisted.
        "trace": _full_trace(state),
    }

    return {"report": report, "outcome": outcome}


# --------------------------------------------------------------------------------------
# record_failure: deterministic
# --------------------------------------------------------------------------------------



def _full_trace(state: State) -> list[StepRecord]:
    """The trace as the report should carry it: masked, and complete.

    mask_pii writes `masked_trace` while it runs, so that copy necessarily stops before
    mask_pii and generate_report have finished and cannot record themselves. Reporting it
    alone leaves the last two nodes missing from the workflow status tracking, which is
    exactly the part a reader checks to see that processing ran to the end.

    The tail is taken from the live trace with its detail dropped rather than scrubbed. The
    nodes it covers are deterministic and write no detail except on error, so there is
    nothing to lose, and dropping is the safe direction: this runs after masking, so there
    is no secrets list here to scrub with and a detail that did contain a value would go
    straight into the report.

    The report node itself is necessarily absent: its own trace entry is written once it
    returns, so no record it builds can contain its own completion. The trace therefore ends
    at the last node before the report was assembled, which for a successful run is mask_pii.
    The consumer records the document reaching COMPLETED separately, which is where the end
    of the run is actually observable.
    """
    masked = list(state.get("masked_trace") or [])
    tail = list(state.get("trace") or [])[len(masked):]
    return masked + [
        StepRecord(
            node=step["node"],
            status=step["status"],
            duration_ms=step["duration_ms"],
            detail=None,
        )
        for step in tail
    ]


@node("record_failure")
def record_failure(state: State) -> dict[str, Any]:
    """Terminal failure. Produce a minimal report so the user sees why, not just that.

    Only reached for terminal errors. Transient ones never get here; they leave the graph so
    SQS redelivers, and the document keeps its previous status rather than being marked
    failed while it is still in flight.
    """
    error = state.get("error") or GraphError(
        node="unknown", kind=ErrorKind.TERMINAL, message="unspecified failure"
    )

    return {
        "report": {
            "document_id": state.get("document_id"),
            "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "document_type": str(state.get("doc_type", DocType.UNKNOWN)),
            "outcome": "FAILED",
            "summary": f"Processing failed in {error['node']}: {error['message']}",
            "extracted": {},
            "missing_information": [],
            "validation_errors": [],
            "observations": list(state.get("observations") or []),
            "trace": _full_trace(state),
            "error": dict(error),
        }
    }
