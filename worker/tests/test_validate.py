"""Tests for the deterministic validation rules. No network, no database.

Each test targets one rule in isolation as far as possible, so a failure points at the rule
that broke rather than at "validation is wrong somewhere".
"""

from __future__ import annotations

from decimal import Decimal

from graph.state import DocType, FieldValue
from rules.validate import _parse_amount, validate


def fv(value: str | None, snippet: str | None) -> FieldValue:
    return FieldValue(value=value, snippet=snippet, verified=None)


def row(amount: str, description: str = "Service", snippet: str | None = None) -> dict:
    """One invoice line item, in the shape InvoiceExtraction.line_items actually produces.

    A list of rows, each row a dict of cells, each cell a value plus its snippet. The
    arithmetic rule sums the `amount` cell of each row.
    """
    return {
        "description": fv(description, snippet),
        "quantity": fv("1", snippet),
        "unit_price": fv(amount, snippet),
        "amount": fv(amount, snippet),
    }


# The claim fields ClaimFormExtraction marks required, so a test that is not about missing
# fields does not accidentally also test missing fields.
def complete_claim() -> dict:
    return {
        "policy_number": fv("POL-004821", "Policy Number: POL-004821"),
        "claim_reference": fv("CLM-2026-0831", "Claim Reference: CLM-2026-0831"),
        "claimant_name": fv("Jane Tan", "Claimant: Jane Tan"),
        "date_of_incident": fv("2026-01-05", "Date of Incident: 2026-01-05"),
        "incident_description": fv("A burst pipe.", "Incident: A burst pipe."),
        "claimed_amount": fv("1,450.00", "Amount Claimed: 1,450.00"),
    }


CLAIM_SOURCE = (
    "Claimant: Jane Tan\n"
    "Policy Number: POL-004821\n"
    "Claim Reference: CLM-2026-0831\n"
    "Date of Incident: 2026-01-05\n"
    "Date of Claim: 2026-01-10\n"
    "Incident: A burst pipe.\n"
    "Amount Claimed: 1,450.00\n"
)


def test_clean_claim_form_has_no_missing_fields_or_errors() -> None:
    # Protects the happy path: a well-formed claim form with every required field present
    # and every snippet genuinely in the source passes through with nothing to report.
    extracted = complete_claim()
    extracted["date_filed"] = fv("2026-01-10", "Date of Claim: 2026-01-10")

    result = validate(DocType.CLAIM_FORM, extracted, CLAIM_SOURCE, image_only=False)

    assert result.missing_fields == []
    assert result.validation_errors == []
    assert result.extracted["claimant_name"]["verified"] is True


def test_fabricated_snippet_is_rejected() -> None:
    # Protects the deterministic hallucination check: a snippet that does not appear anywhere
    # in the source text is a fabricated citation, so the field is cleared and reported.
    source_text = (
        "Policy Number: POL-004821\nDate of Incident: 2026-01-05\nDate of Claim: 2026-01-10\n"
    )
    extracted = {
        "claimant_name": fv("Jane Tan", "This text never appears in the document"),
        "policy_number": fv("POL-004821", "Policy Number: POL-004821"),
        "date_of_incident": fv("2026-01-05", "Date of Incident: 2026-01-05"),
        "date_filed": fv("2026-01-10", "Date of Claim: 2026-01-10"),
    }

    result = validate(DocType.CLAIM_FORM, extracted, source_text, image_only=False)

    assert result.extracted["claimant_name"]["value"] is None
    assert result.extracted["claimant_name"]["verified"] is False
    assert any("claimant_name" in e for e in result.validation_errors)
    # Clearing the value also leaves the required field absent, so it must show up as missing
    # too, on top of the validation error for the fabrication itself.
    assert "claimant_name" in result.missing_fields


