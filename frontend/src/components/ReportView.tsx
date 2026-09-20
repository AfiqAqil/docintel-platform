import { useEffect, useState } from "react";
import { ApiError, getReport } from "../api";
import type { DocumentOut, Report } from "../types";
import { StatusBadge } from "./StatusBadge";

type Props = {
  doc: DocumentOut;
};

// Four outcomes, not two, and the difference matters to whoever reads the report.
//
//   true   the evidence snippet was found in the source text
//   false  it was not, so the field was rejected as a fabricated citation
//   null   nothing was checked, for one of two quite different reasons
//
// The two null cases have to be told apart. A field with no value was never extracted, so
// there was nothing to verify. A field with a value and no verification means the input was
// image only and there was no text to match a snippet against. Labelling both as "image
// only" told the reader something false about a text document, which is worse than saying
// nothing: it invents a reason.
function verifiedLabel(verified: boolean | null, hasValue: boolean): string {
  if (verified === true) return "Verified";
  if (verified === false) return "Rejected - evidence not found in source";
  if (!hasValue) return "Not present in the document";
  return "Unverified - image only input, no text to check";
}

function verifiedClass(verified: boolean | null, hasValue: boolean): string {
  if (verified === true) return "verified verified-true";
  if (verified === false) return "verified verified-false";
  if (!hasValue) return "verified verified-absent";
  return "verified verified-null";
}

function ListSection({ title, items }: { title: string; items: string[] }): JSX.Element | null {
  if (items.length === 0) return null;
  return (
    <section>
      <h3>{title}</h3>
      <ul>
        {items.map((item) => (
          <li key={item}>{item}</li>
        ))}
      </ul>
    </section>
  );
}

function StatusBlock({ doc }: { doc: DocumentOut }): JSX.Element {
  return (
    <div className="status-block">
      <StatusBadge status={doc.status} />
      {doc.status === "PROCESSING" && doc.current_step ? <p>Current step: {doc.current_step}</p> : null}
      {doc.status === "EXPIRED" ? <p>The upload slot expired before a file arrived.</p> : null}
      {doc.status === "UPLOADING" || doc.status === "QUEUED" ? <p>Waiting to be picked up for processing.</p> : null}
    </div>
  );
}

// Shows everything the assignment asks for in a report: classification and
// confidence, each field's verification state and evidence snippet, missing
// information, validation errors, observations, and the trace through the graph.
export function ReportView({ doc }: Props): JSX.Element {
  const [report, setReport] = useState<Report | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    setReport(null);
    setLoadError(null);

    if (doc.status !== "COMPLETED") {
      return;
    }

    getReport(doc.id)
      .then((r) => {
        if (!cancelled) setReport(r);
      })
      .catch((e: unknown) => {
        if (cancelled) return;
        setLoadError(e instanceof ApiError ? e.message : "Could not load the report.");
      });

    return () => {
      cancelled = true;
    };
  }, [doc.id, doc.status]);

  if (doc.status === "FAILED") {
    return (
      <div className="report-view">
        <h2>{doc.filename}</h2>
        <StatusBlock doc={doc} />
        <p className="error-text">Processing failed: {doc.error_message ?? "unknown error"}</p>
      </div>
    );
  }

  if (doc.status !== "COMPLETED") {
    return (
      <div className="report-view">
        <h2>{doc.filename}</h2>
        <StatusBlock doc={doc} />
      </div>
    );
  }

  if (loadError) {
    return (
      <div className="report-view">
        <h2>{doc.filename}</h2>
        <p className="error-text">{loadError}</p>
      </div>
    );
  }

  if (!report) {
    return (
      <div className="report-view">
        <h2>{doc.filename}</h2>
        <p>Loading report...</p>
      </div>
    );
  }

  const fields = Object.entries(report.extracted);

  return (
    <div className="report-view">
      <h2>{doc.filename}</h2>

      <section>
        <h3>Classification</h3>
        <p>
          Document type: <strong>{report.document_type}</strong> (confidence{" "}
          {(report.classification.confidence * 100).toFixed(0)}%)
        </p>
        {report.classification.notes ? <p className="notes">{report.classification.notes}</p> : null}
        <p>
          Outcome: <strong>{report.outcome}</strong>
        </p>
      </section>

      <section>
        <h3>Summary</h3>
        <p>{report.summary}</p>
      </section>

      <section>
        <h3>Extracted fields</h3>
        {fields.length === 0 ? (
          <p className="empty-state">No fields were extracted.</p>
        ) : (
          <table className="fields-table">
            <thead>
              <tr>
                <th>Field</th>
                <th>Value</th>
                <th>Verification</th>
                <th>Evidence snippet</th>
              </tr>
            </thead>
            <tbody>
              {fields.map(([name, field]) => (
                <tr key={name}>
                  <td>{name}</td>
                  <td>{field.value}</td>
                  <td className={verifiedClass(field.verified, field.value !== null)}>
                    {verifiedLabel(field.verified, field.value !== null)}
                  </td>
                  <td className="snippet">{field.snippet}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </section>

      <ListSection title="Missing information" items={report.missing_information} />
      <ListSection title="Validation errors" items={report.validation_errors} />
      <ListSection title="Observations" items={report.observations} />

      <section>
        <h3>Trace</h3>
        <ol className="trace-list">
          {report.trace.map((step, i) => (
            <li key={`${step.node}-${i}`} className={step.status === "error" ? "trace-step trace-error" : "trace-step trace-ok"}>
              <span className="trace-node">{step.node}</span>
              <span className="trace-status">{step.status}</span>
              <span className="trace-duration">{step.duration_ms} ms</span>
              {step.detail ? <span className="trace-detail">{step.detail}</span> : null}
            </li>
          ))}
        </ol>
      </section>
    </div>
  );
}
