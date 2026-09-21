"""Masking, tested directly.

The graph route tests prove masking runs in the right place. These prove the scrubbing itself
finds a sensitive value wherever snippet verification would have accepted it.
"""

from __future__ import annotations

from rules.mask import redact
from rules.snippets import verify_snippet

ADDRESS = "62 Mossglass Lane, Varrow, Elandor"
# How pypdf actually returns that address when the PDF wraps it onto a second line.
WRAPPED_SNIPPET = "Address\n62 Mossglass Lane, Varrow,\nElandor"


def test_a_value_is_masked_even_when_the_snippet_wraps_it_across_lines() -> None:
    """Found by running a real claim form. The field's value was masked and its evidence
    snippet was published in full, because the address wrapped onto a second line in the
    source. The snippet has a newline where the value has a space, so an exact text match
    found nothing to redact, and a home address went into the report unmasked."""
    # The premise: verification is whitespace tolerant, so this snippet is accepted.
    assert verify_snippet(WRAPPED_SNIPPET, "Motor Claim Form\n" + WRAPPED_SNIPPET) is True

    masked = redact(WRAPPED_SNIPPET, [(ADDRESS, False)])

    assert "Mossglass" not in masked
    assert "Varrow" not in masked
    assert "Elandor" not in masked
    # The label is not sensitive and stays, so a reviewer can still see what the field was.
    assert masked.startswith("Address")


def test_a_value_is_masked_whatever_its_capitalisation() -> None:
    """Verification ignores case, so masking has to as well: a document that prints a name
    in capitals must not slip past a value the model returned in title case."""
    masked = redact("Claimant: JORAH FEN", [("Jorah Fen", False)])

    assert "JORAH" not in masked
    assert "FEN" not in masked


def test_an_exact_occurrence_is_still_masked_and_keeps_its_tail() -> None:
    masked = redact("Identity Number: A12344321", [("A12344321", True)])

    assert "A1234" not in masked
    assert masked.endswith("4321")


def test_text_without_the_value_is_left_alone() -> None:
    text = "Policy Number: POL-004821"

    assert redact(text, [("Jorah Fen", False)]) == text
