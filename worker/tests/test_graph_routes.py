"""Every route through the graph, driven by a fake model with no network call.

The point of these tests is the wiring, not the prompts: that each document type reaches its
own extractor, that the retry cycle goes back to the same extractor exactly once, that low
confidence and unknown types are declined rather than guessed at, that a terminal error ends
at record_failure, and that masking happens after validation and before the report.
"""

from __future__ import annotations

import pytest

from graph.build import build_graph
from graph.state import DocType, Outcome
from llm.schemas import SCHEMA_BY_TYPE, Classification
from tests.documents import DOCX_CONTENT_TYPE, make_docx, make_png

CLAIM_TEXT = (
    "CLAIM FORM\n"
    "Policyholder: Jordan Avery\n"
    "Policy number: POL-4471920\n"
    "Claim reference: CLM-2026-0831\n"
    "Incident date: 2026-03-02\n"
    "Claim date: 2026-03-09\n"
    "Amount claimed: 1,450.00 SGD\n"
    "Incident: Water damage to the kitchen following a burst pipe.\n"
)


def _classification(doc_type: DocType, confidence: float = 0.95) -> Classification:
    return Classification(doc_type=doc_type, confidence=confidence, notes="scripted for test")


def _field(schema, **values):
    """Build an extraction schema instance with only the named fields set.

    Unspecified fields are left to their defaults rather than set to None, because every
    field is an ExtractedField and not an optional one; passing None fails validation.
    """
    return schema(**values)


def _initial(text=CLAIM_TEXT):
    """A starting state carrying real file bytes.

    The graph is driven from its actual entry point, so load_document really parses the
    document. Pre-populating `text` instead would exercise a path production never takes.
    """
    data = make_docx(text)
    return {
        "document_id": "00000000-0000-0000-0000-000000000001",
        "s3_key": "uploads/00000000-0000-0000-0000-000000000001",
        "content_type": DOCX_CONTENT_TYPE,
        "filename": "sample.docx",
        "file_size": len(data),
        "raw_bytes": data,
    }


def _run(graph, fake_llm, responses, *, text=CLAIM_TEXT):
    fake_llm(responses)
    return graph.invoke(_initial(text))


@pytest.fixture
def graph():
    return build_graph()


def _nodes_visited(result) -> list[str]:
    return [step["node"] for step in result["trace"]]


# ---------------------------------------------------------------------------------------
# route_by_type: one test per document type, proving each takes its own path
# ---------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "doc_type,expected_node",
    [
        (DocType.CLAIM_FORM, "extract_claim"),
        (DocType.INVOICE, "extract_invoice"),
        (DocType.IDENTITY_DOCUMENT, "extract_identity"),
        (DocType.POLICY_DOCUMENT, "extract_generic"),
        (DocType.CUSTOMER_CORRESPONDENCE, "extract_generic"),
        (DocType.SUPPORTING_EVIDENCE, "extract_generic"),
    ],
)
def test_each_document_type_reaches_its_own_extractor(graph, fake_llm, doc_type, expected_node):
    """route_by_type is a real router: the type decides which extraction node runs."""
    schema = SCHEMA_BY_TYPE[doc_type]
    result = _run(
        graph,
        fake_llm,
        [_classification(doc_type), _field(schema), "A one paragraph summary."],
    )
    assert expected_node in _nodes_visited(result)
    assert result["report"]["document_type"] == str(doc_type)


# ---------------------------------------------------------------------------------------
# route_by_type: declining rather than guessing
# ---------------------------------------------------------------------------------------


def test_unknown_type_is_unsupported_not_guessed(graph, fake_llm):
    """An unknown type must never reach an extractor."""
    result = _run(
        graph,
        fake_llm,
        [_classification(DocType.UNKNOWN, 0.99), "A one paragraph summary."],
    )
    visited = _nodes_visited(result)
    assert "mark_unsupported" in visited
    assert not any(n.startswith("extract_") for n in visited)
    assert result["outcome"] == Outcome.UNSUPPORTED


def test_low_confidence_is_unsupported_even_for_a_known_type(graph, fake_llm):
    """Confidence below the threshold is treated exactly like an unknown type.

    This is the guard against a confident extraction against the wrong schema, which nothing
    downstream would flag.
    """
    result = _run(
        graph,
        fake_llm,
        [_classification(DocType.CLAIM_FORM, 0.20), "A one paragraph summary."],
    )
    visited = _nodes_visited(result)
    assert "mark_unsupported" in visited
    assert "extract_claim" not in visited
    assert result["outcome"] == Outcome.UNSUPPORTED


