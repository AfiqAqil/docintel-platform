import { useCallback, useState } from "react";
import { DocumentList } from "./components/DocumentList";
import { ReportView } from "./components/ReportView";
import { Toasts } from "./components/Toasts";
import type { Toast, ToastKind } from "./components/Toasts";
import { UploadPanel } from "./components/UploadPanel";
import { usePolling } from "./usePolling";
import type { StatusTransition } from "./usePolling";

const TOAST_LIFETIME_MS = 6000;

let toastCounter = 0;

export function App(): JSX.Element {
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [toasts, setToasts] = useState<Toast[]>([]);

  const dismissToast = useCallback((id: string) => {
    setToasts((current) => current.filter((t) => t.id !== id));
  }, []);

  const addToast = useCallback(
    (message: string, kind: ToastKind) => {
      toastCounter += 1;
      const id = `toast-${toastCounter}`;
      setToasts((current) => [...current, { id, message, kind }]);
      window.setTimeout(() => dismissToast(id), TOAST_LIFETIME_MS);
    },
    [dismissToast]
  );

  // Completion and failure notification: fires once per transition, not per poll.
  const handleTransition = useCallback(
    ({ document }: StatusTransition) => {
      if (document.status === "COMPLETED") {
        addToast(`${document.filename} finished processing.`, "success");
      } else if (document.status === "FAILED") {
        addToast(`${document.filename} failed: ${document.error_message ?? "unknown error"}`, "error");
      }
    },
    [addToast]
  );

  const { documents, error, refresh } = usePolling(handleTransition);
  const selected = documents.find((d) => d.id === selectedId) ?? null;

  return (
    <div className="app">
      <header className="app-header">
        <h1>Document Intelligence Platform</h1>
      </header>

      <Toasts toasts={toasts} onDismiss={dismissToast} />

      <main className="app-main">
        <section className="app-sidebar">
          <UploadPanel onUploaded={refresh} />
          {error ? <p className="error-text">{error}</p> : null}
          <h2>Documents</h2>
          <DocumentList documents={documents} selectedId={selectedId} onSelect={(doc) => setSelectedId(doc.id)} />
        </section>

        <section className="app-detail">
          {selected ? (
            <ReportView doc={selected} />
          ) : (
            <p className="empty-state">Select a document to see its report.</p>
          )}
        </section>
      </main>
    </div>
  );
}