def test_genuine_quote_with_different_whitespace_and_case_still_verifies() -> None:
    # Protects the normalisation: a real quote should not be flagged just because a line
    # break or a capitalisation difference separates it from the source.
    source_text = "Policy Number: POL-004821\n"
    extracted = {
        "policy_number": fv("POL-004821", "policy   number:\nPOL-004821"),
    }

    result = validate(DocType.INVOICE, extracted, source_text, image_only=False)

    assert result.extracted["policy_number"]["verified"] is True
    assert result.extracted["policy_number"]["value"] == "POL-004821"
    assert result.validation_errors == []


def test_image_only_input_skips_verification() -> None:
    # Protects the image-only path: with no extracted text to check against, verified must be
    # None (not False) on every field, and nothing gets rejected on that basis.
    extracted = {
        "claimant_name": fv("Jane Tan", "a snippet that would not match anything"),
        "policy_number": fv("POL-004821", None),
    }

    result = validate(DocType.CLAIM_FORM, extracted, "", image_only=True)

    assert result.extracted["claimant_name"]["verified"] is None
    assert result.extracted["policy_number"]["verified"] is None
    assert result.extracted["claimant_name"]["value"] == "Jane Tan"
    assert not any("evidence snippet" in e or "fabricat" in e for e in result.validation_errors)


def test_missing_required_field_is_missing_not_a_validation_error() -> None:
    # Protects the missing vs invalid distinction that route_by_validation relies on: an
    # absent field is not the model's fault and must not trigger a retry.
    extracted = complete_claim()
    extracted["claim_reference"] = fv(None, None)

    result = validate(DocType.CLAIM_FORM, extracted, CLAIM_SOURCE, image_only=False)

    assert "claim_reference" in result.missing_fields
    assert not any("claim_reference" in e for e in result.validation_errors)


def test_incident_date_after_claim_date_is_a_validation_error() -> None:
    # Protects the one cross-field rule the architecture names explicitly.
    extracted = {
        "date_of_incident": fv("2026-02-01", None),
        "date_filed": fv("2026-01-10", None),
    }

    result = validate(DocType.CLAIM_FORM, extracted, "", image_only=True)

    assert any("date_of_incident is after date_filed" in e for e in result.validation_errors)


def test_invoice_line_items_matching_total_is_clean() -> None:
    # Protects the arithmetic happy path. One amount carries a thousands separator comma, so
    # this also proves the currency parser strips separators rather than choking on them.
    extracted = {
        "invoice_number": fv("INV-1001", None),
        "invoice_date": fv("2026-01-10", None),
        "customer_name": fv("Jane Tan", None),
        "line_items": [row("1,100.00"), row("50.00")],
        "total_amount": fv("1,150.00", None),
    }

    result = validate(DocType.INVOICE, extracted, "", image_only=True)

    assert result.validation_errors == []
    assert result.missing_fields == []


def test_invoice_line_items_not_matching_total_is_a_validation_error() -> None:
    # Protects the arithmetic check catching a real mismatch.
    extracted = {
        "invoice_number": fv("INV-1001", None),
        "invoice_date": fv("2026-01-10", None),
        "customer_name": fv("Jane Tan", None),
        "line_items": [row("100.00"), row("50.00")],
        "total_amount": fv("999.00", None),
    }

    result = validate(DocType.INVOICE, extracted, "", image_only=True)

    assert any("line_items sum" in e for e in result.validation_errors)


def test_invoice_line_item_garbage_is_a_validation_error() -> None:
    # Protects against a silent pass: an unparseable amount inside a row must not be dropped
    # on the floor just because it is nested rather than a top level field.
    extracted = {
        "invoice_number": fv("INV-1001", None),
        "invoice_date": fv("2026-01-10", None),
        "customer_name": fv("Jane Tan", None),
        "line_items": [row("100.00"), row("not-a-number")],
        "total_amount": fv("150.00", None),
    }

    result = validate(DocType.INVOICE, extracted, "", image_only=True)

    assert any("line_items" in e and "not-a-number" in e for e in result.validation_errors)


