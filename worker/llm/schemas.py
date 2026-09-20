"""Structured output schemas for the LLM assisted nodes.

Every extraction schema is built out of `ExtractedField`, one value plus the verbatim source
span it came from. The snippet is not optional decoration: `validate` (in `rules/`) checks
that every snippet actually appears in the source text and drops the field if it does not.
That deterministic check is the hallucination guard architecture section 5 describes, and it
is the reason every field carries a snippet at all.

Sensitive fields are tagged `{"pii": True}`, or `{"pii": "tail"}` for the few that are true
identifiers and may keep their last few characters visible so a reviewer can tell two records
apart. Tagging is opt in both ways: an untagged field is not masked, and a field tagged
`True` is masked whole. Forgetting a tag should never be the thing that reveals a value, so
the safer behaviour is the default in each case that matters.

Two things every extraction schema exposes, read back by other modules rather than guessed at:

  - `REQUIRED_FIELDS`, a `ClassVar` tuple of field names, consumed by `validate` to produce
    `missing_fields`.
  - PII tags, `json_schema_extra={"pii": True}` on every sensitive field, read back by
    `rules.mask.pii_field_names()` unchanged. Sensitive means identity and passport numbers,
    dates of birth, full addresses, bank and card numbers, phone numbers, email addresses and
    personal names, per architecture section 4.
"""

from __future__ import annotations

from typing import ClassVar

from pydantic import BaseModel, Field

from graph.state import DocType


class Classification(BaseModel):
    """Structured output for the classify node."""

    doc_type: DocType = Field(
        description="The document type, chosen from the fixed set of supported types."
    )
    confidence: float = Field(
        ge=0.0,
        le=1.0,
        description="Confidence in this classification, from 0.0 (not confident) to 1.0 "
        "(fully confident).",
    )
    notes: str = Field(
        description="A short rationale for this classification, one or two sentences."
    )


class ExtractedField(BaseModel):
    """One extracted value, plus the exact source text it was read from.

    `snippet` must be a verbatim quote, copied character for character from the document,
    never paraphrased. A later deterministic check confirms the snippet is really in the
    source text and rejects the field otherwise, so a field with no genuine quote to back it
    should be left null rather than guessed at.
    """

    value: str | None = Field(
        default=None,
        description="The extracted value as plain text, or null if it is not present in the "
        "document.",
    )
    snippet: str | None = Field(
        default=None,
        description="The exact, verbatim span of source text this value was quoted from, "
        "copied character for character from the document. Do not paraphrase, summarise or "
        "correct it. Null if the value is not present or cannot be backed by a direct quote.",
    )


class LineItem(BaseModel):
    """One row of an invoice.

    Each cell keeps its own snippet, the same way a top level field does, so a single line
    item can be verified independently of the invoice total.
    """

    description: ExtractedField = Field(default_factory=ExtractedField)
    quantity: ExtractedField = Field(default_factory=ExtractedField)
    unit_price: ExtractedField = Field(default_factory=ExtractedField)
    amount: ExtractedField = Field(default_factory=ExtractedField)


class ClaimFormExtraction(BaseModel):
    """Fields extracted from `DocType.CLAIM_FORM`."""

    REQUIRED_FIELDS: ClassVar[tuple[str, ...]] = (
        "policy_number",
        "claim_reference",
        "claimant_name",
        "date_of_incident",
        "incident_description",
        "claimed_amount",
    )

    policy_number: ExtractedField = Field(
        default_factory=ExtractedField,
        description="The insurance policy number this claim is filed against.",
    )
    claim_reference: ExtractedField = Field(
        default_factory=ExtractedField, description="The claim reference or claim number."
    )
    claimant_name: ExtractedField = Field(
        default_factory=ExtractedField,
        description="The full name of the claimant or policyholder filing the claim.",
        json_schema_extra={"pii": True},
    )
    claimant_address: ExtractedField = Field(
        default_factory=ExtractedField,
        description="The claimant's full postal address.",
        json_schema_extra={"pii": True},
    )
    claimant_phone: ExtractedField = Field(
        default_factory=ExtractedField,
        description="The claimant's phone number.",
        # "tail" rather than True: the last four digits are how a reviewer tells two phone
        # numbers apart, and that is the only reason a tail exists.
        json_schema_extra={"pii": "tail"},
    )
    claimant_email: ExtractedField = Field(
        default_factory=ExtractedField,
        description="The claimant's email address.",
        json_schema_extra={"pii": True},
    )
    date_of_incident: ExtractedField = Field(
        default_factory=ExtractedField,
        description="The date the incident being claimed for occurred.",
    )
    date_filed: ExtractedField = Field(
        default_factory=ExtractedField, description="The date this claim form was filed."
    )
    incident_description: ExtractedField = Field(
        default_factory=ExtractedField,
        description="A description of what happened, in the claimant's or form's own words.",
    )
    incident_location: ExtractedField = Field(
        default_factory=ExtractedField, description="Where the incident took place."
    )
    claimed_amount: ExtractedField = Field(
        default_factory=ExtractedField, description="The monetary amount being claimed."
    )


