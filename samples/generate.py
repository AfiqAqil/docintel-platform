"""Generates the synthetic sample documents in samples/out/.

Every document here is fictional (architecture assumption 8): invented people, an invented
insurer, addresses and phone numbers drawn from ranges reserved for fiction. Nothing here is a
real customer, company or place.

The field values are chosen to satisfy the deterministic rules in worker/rules/validate.py
(identity and policy number formats, date parsing, currency parsing, invoice arithmetic) where
a sample is meant to pass, and to violate exactly one rule where a sample is meant to fail. The
docstring in each generator function below names which rule and which file.

Randomness is used only for that password, so re-running this script produces byte-identical output for
every file except corrupt_encrypted.pdf, which is encrypted with a throwaway password that
is generated per run and never stored, so its bytes differ each time by design.
"""

from __future__ import annotations

import argparse
import io
import secrets
import zipfile
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from pathlib import Path

import pypdfium2 as pdfium
from docx import Document
from PIL import Image, ImageDraw, ImageFont
from pypdf import PdfReader, PdfWriter
from reportlab.lib.pagesizes import A4
from reportlab.lib.units import mm
from reportlab.lib.utils import ImageReader
from reportlab.pdfgen import canvas as pdfcanvas

# ---------------------------------------------------------------------------------------------
# Shared fictional case data. Reused across documents so the sample set reads as one
# consistent (invented) case file rather than eleven unrelated fixtures.
# ---------------------------------------------------------------------------------------------

CLAIMANT_NAME = "Priya Wexford"
CLAIMANT_ADDRESS = "42 Windmere Lane, Rivermouth RM1 4QS"
# 07700 900xxx is the UK numbering range reserved by Ofcom for fiction and drama use, so this
# cannot collide with a real subscriber.
CLAIMANT_PHONE = "+44 7700 900123"
# example.com is reserved for documentation by RFC 2606, so this cannot collide with a real
# mailbox either.
CLAIMANT_EMAIL = "priya.wexford@example.com"

# POLICY_NUMBER_RE = ^POL-\d{6,10}$ (worker/rules/validate.py).
POLICY_NUMBER = "POL-004821"
CLAIM_REFERENCE = "CLM-2026-00931"
DATE_FILED = "2026-06-10"
DATE_OF_INCIDENT = "2026-06-02"
INCIDENT_DESCRIPTION = "Kitchen pipe burst causing water damage to flooring and cabinets."
INCIDENT_LOCATION = "42 Windmere Lane, Rivermouth"
CLAIMED_AMOUNT = "3250.00"

INSURER_NAME = "Quillmarsh Mutual Assurance"

VENDOR_NAME = "Rustlebrook Auto Repairs"
INVOICE_NUMBER = "INV-2026-00456"
INVOICE_DATE = "2026-05-14"

# IDENTITY_NUMBER_RE = ^[A-Z]\d{8}$ (worker/rules/validate.py).
IDENTITY_NUMBER = "B87654321"
IDENTITY_NAME = "Milo Thornwood"
IDENTITY_DOB = "1990-03-14"
IDENTITY_NATIONALITY = "Avantian"
IDENTITY_AUTHORITY = "Avantia Department of Identity Services"
IDENTITY_ISSUE_DATE = "2022-01-10"
IDENTITY_EXPIRY_DATE = "2032-01-10"
IDENTITY_ADDRESS = "17 Cobalt Street, Avantia City"

PAGE_SIZE = A4
MARGIN = 20 * mm


# ---------------------------------------------------------------------------------------------
# PDF helper: a plain "one field per line" text page, real text layer via reportlab.
# ---------------------------------------------------------------------------------------------


def _text_pdf(title: str, lines: list[str]) -> bytes:
    buffer = io.BytesIO()
    # invariant=True drops the creation timestamp and document id reportlab would otherwise
    # embed, which is what makes re-running this script produce byte-identical PDFs.
    c = pdfcanvas.Canvas(buffer, pagesize=PAGE_SIZE, invariant=True)
    width, height = PAGE_SIZE

    def new_page() -> float:
        c.setFont("Helvetica-Bold", 16)
        c.drawString(MARGIN, height - MARGIN, title)
        c.setFont("Helvetica", 11)
        return height - MARGIN - 12 * mm

    y = new_page()
    for line in lines:
        if y < MARGIN:
            c.showPage()
            y = new_page()
        c.drawString(MARGIN, y, line)
        y -= 7 * mm
    c.showPage()
    c.save()
    return buffer.getvalue()


