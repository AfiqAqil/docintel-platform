import type {
  CreateDocumentRequest,
  CreateDocumentResponse,
  DocumentListResponse,
  Report,
} from "./types";

export class ApiError extends Error {
  status: number;

  constructor(message: string, status: number) {
    super(message);
    this.name = "ApiError";
    this.status = status;
  }
}

// FastAPI's default error body is {"detail": "..."}. When the backend names
// the actual size limit or accepted types in that detail, surface it as-is
// instead of a generic message; fall back only if the body isn't shaped that way.
async function readErrorDetail(res: Response): Promise<string | null> {
  try {
    const body: unknown = await res.json();
    if (body && typeof body === "object" && typeof (body as { detail?: unknown }).detail === "string") {
      return (body as { detail: string }).detail;
    }
    return null;
  } catch {
    return null;
  }
}

export async function createDocumentSlot(
  req: CreateDocumentRequest
): Promise<CreateDocumentResponse> {
  const res = await fetch("/api/documents", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(req),
  });

  if (res.status === 413) {
    throw new ApiError((await readErrorDetail(res)) ?? "File is larger than the platform's upload limit.", 413);
  }
  if (res.status === 415) {
    throw new ApiError((await readErrorDetail(res)) ?? "That file type is not accepted by the platform.", 415);
  }
  if (!res.ok) {
    throw new ApiError((await readErrorDetail(res)) ?? `Could not start the upload (status ${res.status}).`, res.status);
  }

  return res.json() as Promise<CreateDocumentResponse>;
}

// Step 2 of the upload: posts straight to S3, never through the API.
// S3 requires every entry of `fields` to appear in the form before `file`,
// or the file field is rejected - this is the single most common way this breaks.
export function uploadFileToS3(
  uploadUrl: string,
  fields: Record<string, string>,
  file: File,
  onProgress: (percent: number) => void
): Promise<void> {
  return new Promise((resolve, reject) => {
    const form = new FormData();
    for (const [key, value] of Object.entries(fields)) {
      form.append(key, value);
    }
    form.append("file", file);

    const xhr = new XMLHttpRequest();
    xhr.open("POST", uploadUrl);
    xhr.upload.onprogress = (event) => {
      if (event.lengthComputable) {
        onProgress(Math.round((event.loaded / event.total) * 100));
      }
    };
    xhr.onload = () => {
      if (xhr.status >= 200 && xhr.status < 300) {
        resolve();
      } else {
        reject(new Error(`Upload to storage failed (status ${xhr.status}).`));
      }
    };
    xhr.onerror = () => reject(new Error("Upload to storage failed."));
    xhr.send(form);
  });
}

export async function completeUpload(documentId: string): Promise<void> {
  const res = await fetch(`/api/documents/${documentId}/upload-complete`, { method: "POST" });
  if (!res.ok) {
    const detail = await readErrorDetail(res);
    if (res.status === 409) {
      throw new ApiError(detail ?? "The uploaded object was not found in storage yet.", 409);
    }
    throw new ApiError(detail ?? `Could not confirm the upload (status ${res.status}).`, res.status);
  }
}

export async function listDocuments(): Promise<DocumentListResponse> {
  const res = await fetch("/api/documents");
  if (!res.ok) {
    throw new ApiError(`Could not load documents (status ${res.status}).`, res.status);
  }
  return res.json() as Promise<DocumentListResponse>;
}

export async function getReport(documentId: string): Promise<Report> {
  const res = await fetch(`/api/documents/${documentId}/report`);
  if (res.status === 409) {
    throw new ApiError("The report is not ready yet.", 409);
  }
  if (res.status === 404) {
    throw new ApiError("No report was found for this document.", 404);
  }
  if (!res.ok) {
    throw new ApiError(`Could not load the report (status ${res.status}).`, res.status);
  }
  return res.json() as Promise<Report>;
}
