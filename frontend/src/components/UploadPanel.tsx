import { useRef, useState } from "react";
import type { ChangeEvent } from "react";
import { ApiError, completeUpload, createDocumentSlot, uploadFileToS3 } from "../api";

type Props = {
  // Called once the slot is confirmed, so the caller can refresh the list
  // immediately instead of waiting for the next scheduled poll.
  onUploaded: () => void;
};

type UploadState =
  | { phase: "idle" }
  | { phase: "uploading"; filename: string; percent: number }
  | { phase: "confirming"; filename: string }
  | { phase: "error"; message: string };

export function UploadPanel({ onUploaded }: Props): JSX.Element {
  const [state, setState] = useState<UploadState>({ phase: "idle" });
  const inputRef = useRef<HTMLInputElement | null>(null);

  const handleFile = async (file: File): Promise<void> => {
    setState({ phase: "uploading", filename: file.name, percent: 0 });
    try {
      // 1. Ask the API for an upload slot.
      const slot = await createDocumentSlot({
        filename: file.name,
        content_type: file.type || "application/octet-stream",
        size_bytes: file.size,
      });

      // 2. Upload straight to S3 using the presigned POST, not through the API.
      await uploadFileToS3(slot.upload_url, slot.fields, file, (percent) => {
        setState({ phase: "uploading", filename: file.name, percent });
      });

      // 3. Tell the API the upload finished, so the UI can move to QUEUED
      // right away instead of waiting on the S3 event.
      setState({ phase: "confirming", filename: file.name });
      await completeUpload(slot.document_id);

      setState({ phase: "idle" });
      onUploaded();
    } catch (e) {
      const message = e instanceof ApiError ? e.message : "Upload failed. Please try again.";
      setState({ phase: "error", message });
    } finally {
      if (inputRef.current) {
        inputRef.current.value = "";
      }
    }
  };

  const handleChange = (event: ChangeEvent<HTMLInputElement>): void => {
    const file = event.target.files?.[0];
    if (file) {
      void handleFile(file);
    }
  };

  const busy = state.phase === "uploading" || state.phase === "confirming";

  return (
    <div className="upload-panel">
      <label className="upload-label">
        Upload a document
        <input ref={inputRef} type="file" onChange={handleChange} disabled={busy} />
      </label>

      {state.phase === "uploading" ? (
        <div className="upload-progress" role="progressbar" aria-valuenow={state.percent}>
          <div className="upload-progress-bar" style={{ width: `${state.percent}%` }} />
          <span className="upload-progress-label">
            {state.filename}: {state.percent}%
          </span>
        </div>
      ) : null}

      {state.phase === "confirming" ? <p>Confirming upload of {state.filename}...</p> : null}

      {state.phase === "error" ? <p className="error-text">{state.message}</p> : null}
    </div>
  );
}
