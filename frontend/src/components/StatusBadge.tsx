import type { DocumentStatus } from "../types";

const LABELS: Record<DocumentStatus, string> = {
  UPLOADING: "Uploading",
  QUEUED: "Queued",
  PROCESSING: "Processing",
  COMPLETED: "Completed",
  FAILED: "Failed",
  EXPIRED: "Expired",
};

export function StatusBadge({ status }: { status: DocumentStatus }): JSX.Element {
  return <span className={`status-badge status-${status.toLowerCase()}`}>{LABELS[status]}</span>;
}
