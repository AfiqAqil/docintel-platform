"""Deterministic validation. No LLM call, by design (architecture section 5): these are rules
with exact answers, and a model would make them non-deterministic and slower for no gain.

Two lists come out of this, and the distinction matters because route_by_validation
(graph/routers.py) routes on it:

  - missing_fields: a required field the document simply does not supply. Retrying the
    extractor cannot fix this, so it never triggers the retry cycle.
  - validation_errors: the model returned something wrong, a bad date, arithmetic that does
    not add up, a fabricated snippet. This is worth one retry, because the same document might
    yield a correct answer on a second attempt.

Checks, all deterministic:
  1. Snippet verification (rules/snippets.py), the hallucination check.
  2. Required field presence, per document type.
  3. Date parsing and ordering.
  4. Currency amount parsing.
  5. Invoice line item arithmetic.
  6. Identity and policy number formats.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from pydantic import BaseModel

from graph.state import DocType
from rules import snippets

# The schema module is imported defensively so that this module, which is pure rules, can
# be imported and unit tested without the LLM layer present at all.
_SCHEMA_BY_TYPE: dict[DocType, type[BaseModel]] | None
try:
    from llm.schemas import SCHEMA_BY_TYPE as _SCHEMA_BY_TYPE
except ImportError:
    _SCHEMA_BY_TYPE = None

# Fallback required-field mapping, consulted only when llm.schemas is not importable (not yet
# written, or a unit test of this module in isolation). Once llm/schemas.py exists, its
# SCHEMA_BY_TYPE and each schema's REQUIRED_FIELDS are authoritative and this is never reached.
_FALLBACK_REQUIRED_FIELDS: dict[DocType, list[str]] = {
    DocType.CLAIM_FORM: ["claimant_name", "policy_number", "incident_date", "claim_date"],
    DocType.INVOICE: ["invoice_number", "total_amount", "line_items"],
    DocType.IDENTITY_DOCUMENT: ["full_name", "identity_number", "date_of_birth"],
}


def _required_fields(doc_type: DocType) -> list[str]:
    if _SCHEMA_BY_TYPE is not None and doc_type in _SCHEMA_BY_TYPE:
        schema = _SCHEMA_BY_TYPE[doc_type]
        return list(getattr(schema, "REQUIRED_FIELDS", []))
    return _FALLBACK_REQUIRED_FIELDS.get(doc_type, [])


@dataclass(frozen=True)
class ValidationResult:
    # dict[str, Any] rather than dict[str, FieldValue], because it is not only FieldValue.
    # An invoice's line_items is a list of rows, and claiming otherwise would be a signature
    # that lies about what this function actually takes and returns.
    extracted: dict[str, Any]
    missing_fields: list[str]
    validation_errors: list[str]


# Accepted date formats. datetime.fromisoformat is tried first since it is the most common
# case; these cover the other shapes synthetic documents plausibly use. No date parsing
# dependency is added, per the architecture's own constraint.
_DATE_FORMATS = ("%Y-%m-%d", "%d/%m/%Y", "%d %B %Y")


def _parse_date(value: str) -> datetime | None:
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        pass
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(value, fmt)
        except ValueError:
            continue
    return None


# Strips everything but digits, the decimal point and a leading minus sign, so currency
# symbols ("$", "SGD"), thousands separators (",") and surrounding whitespace all fall away
# and what is left is either a valid decimal literal or garbage that Decimal() rejects.
_CURRENCY_STRIP = re.compile(r"[^\d.\-]")


def _parse_amount(value: str) -> Decimal | None:
    cleaned = _CURRENCY_STRIP.sub("", value.strip())
    if not cleaned or cleaned in ("-", "."):
        return None
    try:
        return Decimal(cleaned)
    except InvalidOperation:
        return None


# Identity and policy number formats.
#
# The sample documents are synthetic (architecture assumption 8), so there is no real-world
# standard to validate against. These formats are our own convention, chosen to be simple,
# explainable, and clearly distinguishable from arbitrary free text:
#
#   identity_number: one uppercase letter followed by 8 digits, e.g. "A12345678". This mirrors
#     the common shape of a passport or national ID number, a letter prefix plus a fixed run
#     of digits, without matching any specific country's real scheme.
#   policy_number: "POL-" followed by 6 to 10 digits, e.g. "POL-004821". A readable,
#     insurer-style prefix makes a malformed value obvious in a report rather than silently
#     accepted as a plausible-looking string.
INCIDENT_DATE_FIELD = "date_of_incident"
CLAIM_DATE_FIELD = "date_filed"

IDENTITY_NUMBER_RE = re.compile(r"^[A-Z]\d{8}$")
POLICY_NUMBER_RE = re.compile(r"^POL-\d{6,10}$")

# Tolerance for invoice line items summing to the stated total, to absorb rounding on the
# individual line amounts. Decimal, not float, so this comparison is exact rather than subject
# to binary floating point representation error.
_ROUNDING_TOLERANCE = Decimal("0.01")



def _flat_value(extracted: dict[str, Any], name: str) -> str | None:
    """The value of a plain field, or None if it is absent or is a nested structure."""
    field = extracted.get(name)
    return field.get("value") if isinstance(field, dict) else None


def _is_present(extracted: dict[str, Any], name: str) -> bool:
    """Whether a required field was actually supplied.

    A nested structure such as line items counts as present when it has at least one row; a
    plain field counts when it has a value. An empty list is as missing as a null string.
    """
    field = extracted.get(name)
    if isinstance(field, list):
        return len(field) > 0
    return isinstance(field, dict) and field.get("value") is not None


def validate(
    doc_type: DocType, extracted: dict[str, Any], source_text: str, image_only: bool
) -> ValidationResult:
    # Snippet verification runs first. A field it rejects has its value cleared, which the
    # required-field check below then naturally reports as missing (if the field is required)
    # on top of the validation error snippets.verify_all already recorded for the fabrication.
    verified, errors = snippets.verify_all(extracted, source_text, image_only)
    errors = list(errors)

    # Date parsing and ordering. Any field named exactly "date", or ending or starting with
    # "date" on a word boundary ("_date", "date_"), is a candidate. A plain "date" substring
    # would also catch something like "last_updated", which is not a date field.
    parsed_dates: dict[str, datetime] = {}
    for name, f in verified.items():
        if not isinstance(f, dict):
            continue
        lowered = name.lower()
        if not (lowered == "date" or lowered.endswith("_date") or lowered.startswith("date_")):
            continue
        value = f.get("value")
        if value is None:
            continue
        parsed = _parse_date(value)
        if parsed is None:
            errors.append(f"{name}: '{value}' is not a recognised date")
        else:
            parsed_dates[name] = parsed

    # The one cross-field rule the architecture names explicitly: an incident date cannot be
    # after the claim date.
    # The field names are the ones ClaimFormExtraction actually defines. Getting these
    # wrong does not fail loudly, it just means the rule never fires, so they are named
    # once here rather than spelled inline.
    if (
        INCIDENT_DATE_FIELD in parsed_dates
        and CLAIM_DATE_FIELD in parsed_dates
        and parsed_dates[INCIDENT_DATE_FIELD] > parsed_dates[CLAIM_DATE_FIELD]
    ):
        errors.append(f"{INCIDENT_DATE_FIELD} is after {CLAIM_DATE_FIELD}")

    # Currency amounts. Any field whose name mentions "amount" or "total" is a candidate.
    for name, f in verified.items():
        if not isinstance(f, dict):
            continue
        if "amount" not in name.lower() and "total" not in name.lower():
            continue
        value = f.get("value")
        if value is None:
            continue
        if _parse_amount(value) is None:
            errors.append(f"{name}: '{value}' is not a valid monetary amount")

    # Invoice arithmetic: the line items must sum to the stated total.
    #
    # line_items is a list of rows, each row a dict of cells, matching InvoiceExtraction in
    # llm/schemas.py. Money is Decimal throughout, never float: 0.1 + 0.2 is not 0.3 in
    # binary floating point, and an invoice check that is wrong by a cent is worse than no
    # check at all.
    if doc_type == DocType.INVOICE:
        rows = verified.get("line_items")
        total_value = None
        for total_name in ("total_amount", "total"):
            candidate = verified.get(total_name)
            if isinstance(candidate, dict) and candidate.get("value") is not None:
                total_value = candidate["value"]
                break

        if isinstance(rows, list) and rows and total_value is not None:
            amounts: list[Decimal] = []
            unparsed: list[str] = []

            for index, row in enumerate(rows):
                if not isinstance(row, dict):
                    continue
                cell = row.get("amount")
                raw = cell.get("value") if isinstance(cell, dict) else None
                if raw is None:
                    # Either the row genuinely had no amount, or snippet verification
                    # rejected it. Both mean the sum cannot be trusted, so say so rather
                    # than quietly summing the rows that survived.
                    unparsed.append(f"row {index} has no usable amount")
                    continue
                amount = _parse_amount(raw)
                if amount is None:
                    unparsed.append(f"row {index}: '{raw}' is not a valid monetary amount")
                else:
                    amounts.append(amount)

            total = _parse_amount(total_value)

            if unparsed:
                for problem in unparsed:
                    errors.append(f"line_items: {problem}")
            elif total is not None:
                # A None total was already reported by the currency check above, since
                # total_amount matches on containing "total".
                line_sum = sum(amounts, Decimal("0"))
                if abs(line_sum - total) > _ROUNDING_TOLERANCE:
                    errors.append(
                        f"line_items sum to {line_sum} but total_amount is {total}"
                    )

    # Identity and policy number formats.
    identity_value = _flat_value(verified, "identity_number")
    if identity_value is not None and not IDENTITY_NUMBER_RE.match(identity_value):
        errors.append(f"identity_number: '{identity_value}' does not match the expected format")

    policy_value = _flat_value(verified, "policy_number")
    if policy_value is not None and not POLICY_NUMBER_RE.match(policy_value):
        errors.append(f"policy_number: '{policy_value}' does not match the expected format")

    # Required field presence, checked last so it sees the value as snippet verification left
    # it: a field rejected as a fabricated citation has already had its value cleared above,
    # so it lands here too if it was required. Retrying cannot fix an absent field, so this
    # goes to missing_fields, never validation_errors.
    missing = [name for name in _required_fields(doc_type) if not _is_present(verified, name)]

    return ValidationResult(extracted=verified, missing_fields=missing, validation_errors=errors)