def test_unsupported_still_produces_a_report(graph, fake_llm):
    """Declining is a business result, not a failure, so the user still gets a report."""
    result = _run(
        graph,
        fake_llm,
        [_classification(DocType.UNKNOWN), "A one paragraph summary."],
    )
    assert result["report"]["outcome"] == str(Outcome.UNSUPPORTED)
    assert result["report"]["observations"]


# ---------------------------------------------------------------------------------------
# route_by_validation: the cycle
# ---------------------------------------------------------------------------------------


def test_invalid_extraction_retries_the_same_extractor_once(graph, fake_llm):
    """The cycle: back to the same node, with the errors appended, exactly once.

    The first extraction cites a snippet that does not appear in the source, which is a
    fabricated citation and therefore a validation error. That triggers one retry. The
    second attempt cites a real snippet and passes.
    """
    from llm.schemas import ExtractedField

    schema = SCHEMA_BY_TYPE[DocType.CLAIM_FORM]
    bad = _field(
        schema,
        policy_number=ExtractedField(
            value="POL-9999999", snippet="Policy number: POL-9999999"
        ),
    )
    good = _field(
        schema,
        policy_number=ExtractedField(
            value="POL-4471920", snippet="Policy number: POL-4471920"
        ),
    )

    model = fake_llm([_classification(DocType.CLAIM_FORM), bad, good, "Summary."])
    result = graph.invoke(_initial())

    visited = _nodes_visited(result)
    assert visited.count("extract_claim") == 2, visited
    assert visited.count("validate") == 2, visited
    assert result["extraction_attempts"] == 2
    # Four model calls: classify, extract, re-extract, summarise. No more.
    assert len(model.calls) == 4


def test_retry_budget_is_not_exceeded(graph, fake_llm):
    """A model that keeps fabricating must not loop forever.

    After the budget is spent the graph proceeds with what it has, and the outcome is
    INCOMPLETE rather than a hang or a failure.
    """
    from llm.schemas import ExtractedField

    schema = SCHEMA_BY_TYPE[DocType.CLAIM_FORM]
    bad = _field(
        schema,
        policy_number=ExtractedField(value="POL-9999999", snippet="not in the source at all"),
    )

    result = _run(
        graph,
        fake_llm,
        [_classification(DocType.CLAIM_FORM), bad, bad, "Summary."],
    )
    assert _nodes_visited(result).count("extract_claim") == 2
    assert result["outcome"] == Outcome.INCOMPLETE


def test_missing_fields_do_not_trigger_a_retry(graph, fake_llm):
    """A field the document does not contain will not appear however often it is asked for.

    Missing is routed differently from invalid, and this is the test that holds that apart.
    """
    schema = SCHEMA_BY_TYPE[DocType.CLAIM_FORM]
    empty = _field(schema)

    model = fake_llm([_classification(DocType.CLAIM_FORM), empty, "Summary."])
    result = graph.invoke(_initial())

    assert _nodes_visited(result).count("extract_claim") == 1
    assert result["missing_fields"]
    assert result["outcome"] == Outcome.INCOMPLETE
    # Three calls only: classify, extract, summarise. No retry.
    assert len(model.calls) == 3


# ---------------------------------------------------------------------------------------
# terminal failure
# ---------------------------------------------------------------------------------------


def test_unreadable_file_ends_at_record_failure(graph, fake_llm):
    """An encrypted or corrupt file is the document's fault, so it is terminal.

    No model call is made at all, and the run ends at record_failure rather than producing
    a half filled report.
    """
    model = fake_llm([])
    result = graph.invoke(
        {
            "document_id": "d",
            "content_type": "application/zip",
            "filename": "archive.zip",
            "raw_bytes": b"PK\x03\x04not a document",
        }
    )

    visited = _nodes_visited(result)
    assert visited == ["load_document", "record_failure"], visited
    assert result["error"]["kind"] == "terminal"
    assert result["report"]["outcome"] == "FAILED"
    assert model.calls == []


def test_transient_error_leaves_the_graph_rather_than_failing_the_document(graph, fake_llm):
    """A throttled model call is not the document's fault.

    The exception must propagate out of the graph so the consumer does not ack and SQS
    redelivers. If this were caught and routed to record_failure, a blip would permanently
    mark a document failed with no automatic way back.
    """

    class ThrottlingException(Exception):
        pass

    fake_llm([ThrottlingException("Too many tokens per day")])

    with pytest.raises(ThrottlingException):
        graph.invoke(_initial())


# ---------------------------------------------------------------------------------------
# masking order
# ---------------------------------------------------------------------------------------