def _render_pdf_first_page_to_image(pdf_bytes: bytes) -> Image.Image:
    """Render a PDF's first page to a PIL image, used to build the scanned sample.

    pypdfium2 only, per architecture section 5: PyMuPDF is AGPL licensed and is banned.
    """
    pdf = pdfium.PdfDocument(pdf_bytes)
    page = pdf[0]
    return page.render(scale=2.0).to_pil()


def _image_only_pdf(image: Image.Image) -> bytes:
    """Wrap a full-page image in a PDF with no text objects at all.

    This is what makes claim_form_scanned.pdf exercise the pypdfium2 render path in
    rules/parse.py: pypdf.extract_text() finds nothing, so load_document falls back to
    page images.
    """
    buffer = io.BytesIO()
    c = pdfcanvas.Canvas(buffer, pagesize=PAGE_SIZE, invariant=True)
    width, height = PAGE_SIZE
    # Fit the image inside the page without distorting its aspect ratio.
    scale = min(width / image.width, height / image.height)
    draw_width, draw_height = image.width * scale, image.height * scale
    x = (width - draw_width) / 2
    y = (height - draw_height) / 2
    c.drawImage(
        ImageReader(image), x, y, width=draw_width, height=draw_height, preserveAspectRatio=True
    )
    c.showPage()
    c.save()
    return buffer.getvalue()


# ---------------------------------------------------------------------------------------------
# Claim form. One line-builder shared by the complete, incomplete and bad-dates variants, plus
# the scanned version, so the four documents read as the same form.
# ---------------------------------------------------------------------------------------------


def _claim_form_lines(
    *,
    include_claim_reference: bool = True,
    include_claimed_amount: bool = True,
    date_of_incident: str = DATE_OF_INCIDENT,
) -> list[str]:
    lines = [
        f"Insurer: {INSURER_NAME}",
        f"Policy Number: {POLICY_NUMBER}",
    ]
    if include_claim_reference:
        lines.append(f"Claim Reference: {CLAIM_REFERENCE}")
    lines += [
        f"Claimant Name: {CLAIMANT_NAME}",
        f"Claimant Address: {CLAIMANT_ADDRESS}",
        f"Claimant Phone: {CLAIMANT_PHONE}",
        f"Claimant Email: {CLAIMANT_EMAIL}",
        f"Date of Incident: {date_of_incident}",
        f"Date Filed: {DATE_FILED}",
        f"Incident Location: {INCIDENT_LOCATION}",
        f"Incident Description: {INCIDENT_DESCRIPTION}",
    ]
    if include_claimed_amount:
        lines.append(f"Claimed Amount: {CLAIMED_AMOUNT}")
    return lines


def make_claim_form_complete() -> bytes:
    """Happy path. Every ClaimFormExtraction.REQUIRED_FIELDS name is present and valid."""
    return _text_pdf("Insurance Claim Form", _claim_form_lines())


def make_claim_form_incomplete() -> bytes:
    """Omits claim_reference and claimed_amount entirely, so both land in missing_fields."""
    return _text_pdf(
        "Insurance Claim Form",
        _claim_form_lines(include_claim_reference=False, include_claimed_amount=False),
    )


def make_claim_form_bad_dates() -> bytes:
    """date_of_incident after date_filed: the one cross-field rule validate.py names by name."""
    return _text_pdf("Insurance Claim Form", _claim_form_lines(date_of_incident="2026-06-15"))


def make_claim_form_scanned() -> bytes:
    """Same complete claim form, rendered to a page image with no text layer at all."""
    complete_pdf = make_claim_form_complete()
    page_image = _render_pdf_first_page_to_image(complete_pdf)
    return _image_only_pdf(page_image)


# ---------------------------------------------------------------------------------------------
# Invoice: 4 line items summing exactly to the total, one amount with a thousands separator.
# ---------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class LineItem:
    description: str
    quantity: str
    unit_price: str
    amount: Decimal


LINE_ITEMS: list[LineItem] = [
    LineItem("Front bumper replacement", "1", "450.00", Decimal("450.00")),
    LineItem("Paint and refinishing", "1", "320.50", Decimal("320.50")),
    LineItem("Labor, 12 hours", "12", "85.00", Decimal("1020.00")),
    LineItem("Diagnostic scan", "1", "75.00", Decimal("75.00")),
]
INVOICE_TOTAL = sum((item.amount for item in LINE_ITEMS), Decimal("0.00"))


