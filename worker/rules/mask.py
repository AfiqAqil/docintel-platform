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


def mask_value(value: str) -> str:
    """Mask a value while keeping its shape.

    Separators are kept, so an identity number masks to a recognisably formatted string
    rather than an undifferentiated blob, and a reviewer can still tell one document from
    another.

    The trailing characters are revealed only for values that contain a digit. That
    restriction matters: a tail is genuinely useful on a reference number, a policy number or
    a phone number, where the last four characters are how people disambiguate two records.
    On a name or an email address it is useless for that purpose and leaks part of the value,
    so those are masked whole. Deciding on the presence of a digit rather than on the field
    name keeps this a property of the value, so it cannot drift out of step with the schema.
    """
    if not value:
        return value

    chars = list(value)
    alnum_positions = [i for i, c in enumerate(chars) if c.isalnum()]
    identifier_like = any(c.isdigit() for c in value)

    if identifier_like and len(alnum_positions) >= MIN_LENGTH_FOR_TAIL:
        keep_from = len(alnum_positions) - VISIBLE_TAIL
    else:
        keep_from = len(alnum_positions)

    for idx in alnum_positions[:keep_from]:
        chars[idx] = MASK_CHAR

    return "".join(chars)


def pii_field_names(schema: type[Any] | None) -> set[str]:
    """The fields a Pydantic schema tags as sensitive.

    Reads `json_schema_extra={"pii": True}` from each model field. Returns an empty set for
    a schema that tags nothing, and for None, so an untyped or unsupported document is not a
    special case at the call site.
    """
    if schema is None or not hasattr(schema, "model_fields"):
        return set()

    names = set()
    for name, field in schema.model_fields.items():
        extra = getattr(field, "json_schema_extra", None)
        if isinstance(extra, dict) and extra.get("pii") is True:
            names.add(name)
    return names


def _redact_occurrences(text: str, secrets: list[str]) -> str:
    """Replace every occurrence of each sensitive value in free text with its mask."""
    for secret in secrets:
        if secret and secret in text:
            text = text.replace(secret, mask_value(secret))
    return text


def mask_state(state: State, schema: type[Any] | None) -> dict[str, Any]:
    """Return the state changes that mask every sensitive value.

    Returns only the keys it changes, as every node does.
    """
    extracted: dict[str, FieldValue] = state.get("extracted") or {}
    sensitive = pii_field_names(schema)

    # Collect the real values first, because they have to be scrubbed from the snippets and
    # the trace as well, not only from the fields they came from.
    secrets = [
        field["value"]
        for name, field in extracted.items()
        if name in sensitive and field.get("value")
    ]
    secrets = [s for s in secrets if s]
    # Longest first, so a value that contains a shorter one is masked whole rather than
    # being partly rewritten by the shorter match.
    secrets.sort(key=len, reverse=True)

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
                value=mask_value(value),
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

    return {"extracted": masked, "masked_trace": masked_trace, "validation_errors": errors}


# Kept for the report builder, which needs the same scrubbing applied to the free text it
# assembles from observations.
def redact(text: str, secrets: list[str]) -> str:
    return _redact_occurrences(text, secrets)


_WHITESPACE = re.compile(r"\s+")


def normalise(text: str) -> str:
    """Whitespace and case normalisation, shared with snippet verification."""
    return _WHITESPACE.sub(" ", text).strip().casefold()