def test_pii_is_masked_in_the_report_but_validated_unmasked(graph, fake_llm):
    """Masking runs after the last validation pass and before the report.

    Validation must see the real identity number, because the format rule cannot check a
    masked value. The report must not contain it. Both halves are asserted here, because
    getting the order wrong breaks one or the other silently.
    """
    from llm.schemas import ExtractedField

    id_text = "IDENTITY CARD\nName: Jordan Avery\nID number: S1234567D\n"
    schema = SCHEMA_BY_TYPE[DocType.IDENTITY_DOCUMENT]
    pii_fields = [
        name
        for name, f in schema.model_fields.items()
        if isinstance(getattr(f, "json_schema_extra", None), dict)
        and f.json_schema_extra.get("pii") is True
    ]
    assert pii_fields, "the identity schema must tag at least one field as pii"

    target = pii_fields[0]
    extraction = _field(
        schema,
        **{target: ExtractedField(value="S1234567D", snippet="ID number: S1234567D")},
    )

    result = _run(
        graph,
        fake_llm,
        [_classification(DocType.IDENTITY_DOCUMENT), extraction, "Summary."],
        text=id_text,
    )

    masked = result["report"]["extracted"][target]["value"]
    assert masked != "S1234567D"
    assert "•" in masked
    # The raw value must not survive anywhere in the serialised report.
    import json

    assert "S1234567D" not in json.dumps(result["report"], default=str)


def test_summary_prompt_is_built_from_masked_state(graph, fake_llm):
    """The summary is generated after masking, so the prompt itself cannot leak a value."""
    from llm.schemas import ExtractedField

    id_text = "IDENTITY CARD\nID number: S7654321B\n"
    schema = SCHEMA_BY_TYPE[DocType.IDENTITY_DOCUMENT]
    target = next(
        name
        for name, f in schema.model_fields.items()
        if isinstance(getattr(f, "json_schema_extra", None), dict)
        and f.json_schema_extra.get("pii") is True
    )
    extraction = _field(
        schema, **{target: ExtractedField(value="S7654321B", snippet="ID number: S7654321B")}
    )

    model = fake_llm([_classification(DocType.IDENTITY_DOCUMENT), extraction, "Summary."])
    graph.invoke(_initial(id_text))

    summary_call = model.calls[-1]
    rendered = " ".join(str(m.content) for m in summary_call)
    assert "S7654321B" not in rendered


# ---------------------------------------------------------------------------------------
# snippet verification, through the graph
# ---------------------------------------------------------------------------------------


def test_fabricated_snippet_is_rejected_and_reported(graph, fake_llm):
    """The deterministic hallucination check, seen from the outside.

    A snippet that is not in the source means the model invented the citation, so the field
    is rejected rather than trusted.
    """
    from llm.schemas import ExtractedField

    schema = SCHEMA_BY_TYPE[DocType.CLAIM_FORM]
    fabricated = _field(
        schema,
        policy_number=ExtractedField(
            value="POL-0000000", snippet="Policy number: POL-0000000"
        ),
    )

    result = _run(
        graph,
        fake_llm,
        [_classification(DocType.CLAIM_FORM), fabricated, fabricated, "Summary."],
    )

    assert result["report"]["extracted"]["policy_number"]["verified"] is False
    assert result["report"]["extracted"]["policy_number"]["value"] is None
    assert any("policy_number" in e for e in result["report"]["validation_errors"])


def test_image_only_input_skips_snippet_verification(graph, fake_llm):
    """With no text layer there is nothing to match a snippet against.

    Verification is skipped rather than failed, so a scanned document is not penalised for
    something that cannot be checked, and the report says the extraction is unverified.
    """
    from llm.schemas import ExtractedField

    schema = SCHEMA_BY_TYPE[DocType.CLAIM_FORM]
    extraction = _field(
        schema,
        policy_number=ExtractedField(value="POL-4471920", snippet="Policy number: POL-4471920"),
    )

    fake_llm([_classification(DocType.CLAIM_FORM), extraction, "Summary."])
    result = graph.invoke(
        {
            "document_id": "d",
            "content_type": "image/png",
            "filename": "scan.png",
            "raw_bytes": make_png(),
        }
    )

    assert result["report"]["extracted"]["policy_number"]["verified"] is None
    assert any("unverified" in o.lower() for o in result["report"]["observations"])


# ---------------------------------------------------------------------------------------
# trace and report shape
# ---------------------------------------------------------------------------------------