class InvoiceExtraction(BaseModel):
    """Fields extracted from `DocType.INVOICE`."""

    REQUIRED_FIELDS: ClassVar[tuple[str, ...]] = (
        "invoice_number",
        "invoice_date",
        "customer_name",
        "line_items",
        "total_amount",
    )

    invoice_number: ExtractedField = Field(
        default_factory=ExtractedField, description="The invoice number."
    )
    invoice_date: ExtractedField = Field(
        default_factory=ExtractedField, description="The date the invoice was issued."
    )
    vendor_name: ExtractedField = Field(
        default_factory=ExtractedField,
        description="The name of the business that issued the invoice.",
    )
    customer_name: ExtractedField = Field(
        default_factory=ExtractedField,
        description="The full name of the customer being billed.",
        json_schema_extra={"pii": True},
    )
    customer_address: ExtractedField = Field(
        default_factory=ExtractedField,
        description="The customer's full billing or postal address.",
        json_schema_extra={"pii": True},
    )
    policy_number: ExtractedField = Field(
        default_factory=ExtractedField,
        description="The insurance policy or claim reference this invoice relates to, if any.",
    )
    line_items: list[LineItem] = Field(
        default_factory=list,
        description="Every line item on the invoice, in the order they appear.",
    )
    subtotal: ExtractedField = Field(
        default_factory=ExtractedField, description="The subtotal before tax, if stated separately."
    )
    tax_amount: ExtractedField = Field(
        default_factory=ExtractedField, description="The tax amount, if stated separately."
    )
    total_amount: ExtractedField = Field(
        default_factory=ExtractedField,
        description="The final total amount due on the invoice. Checked deterministically "
        "against the sum of the line items.",
    )


class IdentityExtraction(BaseModel):
    """Fields extracted from `DocType.IDENTITY_DOCUMENT`."""

    REQUIRED_FIELDS: ClassVar[tuple[str, ...]] = (
        "full_name",
        "date_of_birth",
        "identity_number",
    )

    document_type: ExtractedField = Field(
        default_factory=ExtractedField,
        description="The kind of identity document, for example passport or driver's licence.",
    )
    full_name: ExtractedField = Field(
        default_factory=ExtractedField,
        description="The full name on the document.",
        json_schema_extra={"pii": True},
    )
    date_of_birth: ExtractedField = Field(
        default_factory=ExtractedField,
        description="The date of birth on the document.",
        json_schema_extra={"pii": True},
    )
    identity_number: ExtractedField = Field(
        default_factory=ExtractedField,
        description="The identity, passport or licence number on the document.",
        # "tail" rather than True: an identity number is exactly the case the visible tail
        # was designed for. Everything else on this schema stays fully masked.
        json_schema_extra={"pii": "tail"},
    )
    nationality: ExtractedField = Field(
        default_factory=ExtractedField, description="The nationality stated on the document."
    )
    address: ExtractedField = Field(
        default_factory=ExtractedField,
        description="The full address on the document, if one is printed on it.",
        json_schema_extra={"pii": True},
    )
    issue_date: ExtractedField = Field(
        default_factory=ExtractedField, description="The date the document was issued."
    )
    expiry_date: ExtractedField = Field(
        default_factory=ExtractedField, description="The date the document expires."
    )
    issuing_authority: ExtractedField = Field(
        default_factory=ExtractedField,
        description="The authority or country that issued the document.",
    )


class GenericExtraction(BaseModel):
    """Fields extracted from `DocType.POLICY_DOCUMENT`, `CUSTOMER_CORRESPONDENCE` and
    `SUPPORTING_EVIDENCE`.

    These three types are grouped because none of them share a required shape: a policy
    document has a reference number, a photo of damage may have neither a reference number
    nor a date. `REQUIRED_FIELDS` is deliberately empty rather than forcing a false
    requirement on a type it does not fit.
    """

    REQUIRED_FIELDS: ClassVar[tuple[str, ...]] = ()

    document_title: ExtractedField = Field(
        default_factory=ExtractedField, description="The title or heading of the document, if any."
    )
    reference_number: ExtractedField = Field(
        default_factory=ExtractedField,
        description="Any policy number, claim reference or other document reference found in "
        "the document.",
    )
    related_party_name: ExtractedField = Field(
        default_factory=ExtractedField,
        description="The name of the person the document is about or addressed to, if any.",
        json_schema_extra={"pii": True},
    )
    date: ExtractedField = Field(
        default_factory=ExtractedField, description="The single most relevant date on the document."
    )
    monetary_amount: ExtractedField = Field(
        default_factory=ExtractedField,
        description="The single most relevant monetary amount on the document, if any.",
    )
    summary: ExtractedField = Field(
        default_factory=ExtractedField,
        description="A short, one or two sentence description of what this document is and "
        "what it contains.",
    )


# Selects the extraction schema for a document type without an if/elif chain. DocType.UNKNOWN
# is deliberately absent: route_by_type() in graph/routers.py sends it to mark_unsupported
# before any extraction node runs, so it never needs a schema.
SCHEMA_BY_TYPE: dict[DocType, type[BaseModel]] = {
    DocType.CLAIM_FORM: ClaimFormExtraction,
    DocType.INVOICE: InvoiceExtraction,
    DocType.IDENTITY_DOCUMENT: IdentityExtraction,
    DocType.POLICY_DOCUMENT: GenericExtraction,
    DocType.CUSTOMER_CORRESPONDENCE: GenericExtraction,
    DocType.SUPPORTING_EVIDENCE: GenericExtraction,
}
