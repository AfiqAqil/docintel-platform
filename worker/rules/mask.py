"""PII masking. Deterministic, and the last thing that sees real values.

Ordering matters and is the reason this is its own node rather than something the report
builder does. Validation checks ID number formats and cross field rules, which have to run
against real values. The report and the summary prompt must never contain them. So masking
runs once, after the final validation pass, and everything downstream sees masked state only.

What is masked is not a guess. Each per type Pydantic schema tags its sensitive fields with
`pii=True` in the field metadata, so this module asks the schema rather than pattern matching
on field names. Adding a sensitive field to a schema therefore masks it automatically.

Three places carry a sensitive value and all three are masked:
  1. the field value itself,
  2. the evidence snippet attached to that field, which quotes the source text and so
     usually contains the value verbatim,
  3. any occurrence of the value in the trace, written to `masked_trace` rather than back
     over `trace`, because `trace` has an append only reducer and cannot be overwritten.
     `trace` is never persisted or logged; the report carries `masked_trace`.

Raw text, page images and unmasked values are never written to logs or persisted.
"""

from __future__ import annotations

import re
from typing import Any

from graph.state import FieldValue, State, StepRecord

MASK_CHAR = "•"
# Keep this many trailing characters visible, so a reviewer can still tell two documents
# apart without the full value being recoverable.
VISIBLE_TAIL = 4
# Below this length nothing is revealed: showing 4 of 5 characters is not masking.
MIN_LENGTH_FOR_TAIL = 7
# The json_schema_extra pii value that opts a field into keeping a visible tail.
TAIL_MODE = "tail"


def mask_value(value: str, reveal_tail: bool = False) -> str:
    """Mask a value, optionally keeping its trailing characters visible.

    Separators are always kept, so a masked value still looks like the kind of thing it is
    rather than an undifferentiated blob.

    `reveal_tail` is off by default and is decided by the schema, not by inspecting the
    value. An earlier version guessed from the value itself, revealing a tail whenever it
    contained a digit, which meant an address like "12 Main Street" kept its last four
    characters. Any content based guess has that failure mode, because "contains a digit" is
    a property shared by identifiers and by ordinary text that merely has a number in it.
    Only a field the schema explicitly marks as an identifier gets a tail.
    """
    if not value:
        return value

    chars = list(value)
    alnum_positions = [i for i, c in enumerate(chars) if c.isalnum()]

    if reveal_tail and len(alnum_positions) >= MIN_LENGTH_FOR_TAIL:
        keep_from = len(alnum_positions) - VISIBLE_TAIL
    else:
        keep_from = len(alnum_positions)

    for idx in alnum_positions[:keep_from]:
        chars[idx] = MASK_CHAR

    return "".join(chars)


def pii_field_names(schema: type[Any] | None) -> set[str]:
    """The fields a Pydantic schema tags as sensitive.

    Reads `json_schema_extra={"pii": ...}` off each model field. Any truthy value means
    sensitive. Returns an empty set for a schema that tags nothing and for None, so an
    unsupported or unclassified document is not a special case at the call site.
    """
    return set(_pii_modes(schema))


def pii_tail_fields(schema: type[Any] | None) -> set[str]:
    """The sensitive fields that may keep a visible tail.

    A field opts in with `{"pii": "tail"}` rather than `{"pii": True}`. The tail exists so a
    reviewer can tell two policy numbers or two phone numbers apart, which is only useful for
    identifiers. It is opt in rather than opt out so that a newly added sensitive field is
    fully masked by default: forgetting to add a tag should never be what leaks a value.
    """
    return {name for name, mode in _pii_modes(schema).items() if mode == TAIL_MODE}


def _pii_modes(schema: type[Any] | None) -> dict[str, Any]:
    if schema is None or not hasattr(schema, "model_fields"):
        return {}

    modes: dict[str, Any] = {}
    for name, field in schema.model_fields.items():
        extra = getattr(field, "json_schema_extra", None)
        if isinstance(extra, dict) and extra.get("pii"):
            modes[name] = extra["pii"]
    return modes