def test_every_node_appends_exactly_one_trace_entry(graph, fake_llm):
    """Workflow status tracking depends on the trace, so it must be complete and ordered."""
    schema = SCHEMA_BY_TYPE[DocType.CLAIM_FORM]
    result = _run(
        graph,
        fake_llm,
        [_classification(DocType.CLAIM_FORM), _field(schema), "Summary."],
    )
    visited = _nodes_visited(result)
    assert visited[0] == "load_document"
    assert visited[1] == "classify"
    assert visited[-1] == "generate_report"
    assert all(isinstance(s["duration_ms"], int) for s in result["trace"])


def test_report_carries_every_key_the_assignment_asks_for(graph, fake_llm):
    """Document type, summary, extracted information, missing information, outcome, notes."""
    schema = SCHEMA_BY_TYPE[DocType.CLAIM_FORM]
    result = _run(
        graph,
        fake_llm,
        [_classification(DocType.CLAIM_FORM), _field(schema), "Summary."],
    )
    report = result["report"]
    for key in (
        "document_id",
        "generated_at",
        "document_type",
        "classification",
        "outcome",
        "summary",
        "extracted",
        "missing_information",
        "validation_errors",
        "observations",
        "trace",
    ):
        assert key in report, f"report is missing {key}"


# ---------------------------------------------------------------------------------------
# Regressions. Each of these reproduces a defect found in review.
# ---------------------------------------------------------------------------------------


def test_classification_notes_cannot_carry_pii_into_the_report(graph, fake_llm):
    """The classifier's rationale is free text written from the unmasked document.

    It is persisted in the report, so without scrubbing it is a way around every other
    guard: a note naming the claimant carries that name straight through while the field it
    came from is properly masked.
    """
    from llm.schemas import ExtractedField

    id_text = "IDENTITY CARD\nName: Jordan Avery\nID number: A12345678\n"
    leaky_note = "Identity card belonging to Jordan Avery, number A12345678."

    schema = SCHEMA_BY_TYPE[DocType.IDENTITY_DOCUMENT]
    extraction = _field(
        schema,
        full_name=ExtractedField(value="Jordan Avery", snippet="Name: Jordan Avery"),
        identity_number=ExtractedField(value="A12345678", snippet="ID number: A12345678"),
    )

    result = _run(
        graph,
        fake_llm,
        [
            Classification(
                doc_type=DocType.IDENTITY_DOCUMENT, confidence=0.95, notes=leaky_note
            ),
            extraction,
            "Summary.",
        ],
        text=id_text,
    )

    import json

    serialised = json.dumps(result["report"], default=str)
    assert "Jordan Avery" not in serialised
    assert "A12345678" not in serialised


def test_only_identifier_fields_keep_a_visible_tail():
    """A tail is for telling two reference numbers apart, and nothing else.

    Deciding from the value rather than the schema meant any text containing a digit kept
    its last four characters, so an address like "12 Main Street" was partly revealed.
    """
    from llm.schemas import ClaimFormExtraction, IdentityExtraction
    from rules.mask import mask_value, pii_tail_fields

    assert pii_tail_fields(IdentityExtraction) == {"identity_number"}
    assert pii_tail_fields(ClaimFormExtraction) == {"claimant_phone"}

    # An address is sensitive but is not an identifier, so it is masked whole even though it
    # contains digits.
    assert mask_value("12 Main Street") == "•• •••• ••••••"
    # An identity number is exactly the case the tail was designed for.
    assert mask_value("A12345678", reveal_tail=True).endswith("5678")


def test_an_address_is_masked_whole_in_the_report(graph, fake_llm):
    """End to end version of the rule above, through the real schema tags."""
    from llm.schemas import ExtractedField

    text = (
        "CLAIM FORM\nPolicy number: POL-4471920\nClaimant: Jordan Avery\n"
        "Address: 12 Main Street\nPhone: +65 9123 4567\n"
    )
    schema = SCHEMA_BY_TYPE[DocType.CLAIM_FORM]
    extraction = _field(
        schema,
        claimant_address=ExtractedField(value="12 Main Street", snippet="Address: 12 Main Street"),
        claimant_phone=ExtractedField(value="+65 9123 4567", snippet="Phone: +65 9123 4567"),
    )

    result = _run(
        graph, fake_llm, [_classification(DocType.CLAIM_FORM), extraction, "Summary."], text=text
    )

    extracted = result["report"]["extracted"]
    # Fully masked: no run of the original characters survives anywhere.
    assert "Main" not in extracted["claimant_address"]["value"]
    assert "reet" not in extracted["claimant_address"]["value"]
    # The phone keeps its tail, because that is what tells two numbers apart.
    assert extracted["claimant_phone"]["value"].endswith("4567")
