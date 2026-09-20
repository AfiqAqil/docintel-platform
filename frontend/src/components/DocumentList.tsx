import type { DocumentOut } from "../types";
import { StatusBadge } from "./StatusBadge";

type Props = {
  documents: DocumentOut[];
  selectedId: string | null;
  onSelect: (doc: DocumentOut) => void;
};

// The API already returns documents newest first (created_at desc), which is
// what "processing history" means here - there is no separate history view.
export function DocumentList({ documents, selectedId, onSelect }: Props): JSX.Element {
  if (documents.length === 0) {
    return <p className="empty-state">No documents uploaded yet.</p>;
  }

  return (
    <ul className="document-list">
      {documents.map((doc) => (
        <li
          key={doc.id}
          className={doc.id === selectedId ? "document-row selected" : "document-row"}
          onClick={() => onSelect(doc)}
        >
          <div className="document-row-main">
            <span className="document-filename">{doc.filename}</span>
            <StatusBadge status={doc.status} />
          </div>
          <div className="document-row-meta">
            {doc.status === "PROCESSING" && doc.current_step ? (
              <span>Current step: {doc.current_step}</span>
            ) : null}
            {doc.doc_type ? <span>Type: {doc.doc_type}</span> : null}
            {doc.outcome ? <span>Outcome: {doc.outcome}</span> : null}
            <span>Updated {new Date(doc.updated_at).toLocaleString()}</span>
          </div>
        </li>
      ))}
    </ul>
  );
}
