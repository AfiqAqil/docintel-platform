export type ToastKind = "success" | "error";

export type Toast = {
  id: string;
  message: string;
  kind: ToastKind;
};

type Props = {
  toasts: Toast[];
  onDismiss: (id: string) => void;
};

// Fires on a transition into COMPLETED or FAILED (see usePolling.ts). Purely
// presentational: App owns the list and auto-dismiss timing.
export function Toasts({ toasts, onDismiss }: Props): JSX.Element {
  return (
    <div className="toast-stack">
      {toasts.map((toast) => (
        <div key={toast.id} className={`toast toast-${toast.kind}`}>
          <span>{toast.message}</span>
          <button type="button" className="toast-dismiss" onClick={() => onDismiss(toast.id)} aria-label="Dismiss">
            x
          </button>
        </div>
      ))}
    </div>
  );
}