def make_invoice_repair() -> bytes:
    """4 line items summing exactly to the total. Labor's amount carries a thousands comma."""
    lines = [
        f"Vendor: {VENDOR_NAME}",
        f"Invoice Number: {INVOICE_NUMBER}",
        f"Invoice Date: {INVOICE_DATE}",
        f"Customer Name: {CLAIMANT_NAME}",
        f"Customer Address: {CLAIMANT_ADDRESS}",
        f"Related Policy Number: {POLICY_NUMBER}",
        "",
        "Line Items:",
    ]
    for index, item in enumerate(LINE_ITEMS, start=1):
        # The third line item is the one with a thousands separator, matching the spec's "at
        # least one amount with a thousands separator comma".
        amount_text = f"{item.amount:,.2f}" if index == 3 else f"{item.amount:.2f}"
        lines.append(
            f"  {index}. {item.description} | Qty: {item.quantity} | "
            f"Unit Price: {item.unit_price} | Amount: {amount_text}"
        )
    lines += [
        "",
        f"Subtotal: {INVOICE_TOTAL:.2f}",
        "Tax Amount: 0.00",
        f"Total Amount: {INVOICE_TOTAL:,.2f}",
    ]
    return _text_pdf("Repair Invoice", lines)


# ---------------------------------------------------------------------------------------------
# Restaurant menu: out of scope document type, real text layer, routes to UNSUPPORTED.
# ---------------------------------------------------------------------------------------------


def make_restaurant_menu() -> bytes:
    """No claim, policy, invoice or identity content at all: classification has nowhere to
    route it but UNSUPPORTED."""
    lines = [
        "Starters",
        "  Roasted Pepper Soup - 6.50",
        "  Garlic Flatbread - 4.00",
        "",
        "Mains",
        "  Hollowbrook Fish Stew - 15.00",
        "  Wild Mushroom Risotto - 13.50",
        "  Charred Vegetable Skewers - 12.00",
        "",
        "Desserts",
        "  Honey Almond Cake - 6.00",
        "  Spiced Pear Tart - 6.50",
        "",
        "All dishes are prepared fresh to order. Please inform your server of any allergies.",
        "Open Tuesday to Sunday, noon until late.",
    ]
    return _text_pdf("Thistlewick Kitchen - Seasonal Menu", lines)


# ---------------------------------------------------------------------------------------------
# Encrypted PDF: unreadable by design, terminal failure.
# ---------------------------------------------------------------------------------------------

# Not derived from anything worth reproducing, and deliberately never printed or written to
# README.md, per the assignment's "a password you do NOT publish".
def _throwaway_password() -> str:
    """A password that exists only for the duration of this call and is never stored.

    The point of this fixture is a PDF nobody can open. A password committed next to it is
    not that, whatever the file contains: it is a password in a repository, and the habit is
    worse than the file. Generating one and discarding it means no copy of it exists
    anywhere once the function returns.

    This is the one sample whose bytes differ between runs, and deliberately so. The
    determinism the other ten have is worth having because a diff under out/ then means the
    generator changed rather than that it was run again. Keeping that property here would
    require the password to be reproducible, which is the same as keeping it.
    """
    return secrets.token_urlsafe(32)


def make_corrupt_encrypted() -> bytes:
    """A normal PDF, then encrypted. load_document must treat this as unreadable."""
    plain = _text_pdf(
        "Restricted Document",
        ["This document requires a password that has not been shared with this pipeline."],
    )
    reader = PdfReader(io.BytesIO(plain))
    writer = PdfWriter()
    for page in reader.pages:
        writer.add_page(page)
    # Encrypted with a password that is generated here and never returned, logged or
    # stored, so the file is unopenable by anyone including us. load_document tries only the
    # empty password, so this is the encrypted branch of the PDF reader, not a corrupt file.
    writer.encrypt(_throwaway_password())
    out = io.BytesIO()
    writer.write(out)
    return out.getvalue()


# ---------------------------------------------------------------------------------------------
# DOCX documents.
# ---------------------------------------------------------------------------------------------

# A DOCX is a zip archive, and python-docx stamps every entry with the current wall clock time
# at save(), which alone would make the output different on every run even though the XML
# content is identical. Rewriting every entry with this fixed date is what makes re-running
# this script produce byte-identical DOCX files.
_ZIP_EPOCH = (1980, 1, 1, 0, 0, 0)


def _normalize_docx(data: bytes) -> bytes:
    source = zipfile.ZipFile(io.BytesIO(data))
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as out:
        for info in source.infolist():
            info.date_time = _ZIP_EPOCH
            out.writestr(info, source.read(info.filename))
    return buffer.getvalue()


