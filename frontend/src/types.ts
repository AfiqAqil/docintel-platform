// Mirrors frontend/API.md, which is generated from backend/app/schemas.py.
// Keep this in sync with that file, not with the Python.

export type DocumentStatus =
  | "UPLOADING"
  | "QUEUED"
  | "PROCESSING"
  | "COMPLETED"
  | "FAILED"
  | "EXPIRED";

export type DocumentOutcome = "COMPLETE" | "INCOMPLETE" | "UNSUPPORTED";

export type ReportSummary = {
  summary: string | null;
  missing_information: string[];
  validation_errors: string[];
  observations: string[];
};

export type DocumentOut = {
  id: string;
  filename: string;
  content_type: string;
  size_bytes: number;
  status: DocumentStatus;
  outcome: DocumentOutcome | null;
  doc_type: string | null;
  current_step: string | null; // the graph node currently running
  report_summary: ReportSummary | null;
  error_message: string | null;
  attempt_count: number;
  created_at: string; // ISO 8601
  updated_at: string;
  uploaded_at: string | null;
  completed_at: string | null;
};

export type DocumentListResponse = {
  items: DocumentOut[];
  limit: number;
  offset: number;
};

export type CreateDocumentRequest = {
  filename: string;
  content_type: string;
  size_bytes: number;
};

export type CreateDocumentResponse = {
  document_id: string;
  upload_url: string;
  fields: Record<string, string>;
};

// verified: true = snippet found in source, false = snippet not found and the
// field was rejected, null = image only input, nothing to verify against.
export type ExtractedField = {
  value: string;
  snippet: string;
  verified: boolean | null;
};

export type TraceStep = {
  node: string;
  status: "ok" | "error";
  duration_ms: number;
  detail: string | null;
};

export type Report = {
  document_id: string;
  generated_at: string;
  document_type: string;
  classification: { confidence: number; notes: string };
  outcome: DocumentOutcome;
  summary: string;
  // Most fields are one cell. A repeating group, such as an invoice's line items, is a list
  // of rows where every cell is itself a field with its own evidence snippet.
  extracted: Record<string, ExtractedField | Record<string, ExtractedField>[]>;
  missing_information: string[];
  validation_errors: string[];
  observations: string[];
  trace: TraceStep[];
};