def test_empty_line_items_is_a_missing_field_not_an_error() -> None:
    # An invoice with no rows at all has not supplied a required field. Retrying cannot
    # conjure rows that are not in the document, so this is missing, not invalid.
    extracted = {
        "invoice_number": fv("INV-1001", None),
        "invoice_date": fv("2026-01-10", None),
        "customer_name": fv("Jane Tan", None),
        "line_items": [],
        "total_amount": fv("150.00", None),
    }

    result = validate(DocType.INVOICE, extracted, "", image_only=True)

    assert "line_items" in result.missing_fields
    assert not any("line_items" in e for e in result.validation_errors)


def test_line_item_with_a_fabricated_snippet_is_rejected() -> None:
    # Nested cells carry evidence for the same reason top level fields do, so a fabricated
    # citation inside a row is caught rather than trusted because it is nested.
    source = "Invoice INV-1001\nRepair work 100.00\n"
    extracted = {
        "invoice_number": fv("INV-1001", "Invoice INV-1001"),
        "invoice_date": fv(None, None),
        "customer_name": fv(None, None),
        "line_items": [row("100.00", "Repair work", snippet="a quote that is not present")],
        "total_amount": fv("100.00", None),
    }

    result = validate(DocType.INVOICE, extracted, source, image_only=False)

    assert any("line_items[0]" in e for e in result.validation_errors)
    assert result.extracted["line_items"][0]["amount"]["verified"] is False


def test_unparseable_monetary_value_is_a_validation_error() -> None:
    # Protects the currency parsing check: garbage where an amount should be is caught rather
    # than silently passed through.
    extracted = {
        "invoice_number": fv("INV-1001", None),
        "line_items": [row("100.00")],
        "total_amount": fv("not-a-number", None),
    }

    result = validate(DocType.INVOICE, extracted, "", image_only=True)

    assert any("not a valid monetary amount" in e for e in result.validation_errors)


def test_value_with_no_snippet_is_rejected() -> None:
    # Protects the "evidence required" rule: a value with no snippet at all is treated the
    # same as a fabricated one, since the design gives every value an evidence trail.
    extracted = {
        "claimant_name": fv("Jane Tan", None),
    }

    result = validate(DocType.CLAIM_FORM, extracted, "Claimant: Jane Tan\n", image_only=False)

    assert result.extracted["claimant_name"]["value"] is None
    assert result.extracted["claimant_name"]["verified"] is False
    assert any("no evidence snippet" in e for e in result.validation_errors)


def test_decimal_arithmetic_is_exact_not_float() -> None:
    # Protects against float rounding: 0.1 + 0.2 != 0.3 in binary floating point, but is exact
    # in Decimal. If _parse_amount ever switched to float, this would start failing.
    one_tenth = _parse_amount("0.1")
    two_tenths = _parse_amount("0.2")
    # Narrowed rather than asserted inline: _parse_amount returns None for anything it
    # cannot parse, and a test that silently skipped its own assertion on a None would pass
    # while proving nothing.
    assert one_tenth is not None and two_tenths is not None
    assert one_tenth + two_tenths == Decimal("0.3")
    assert isinstance(one_tenth, Decimal)

    extracted = {
        "invoice_number": fv("INV-1001", None),
        "line_items": fv("0.10; 0.20", None),
        "total_amount": fv("0.30", None),
    }

    result = validate(DocType.INVOICE, extracted, "", image_only=True)

    assert result.validation_errors == []


def test_identity_and_policy_number_format_violations_are_validation_errors() -> None:
    # Protects the regex checks, otherwise never exercised: a malformed identity number and a
    # malformed policy number should each be caught and named.
    extracted = {
        "full_name": fv("Jane Tan", None),
        "identity_number": fv("not-an-id", None),
        "date_of_birth": fv("1990-01-01", None),
        "policy_number": fv("12345", None),
    }

    result = validate(DocType.IDENTITY_DOCUMENT, extracted, "", image_only=True)

    assert any("identity_number" in e for e in result.validation_errors)
    assert any("policy_number" in e for e in result.validation_errors)