def make_policy_document() -> bytes:
    """Exercises python-docx paragraphs and a table, whose cell text is read separately from
    paragraph text in rules/parse.py."""
    doc = Document()
    doc.add_heading(f"{INSURER_NAME} - Home Insurance Policy", level=1)
    doc.add_paragraph(f"Policy Number: {POLICY_NUMBER}")
    doc.add_paragraph(f"Policyholder: {CLAIMANT_NAME}")
    doc.add_paragraph(f"Property Address: {CLAIMANT_ADDRESS}")
    doc.add_paragraph(
        "This policy provides cover for the property and contents named above, subject to the "
        "limits and exclusions set out in the schedule below."
    )

    doc.add_heading("Coverage Schedule", level=2)
    table = doc.add_table(rows=1, cols=2)
    table.style = "Light Grid Accent 1"
    header = table.rows[0].cells
    header[0].text = "Coverage Type"
    header[1].text = "Limit"
    rows = [
        ("Buildings", "250,000.00"),
        ("Contents", "60,000.00"),
        ("Accidental Water Damage", "25,000.00"),
        ("Alternative Accommodation", "10,000.00"),
    ]
    for coverage_type, limit in rows:
        cells = table.add_row().cells
        cells[0].text = coverage_type
        cells[1].text = limit

    doc.add_heading("General Conditions", level=2)
    doc.add_paragraph(
        "The policyholder must notify the insurer of any incident within 14 days of it "
        "occurring. Claims are assessed against the coverage schedule above."
    )
    doc.add_paragraph("Policy period: 2026-01-01 to 2026-12-31.")

    buffer = io.BytesIO()
    doc.save(buffer)
    return _normalize_docx(buffer.getvalue())


def make_customer_correspondence() -> bytes:
    """Prose only, exercised through the generic extractor: a complaint letter about a claim."""
    doc = Document()
    doc.add_paragraph(f"{CLAIMANT_NAME}")
    doc.add_paragraph(f"{CLAIMANT_ADDRESS}")
    doc.add_paragraph("2026-07-02")
    doc.add_paragraph("")
    doc.add_paragraph(f"Re: Claim {CLAIM_REFERENCE} - Request for Update")
    doc.add_paragraph("")
    doc.add_paragraph(f"Dear {INSURER_NAME} Claims Team,")
    doc.add_paragraph(
        f"I am writing regarding my claim, reference {CLAIM_REFERENCE}, filed on {DATE_FILED} "
        f"under policy {POLICY_NUMBER}. It has now been several weeks since I reported the "
        "water damage to my kitchen, and I have not received any update on the assessment."
    )
    doc.add_paragraph(
        "I would appreciate a call back this week with a status update, and an indication of "
        "when the repair estimate will be reviewed. My contact details are on file."
    )
    doc.add_paragraph("Yours sincerely,")
    doc.add_paragraph(CLAIMANT_NAME)

    buffer = io.BytesIO()
    doc.save(buffer)
    return _normalize_docx(buffer.getvalue())


# ---------------------------------------------------------------------------------------------
# Images: an identity card layout and a standalone damage photo, both drawn with Pillow, no
# text layer, so load_document's image path is what handles them (image_only=True).
# ---------------------------------------------------------------------------------------------


def _label_font(size: int) -> ImageFont.FreeTypeFont:
    return ImageFont.load_default(size=size)


