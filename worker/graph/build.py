"""Graph wiring. The edges here are the diagram in architecture section 4.

The graph performs no database calls and no SQS calls. It takes state in and returns state
out. That is what makes it runnable in tests with no AWS and no database, and it is why the
consumer, not the graph, owns status writes.
"""

from __future__ import annotations

from langgraph.graph import END, START, StateGraph

from graph import nodes
from graph.routers import (
    EXTRACTOR_BY_TYPE,
    GENERIC_EXTRACTOR,
    has_terminal_error,
    route_by_type,
    route_by_validation,
)
from graph.state import State

EXTRACTION_NODES = [*dict.fromkeys(EXTRACTOR_BY_TYPE.values()), GENERIC_EXTRACTOR]


def _continue_or_fail(next_node: str):  # type: ignore[no-untyped-def]
    """Every node can fail terminally. This is the dotted edge in the section 4 diagram."""

    def _route(state: State) -> str:
        return "record_failure" if has_terminal_error(state) else next_node

    return _route


def build_graph():  # type: ignore[no-untyped-def]
    g = StateGraph(State)

    g.add_node("load_document", nodes.load_document)
    g.add_node("classify", nodes.classify)
    for name in EXTRACTION_NODES:
        g.add_node(name, getattr(nodes, name))
    g.add_node("mark_unsupported", nodes.mark_unsupported)
    g.add_node("validate", nodes.validate)
    g.add_node("mask_pii", nodes.mask_pii)
    g.add_node("generate_report", nodes.generate_report)
    g.add_node("record_failure", nodes.record_failure)

    g.add_edge(START, "load_document")

    # load_document is where an unreadable or encrypted file is caught, so it is the most
    # likely source of a terminal error.
    g.add_conditional_edges(
        "load_document",
        _continue_or_fail("classify"),
        ["classify", "record_failure"],
    )

    # First real router: different document types take different extraction paths.
    g.add_conditional_edges(
        "classify",
        route_by_type,
        [*EXTRACTION_NODES, "mark_unsupported", "record_failure"],
    )

    for name in EXTRACTION_NODES:
        g.add_conditional_edges(
            name,
            _continue_or_fail("validate"),
            ["validate", "record_failure"],
        )

    # Second real router, and the cycle: an invalid extraction goes back to the same
    # extraction node with the errors appended, at most once.
    g.add_conditional_edges(
        "validate",
        route_by_validation,
        [*EXTRACTION_NODES, "mask_pii", "record_failure"],
    )

    # An unsupported document still gets a report, so a user can see why it was declined.
    g.add_edge("mark_unsupported", "mask_pii")

    # Masking sits here deliberately: after the last validation pass, which needs real
    # values to check ID formats and cross field rules, and before the report and its
    # summary, which must never contain them.
    g.add_conditional_edges(
        "mask_pii",
        _continue_or_fail("generate_report"),
        ["generate_report", "record_failure"],
    )

    g.add_conditional_edges(
        "generate_report",
        _continue_or_fail(END),
        [END, "record_failure"],
    )

    g.add_edge("record_failure", END)

    return g.compile()
