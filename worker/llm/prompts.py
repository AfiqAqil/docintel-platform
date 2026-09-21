"""Prompt builders for the LLM assisted nodes.

Plain functions, no classes: each one takes the state a node has in hand and returns a
LangChain message list ready to pass to `with_structured_output(...).invoke(...)`. Keeping
prompt text out of the nodes is what lets the prompts be read and changed on their own,
without wading through node control flow.

Text input and image input are both supported, because architecture section 5 renders a
scanned or photographed document to page images when there is no usable text layer. Only one
of the two is ever sent: text when there is text, images when there is not.
"""

from __future__ import annotations

import base64
import json
from typing import Any

from langchain_core.messages import BaseMessage, HumanMessage, SystemMessage

from graph.state import DocType
from llm.schemas import SCHEMA_BY_TYPE

# Shared by every prompt that can cite source text, so the instruction reads the same way
# wherever it appears rather than drifting between call sites.
SNIPPET_INSTRUCTION = (
    "Only report a field's value if you can back it with an exact, verbatim quote from the "
    "source text, placed in that field's snippet. If you cannot find a direct quote for a "
    "value, leave the field null rather than guessing. A snippet that is not an exact match "
    "for the source text is treated as a fabricated citation and the field is discarded. "
    # Found by running a real table-layout invoice. In extracted text a table's header row and
    # its cells are far apart, so "Amount 425.00" is not a span that exists anywhere, even
    # though both words do. The model cited it that way, the check rightly rejected it, and a
    # valid invoice came back INCOMPLETE. Our own sample used inline labels ("Qty: 1") and
    # never exercised this.
    "A snippet must be ONE contiguous span copied character for character from the text. "
    "Never join text from two places, and never prepend a label or a column header that is "
    "not immediately next to the value in the text. For a value inside a table, the snippet "
    "is the cell's own text and nothing else, for example '425.00', not 'Amount 425.00'."
)

_DOC_TYPE_LIST = ", ".join(t.value for t in DocType)


def _image_content(intro: str, images: list[bytes]) -> list[str | dict[Any, Any]]:
    """Build a LangChain multimodal content list: one text block, then one image block per page.

    Images arrive as raw PNG bytes from `load_document`; the base64 source type is what
    LangChain's multimodal content block format expects.
    """
    content: list[str | dict[Any, Any]] = [{"type": "text", "text": intro}]
    for image in images:
        content.append(
            {
                "type": "image",
                "source_type": "base64",
                "data": base64.b64encode(image).decode("ascii"),
                "mime_type": "image/png",
            }
        )
    return content


def classification_messages(text: str | None, images: list[bytes] | None) -> list[BaseMessage]:
    """Messages for the classify node."""
    system = SystemMessage(
        content=(
            "You are a document classifier for an insurance document intake pipeline. Read "
            "the document and classify it into exactly one of these types: "
            f"{_DOC_TYPE_LIST}. Use 'unknown' if the document genuinely does not fit any of "
            "the others. Give a short rationale for your choice, describing the document "
            "in general terms. Do not quote or repeat any name, address, phone number, "
            "email address, identity number, policy number or other identifier in the "
            "rationale: it is stored in the report, so anything quoted there outlives the "
            "document itself."
        )
    )

    if images and not text:
        human = HumanMessage(
            content=_image_content(
                "Classify this document. It is provided as page images because no usable "
                "text layer was available.",
                images,
            )
        )
    else:
        human = HumanMessage(
            content=f"Classify the following document text.\n\n--- DOCUMENT TEXT ---\n"
            f"{text or ''}\n--- END DOCUMENT TEXT ---"
        )

    return [system, human]


def extraction_messages(
    doc_type: DocType,
    text: str | None,
    images: list[bytes] | None,
    previous_errors: list[str] | None,
) -> list[BaseMessage]:
    """Messages for an extraction node.

    `previous_errors` is what makes this the retry prompt described in architecture section
    4: route_by_validation sends the state back to the same extraction node with the
    validation errors appended, so the model sees exactly what was wrong last time.
    """
    schema = SCHEMA_BY_TYPE.get(doc_type)
    schema_name = schema.__name__ if schema else "the extraction schema"
    label = doc_type.value.replace("_", " ")

    system_text = (
        f"You are an information extraction assistant for an insurance document intake "
        f"pipeline. Extract the fields defined by {schema_name} from this {label} document. "
        f"{SNIPPET_INSTRUCTION}"
    )

    if previous_errors:
        system_text += (
            "\n\nThis is a retry. The previous attempt failed validation with these errors:\n"
            + "\n".join(f"- {error}" for error in previous_errors)
            + "\nCorrect every one of them in this attempt."
        )

    system = SystemMessage(content=system_text)

    if images and not text:
        human = HumanMessage(
            content=_image_content(
                "Extract the fields from this document. It is provided as page images "
                "because no usable text layer was available.",
                images,
            )
        )
    else:
        human = HumanMessage(
            content=f"Extract the fields from the following document text.\n\n"
            f"--- DOCUMENT TEXT ---\n{text or ''}\n--- END DOCUMENT TEXT ---"
        )

    return [system, human]


def summary_messages(masked_state_fragment: dict[str, Any]) -> list[BaseMessage]:
    """Messages for the report's summary, built from state that mask_pii has already scrubbed.

    The fragment is masked before it ever reaches this function, but the prompt still tells
    the model not to invent information and not to include identifiers, since a masked value
    like '••••••-••-4321' is still an identifier shape that does not belong in prose meant
    for a reviewer.
    """
    system = SystemMessage(
        content=(
            "You write a one paragraph summary of a processed document for a human "
            "reviewer. Use only the information given below, which has already had "
            "identifying details masked. Do not invent or infer any fact that is not present "
            "in the given information. Do not include any identifiers, numbers or codes in "
            "the summary, masked or not, describe what was found in general terms only. "
            "Write in plain English, exactly one paragraph, no headings, no bullet points."
        )
    )
    human = HumanMessage(
        content="Write the summary from this processed document state:\n\n"
        f"{json.dumps(masked_state_fragment, indent=2, default=str)}"
    )
    return [system, human]