def make_identity_card() -> bytes:
    # Height covers 8 label/value pairs at a 55px step starting from y=130, plus a bottom
    # margin, so the last field (Expiry Date) is not clipped by the canvas edge.
    width, height = 900, 650
    image = Image.new("RGB", (width, height), (235, 240, 245))
    draw = ImageDraw.Draw(image)

    draw.rectangle([0, 0, width - 1, height - 1], outline=(40, 60, 90), width=6)
    draw.rectangle([0, 0, width - 1, 90], fill=(40, 60, 90))
    draw.text((30, 25), "AVANTIA NATIONAL IDENTITY CARD", font=_label_font(28), fill="white")

    # Photo placeholder: a plain silhouette-coloured block, not a real likeness.
    draw.rectangle([30, 130, 230, 400], fill=(190, 195, 205), outline=(40, 60, 90), width=3)
    draw.ellipse([90, 170, 170, 250], fill=(150, 155, 165))
    draw.polygon([(70, 380), (190, 380), (170, 290), (90, 290)], fill=(150, 155, 165))

    fields = [
        ("Full Name", IDENTITY_NAME),
        ("Date of Birth", IDENTITY_DOB),
        ("Identity Number", IDENTITY_NUMBER),
        ("Nationality", IDENTITY_NATIONALITY),
        ("Address", IDENTITY_ADDRESS),
        ("Issuing Authority", IDENTITY_AUTHORITY),
        ("Issue Date", IDENTITY_ISSUE_DATE),
        ("Expiry Date", IDENTITY_EXPIRY_DATE),
    ]
    y = 130
    for label, value in fields:
        draw.text((260, y), f"{label}:", font=_label_font(20), fill=(60, 70, 90))
        draw.text((260, y + 24), value, font=_label_font(24), fill=(20, 25, 35))
        y += 55

    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def make_damage_photo() -> bytes:
    """A standalone synthetic scene, not a document scan: supporting evidence for a claim."""
    width, height = 800, 600
    image = Image.new("RGB", (width, height), (120, 150, 190))  # sky
    draw = ImageDraw.Draw(image)
    draw.rectangle([0, 380, width, height], fill=(90, 90, 95))  # ground / driveway

    # A car-shaped block with a visibly dented, discoloured panel.
    draw.rounded_rectangle([120, 250, 680, 400], radius=30, fill=(180, 40, 40))
    draw.rectangle([220, 200, 480, 260], fill=(150, 30, 30))  # roofline
    draw.ellipse([170, 370, 260, 460], fill=(30, 30, 30))  # wheel
    draw.ellipse([540, 370, 630, 460], fill=(30, 30, 30))  # wheel

    # The dent: an irregular darker patch on the door panel.
    draw.polygon(
        [(360, 300), (420, 290), (450, 330), (410, 360), (350, 350)],
        fill=(90, 20, 20),
    )
    draw.line([(360, 300), (450, 330)], fill=(50, 10, 10), width=4)

    buffer = io.BytesIO()
    image.save(buffer, format="JPEG", quality=90)
    return buffer.getvalue()


# ---------------------------------------------------------------------------------------------
# Self-check. The smallest thing that fails if the arithmetic or the format rules drift out of
# sync with worker/rules/validate.py. Not a test framework: one assert block, run on every
# invocation before any file is written.
# ---------------------------------------------------------------------------------------------


def _selftest() -> None:
    import re

    # Copies of the two patterns from worker/rules/validate.py. Duplicated rather than
    # imported, because samples/ must not depend on worker/ (worker is the shipped
    # application, samples/ is dev tooling that generates fixtures for it).
    identity_re = re.compile(r"^[A-Z]\d{8}$")
    policy_re = re.compile(r"^POL-\d{6,10}$")

    assert identity_re.match(IDENTITY_NUMBER), f"{IDENTITY_NUMBER} fails IDENTITY_NUMBER_RE"
    assert policy_re.match(POLICY_NUMBER), f"{POLICY_NUMBER} fails POLICY_NUMBER_RE"

    datetime.fromisoformat(DATE_OF_INCIDENT)
    datetime.fromisoformat(DATE_FILED)
    assert datetime.fromisoformat(DATE_OF_INCIDENT) <= datetime.fromisoformat(DATE_FILED), (
        "the happy-path claim form's incident date must not be after its filed date"
    )
    assert datetime.fromisoformat("2026-06-15") > datetime.fromisoformat(DATE_FILED), (
        "the bad-dates sample must actually have incident after filed"
    )

    line_sum = sum((item.amount for item in LINE_ITEMS), Decimal("0.00"))
    assert line_sum == INVOICE_TOTAL, f"line items sum to {line_sum}, not {INVOICE_TOTAL}"
    assert len(LINE_ITEMS) >= 4, "invoice needs at least 4 line items"


# ---------------------------------------------------------------------------------------------
# Entry point.
# ---------------------------------------------------------------------------------------------

GENERATORS: dict[str, Callable[[], bytes]] = {
    "claim_form_complete.pdf": make_claim_form_complete,
    "claim_form_incomplete.pdf": make_claim_form_incomplete,
    "claim_form_bad_dates.pdf": make_claim_form_bad_dates,
    "invoice_repair.pdf": make_invoice_repair,
    "policy_document.docx": make_policy_document,
    "customer_correspondence.docx": make_customer_correspondence,
    "identity_card.png": make_identity_card,
    "damage_photo.jpg": make_damage_photo,
    "claim_form_scanned.pdf": make_claim_form_scanned,
    "restaurant_menu.pdf": make_restaurant_menu,
    "corrupt_encrypted.pdf": make_corrupt_encrypted,
}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--out", type=Path, default=Path(__file__).parent / "out", help="Output directory."
    )
    args = parser.parse_args()

    _selftest()

    args.out.mkdir(parents=True, exist_ok=True)
    for filename, generator in GENERATORS.items():
        data = generator()
        path = args.out / filename
        path.write_bytes(data)
        print(f"wrote {path} ({len(data)} bytes)")


if __name__ == "__main__":
    main()
