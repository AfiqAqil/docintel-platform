"""Run the graph over the sample documents, locally, against a real model.

This is the check the unit tests cannot make. They drive the graph with a fake, which proves
the wiring and the routing; this proves the prompts and the schemas actually work against a
model, on real files, end to end. No AWS, no database, no queue: the consumer's job of
fetching bytes from S3 is done here by reading a file.

Usage:
    LLM_PROVIDER=openai LLM_MODEL_ID=gpt-4o-mini python run_local.py
    python run_local.py --only invoice_repair.pdf
"""

from __future__ import annotations

import argparse
import json
import mimetypes
import sys
import time
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

from graph.build import build_graph  # noqa: E402
from graph.state import DocType  # noqa: E402

# The content type a browser would send. Guessed from the extension here, because there is no
# browser in this path; in production it arrives on the upload request and is stored.
EXTRA_TYPES = {
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
}


def content_type_for(path: Path) -> str:
    if path.suffix in EXTRA_TYPES:
        return EXTRA_TYPES[path.suffix]
    return mimetypes.guess_type(path.name)[0] or "application/octet-stream"


def summarise(name: str, state: dict[str, Any], elapsed: float) -> None:
    """One compact block per document, readable without opening the JSON."""
    report = state.get("report") or {}
    trace = state.get("trace") or []
    path = " -> ".join(step["node"] for step in trace)

    print(f"\n{'=' * 78}")
    print(f"{name}   [{elapsed:.1f}s]")
    print("=" * 78)
    print(f"  type       : {report.get('document_type', '?')}"
          f"   confidence {state.get('confidence', 0):.2f}")
    print(f"  outcome    : {report.get('outcome', '?')}")
    print(f"  path       : {path}")

    notes = (report.get("classification") or {}).get("notes")
    if notes:
        print(f"  rationale  : {notes}")

    summary = report.get("summary")
    if summary:
        print(f"  summary    : {summary}")

    extracted = report.get("extracted") or {}
    populated = {
        k: v for k, v in extracted.items()
        if isinstance(v, dict) and v.get("value") is not None
    }
    if populated:
        print("  extracted  :")
        for key, field in populated.items():
            flag = {True: "verified", False: "REJECTED", None: "unverified"}[field.get("verified")]
            print(f"      {key:24} {str(field['value'])[:46]:46} [{flag}]")

    rows = extracted.get("line_items")
    if isinstance(rows, list) and rows:
        print(f"  line items : {len(rows)} rows")

    for label, key in (
        ("missing    ", "missing_information"),
        ("errors     ", "validation_errors"),
        ("notes      ", "observations"),
    ):
        values = report.get(key) or []
        for value in values:
            print(f"  {label}: {value}")

    if report.get("error"):
        print(f"  FAILED     : {report['error']['message']}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--samples", default="../samples/out")
    parser.add_argument("--out", default="../samples/reports")
    parser.add_argument("--only", default=None, help="run a single filename")
    args = parser.parse_args()

    samples = sorted(Path(args.samples).iterdir())
    if args.only:
        samples = [p for p in samples if p.name == args.only]
        if not samples:
            print(f"no sample named {args.only}", file=sys.stderr)
            return 1

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    graph = build_graph()
    results: list[tuple[str, str, str]] = []
    started_all = time.monotonic()

    for path in samples:
        started = time.monotonic()
        initial = {
            "document_id": path.stem,
            "s3_key": f"uploads/{path.stem}",
            "filename": path.name,
            "content_type": content_type_for(path),
            "file_size": path.stat().st_size,
            # In production the consumer fetches these from S3. The graph makes no AWS call
            # either way, which is exactly what lets this script exist.
            "raw_bytes": path.read_bytes(),
        }

        try:
            state = graph.invoke(initial)
        except Exception as exc:
            # A transient error propagates out of the graph on purpose, so that SQS
            # redelivers rather than the document being marked failed. Locally there is no
            # queue to redeliver it, so it is reported and the run continues.
            elapsed = time.monotonic() - started
            print(f"\n{path.name}: TRANSIENT {type(exc).__name__}: {exc}")
            results.append((path.name, "TRANSIENT", type(exc).__name__))
            continue

        elapsed = time.monotonic() - started
        summarise(path.name, state, elapsed)

        report = state.get("report") or {}
        (out_dir / f"{path.stem}.json").write_text(json.dumps(report, indent=2, default=str))
        results.append(
            (
                path.name,
                str(report.get("outcome", "?")),
                str(report.get("document_type", DocType.UNKNOWN)),
            )
        )

    print(f"\n{'=' * 78}")
    print(f"SUMMARY   {len(results)} documents in {time.monotonic() - started_all:.1f}s")
    print("=" * 78)
    for name, outcome, doc_type in results:
        print(f"  {name:32} {doc_type:26} {outcome}")
    print(f"\nreports written to {out_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
