"""The deterministic hallucination check.

Every extracted field carries an evidence snippet: the span of source text the model claims
the value came from. If that span cannot be found in the source, it is a fabricated citation,
not a genuine quote, so the field is rejected outright rather than trusted with a caveat. Per
architecture section 5 ("On model self-reported confidence"), the model's own per-field
confidence is not used as a quality signal because it is not calibrated. This check is what
actually catches bad extraction, which is why fields carry snippets at all.

Matching normalises whitespace and case (via rules.mask.normalise, reused rather than
reimplemented) so a line break or a capitalisation difference inside a genuine quote is not
mistaken for a fabrication.

The check is skipped entirely for image only input (scanned PDFs, photographs): there is no
extracted text to match a snippet against. `verified` is then None on every field, not False,
and nothing is rejected on that basis alone.
"""

from __future__ import annotations

from typing import Any

from graph.state import FieldValue
from rules.mask import normalise


def verify_snippet(snippet: str | None, source_text: str) -> bool | None:
    """Check whether one snippet actually appears in the source text.

    None means there was nothing to check, an empty or missing snippet. Whether an empty
    snippet is itself a rejection is the caller's decision (verify_all), because that depends
    on whether the field has a value at all.
    """
    if not snippet:
        return None
    return normalise(snippet) in normalise(source_text)


def verify_all(
    extracted: dict[str, FieldValue], source_text: str, image_only: bool
) -> tuple[dict[str, FieldValue], list[str]]:
    """Verify every field's snippet against the source text.

    Returns the extracted dict with `verified` populated on each field, and rejected fields'
    values cleared, plus the list of validation error strings produced along the way.

    A field is rejected (value cleared, verified set to False, error recorded naming the
    field) when:
      - it has a value but no snippet at all, since the design requires evidence for every
        value, or
      - its snippet does not appear in the source text, a fabricated citation.

    A field with no value is not rejected. It has nothing to verify, and whether it should
    have had a value is a required-field-presence question, handled separately in validate.py.
    """
    if image_only:
        # No extracted text exists to check snippets against. This is not a failure, it is an
        # observation-worthy signal: verified is None (not False) on every field, and nothing
        # is rejected on this basis. That is the signal this function has to give, since its
        # return type is fixed to (dict, list[str]) and errors must never carry an
        # observation (routers.py retries on any non-empty validation_errors, and "not
        # snippet-verified" must never trigger a retry). The calling node already holds the
        # same image_only flag it passed in here, so it records the observation directly from
        # that, rather than needing to infer it back out of this return value.
        return (
            {
                name: (
                    field
                    if isinstance(field, list)
                    else FieldValue(
                        value=field.get("value"), snippet=field.get("snippet"), verified=None
                    )
                )
                for name, field in extracted.items()
            },
            [],
        )

    result: dict[str, Any] = {}
    errors: list[str] = []

    for name, field in extracted.items():
        # A nested structure, today invoice line items: a list of rows, each row a dict of
        # cells that are themselves value plus snippet. Every cell carries evidence for the
        # same reason a top level field does, so the rows are verified too rather than
        # trusted because they happen to be nested.
        if isinstance(field, list):
            rows, row_errors = _verify_rows(name, field, source_text)
            result[name] = rows
            errors.extend(row_errors)
            continue

        value = field.get("value")
        snippet = field.get("snippet")

        if value is None:
            # Nothing extracted, so nothing to verify against the source text.
            result[name] = FieldValue(value=None, snippet=snippet, verified=None)
            continue

        if not snippet:
            errors.append(f"{name}: extracted value has no evidence snippet")
            result[name] = FieldValue(value=None, snippet=snippet, verified=False)
            continue

        verified = verify_snippet(snippet, source_text)
        if not verified:
            errors.append(f"{name}: evidence snippet not found in source text")
            result[name] = FieldValue(value=None, snippet=snippet, verified=False)
        else:
            result[name] = FieldValue(value=value, snippet=snippet, verified=True)

    return result, errors


def _verify_rows(
    field_name: str, rows: list[Any], source_text: str
) -> tuple[list[Any], list[str]]:
    """Verify every cell of a nested row structure, such as invoice line items.

    A row is rejected cell by cell rather than wholesale: one fabricated description does not
    discard a line's amount, which the arithmetic check still needs.
    """
    errors: list[str] = []
    verified_rows: list[Any] = []

    for index, row in enumerate(rows):
        if not isinstance(row, dict):
            verified_rows.append(row)
            continue

        verified_row: dict[str, Any] = {}
        for cell_name, cell in row.items():
            if not isinstance(cell, dict) or "value" not in cell:
                verified_row[cell_name] = cell
                continue

            value = cell.get("value")
            snippet = cell.get("snippet")

            if value is None:
                verified_row[cell_name] = FieldValue(
                    value=None, snippet=snippet, verified=None
                )
            elif not snippet or not verify_snippet(snippet, source_text):
                errors.append(
                    f"{field_name}[{index}].{cell_name}: evidence snippet not found in "
                    "source text"
                )
                verified_row[cell_name] = FieldValue(
                    value=None, snippet=snippet, verified=False
                )
            else:
                verified_row[cell_name] = FieldValue(
                    value=value, snippet=snippet, verified=True
                )

        verified_rows.append(verified_row)

    return verified_rows, errors
