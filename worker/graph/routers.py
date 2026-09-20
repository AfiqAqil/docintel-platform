"""The two conditional routers, and the shared error check.

These are pure functions of state. Keeping them out of the nodes means the routing rules can
be tested directly, and read without tracing through node bodies.
"""

from __future__ import annotations

from config import CONFIG
from graph.state import DocType, Outcome, State

# Which extraction node handles which document type. Types absent from this mapping take the
# generic extractor; `unknown` never reaches here, because route_by_type stops it first.
EXTRACTOR_BY_TYPE: dict[DocType, str] = {
    DocType.CLAIM_FORM: "extract_claim",
    DocType.INVOICE: "extract_invoice",
    DocType.IDENTITY_DOCUMENT: "extract_identity",
}
GENERIC_EXTRACTOR = "extract_generic"


def has_terminal_error(state: State) -> bool:
    """True when a node recorded an error the graph should stop on.

    Transient errors never reach here: the node re-raises them so the exception leaves the
    graph and SQS redelivers the message. Only terminal errors become state.
    """
    return state.get("error") is not None


def route_by_type(state: State) -> str:
    """Send the document down the extraction path for its type.

    Low confidence is treated exactly like an unknown type. Guessing at a type the model is
    unsure of would produce a confident extraction against the wrong schema, which is worse
    than declining, because nothing downstream would flag it.
    """
    if has_terminal_error(state):
        return "record_failure"

    doc_type = state.get("doc_type", DocType.UNKNOWN)
    confidence = state.get("confidence", 0.0)

    if doc_type == DocType.UNKNOWN or confidence < CONFIG.classify_confidence_threshold:
        return "mark_unsupported"

    return EXTRACTOR_BY_TYPE.get(doc_type, GENERIC_EXTRACTOR)


def route_by_validation(state: State) -> str:
    """Decide what to do with the validation result.

    Three outcomes:

    - Something was invalid and the retry budget is not spent: go back to the same extraction
      node with the validation errors appended to the prompt. Back to the *same* node, not to
      a shared retry node, so the second attempt follows the identical path including any type
      specific handling.
    - Something was invalid and the budget is spent, or fields are simply missing from the
      document: carry on to masking and report it. A field that is genuinely absent from the
      document will never appear however many times we ask.
    - Everything checked out: carry on to masking.

    Note the asymmetry. `validation_errors` means the model returned something wrong, which is
    worth one more attempt. `missing_fields` means the document does not contain the
    information, which retrying cannot fix.
    """
    if has_terminal_error(state):
        return "record_failure"

    invalid = bool(state.get("validation_errors"))
    attempts = state.get("extraction_attempts", 0)

    if invalid and attempts < CONFIG.max_extraction_attempts:
        return EXTRACTOR_BY_TYPE.get(state.get("doc_type", DocType.UNKNOWN), GENERIC_EXTRACTOR)

    return "mask_pii"


def outcome_for(state: State) -> Outcome:
    """The outcome a completed document should carry.

    Anything unresolved at this point, whether missing or invalid, is INCOMPLETE: the
    document processed fine, but the information is not all there. That is a business result
    to review, not a technical failure, which is why it is not FAILED.
    """
    if state.get("outcome") == Outcome.UNSUPPORTED:
        return Outcome.UNSUPPORTED
    if state.get("missing_fields") or state.get("validation_errors"):
        return Outcome.INCOMPLETE
    return Outcome.COMPLETE