def _redact_occurrences(text: str, secrets: list[tuple[str, bool]]) -> str:
    """Replace every occurrence of each sensitive value in free text with its mask.

    Each secret carries its own tail decision, so a value is masked the same way wherever it
    appears: in its own field, quoted inside a snippet, or mentioned in a trace detail.
    """
    for secret, reveal_tail in secrets:
        if secret and secret in text:
            text = text.replace(secret, mask_value(secret, reveal_tail=reveal_tail))
    return text


def mask_state(state: State, schema: type[Any] | None) -> dict[str, Any]:
    """Return the state changes that mask every sensitive value.

    Returns only the keys it changes, as every node does.
    """
    extracted: dict[str, FieldValue] = state.get("extracted") or {}
    sensitive = pii_field_names(schema)
    tail_allowed = pii_tail_fields(schema)

    # Collect the real values first, because they have to be scrubbed from the snippets and
    # the trace as well, not only from the fields they came from.
    secrets = [
        (field["value"], name in tail_allowed)
        for name, field in extracted.items()
        if name in sensitive and isinstance(field, dict) and field.get("value")
    ]
    # Longest first, so a value that contains a shorter one is masked whole rather than
    # being partly rewritten by the shorter match.
    secrets.sort(key=lambda pair: len(pair[0]), reverse=True)

    masked: dict[str, Any] = {}
    for name, field in extracted.items():
        # Nested structures, today invoice line items, are a list rather than a FieldValue.
        # Nothing in them is tagged sensitive, because the tags live on the top level schema
        # fields, so they pass through untouched.
        if not isinstance(field, dict) or "value" not in field:
            masked[name] = field
            continue

        value = field.get("value")
        snippet = field.get("snippet")

        if name in sensitive and value:
            masked[name] = FieldValue(
                value=mask_value(value, reveal_tail=name in tail_allowed),
                # The snippet quotes the source, so it usually contains the value verbatim.
                snippet=_redact_occurrences(snippet, secrets) if snippet else snippet,
                verified=field.get("verified"),
            )
        else:
            # Not a sensitive field, but its snippet may still quote a sensitive value from
            # a neighbouring line, so the snippet is scrubbed either way.
            masked[name] = FieldValue(
                value=value,
                snippet=_redact_occurrences(snippet, secrets) if snippet else snippet,
                verified=field.get("verified"),
            )

    trace: list[StepRecord] = state.get("trace") or []
    masked_trace = [
        StepRecord(
            node=step["node"],
            status=step["status"],
            duration_ms=step["duration_ms"],
            detail=_redact_occurrences(step["detail"], secrets) if step.get("detail") else None,
        )
        for step in trace
    ]

    errors = [_redact_occurrences(e, secrets) for e in (state.get("validation_errors") or [])]

    # The classifier's rationale is free text the model wrote while looking at the
    # unmasked document, and it is persisted in the report. Without this it is a way around
    # every other guard here: a note reading "claim form for Jordan Avery, policy POL-4471920"
    # would carry both values straight through. Scrubbing it is the second of two defences;
    # the first is that the classification prompt forbids quoting identifiers at all.
    notes = state.get("classification_notes")

    return {
        "extracted": masked,
        "masked_trace": masked_trace,
        "validation_errors": errors,
        "classification_notes": _redact_occurrences(notes, secrets) if notes else notes,
    }


# Kept for the report builder, which needs the same scrubbing applied to the free text it
# assembles from observations.
def redact(text: str, secrets: list[tuple[str, bool]]) -> str:
    return _redact_occurrences(text, secrets)


_WHITESPACE = re.compile(r"\s+")


def normalise(text: str) -> str:
    """Whitespace and case normalisation, shared with snippet verification."""
    return _WHITESPACE.sub(" ", text).strip().casefold()
