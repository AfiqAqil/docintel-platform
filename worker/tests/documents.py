"""Builders for real test documents.

The graph route tests drive the graph from its actual entry point, so load_document
really parses a file. These build genuine DOCX and PNG bytes for that, rather than the
tests pre-populating the `text` key and exercising a path production never takes.
"""

from __future__ import annotations


def make_docx(text: str) -> bytes:
    """Build a real DOCX carrying this text, so load_document actually parses something.

    The graph route tests drive the whole graph including load_document, so they need real
    file bytes rather than a pre-populated `text` key: pre-populating it would be testing a
    path production never takes. DOCX is used because python-docx can write one in three
    lines and it is already a dependency, and because the document type the graph decides on
    comes from the classifier, not from the file format.
    """
    import io

    from docx import Document

    document = Document()
    for line in text.splitlines():
        document.add_paragraph(line)
    buffer = io.BytesIO()
    document.save(buffer)
    return buffer.getvalue()


def make_png(width: int = 64, height: int = 64) -> bytes:
    """A small real PNG, for the image only path."""
    import io

    from PIL import Image

    buffer = io.BytesIO()
    Image.new("RGB", (width, height), "white").save(buffer, format="PNG")
    return buffer.getvalue()


DOCX_CONTENT_TYPE = (
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
)
