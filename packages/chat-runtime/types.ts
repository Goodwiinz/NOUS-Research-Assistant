/** A single agent tool execution captured during a streaming turn. */
export interface ActivityStep {
  /** Invocation identity when the producer supplies one. */
  id?: string;
  tool: string;
  label: string;
  status: "running" | "done" | "error" | "cancelled";
  durationMs?: number;
  /** Compact one-line summary of the tool's arguments (e.g. the query). */
  argsSummary?: string;
  /** Structured (already backend-redacted) tool arguments, for declarative
   * per-tool renderers that want fields rather than the one-line summary. */
  args?: Record<string, unknown>;
  /** Compact one-line summary of the result, or the error text on failure. */
  resultSummary?: string;
  /** Structured, backend-redacted result for registered per-tool renderers. */
  result?: unknown;
}

export interface RuntimeAttachment {
  id: string;
  document_id: string;
  display_name?: string;
  thumbnail_url?: string;
  document_title?: string;
  document_type?: string;
  mime_type?: string;
}

/** Platform-independent message fields projected into assistant-ui. */
export interface RuntimeMessage {
  runtimeId: string;
  role: "user" | "assistant";
  content: string;
  timestamp: number;
  attachments?: RuntimeAttachment[];
  toolExecutions?: ActivityStep[];
  isStreaming?: boolean;
  pendingApproval?: {
    id: string;
    tools: Array<{ name: string; args: Record<string, unknown> }>;
  };
  metadata?: {
    stopped?: boolean;
    tokenUsage?: { input: number; output: number };
  };
  error?: { message: string; category?: string };
  timing?: { tokenCount?: number; tokensPerSecond?: number };
  feedback?: { rating: number | null; comment: string | null };
  reasoning?: string;
  activity?: string;
  contexts?: Record<string, unknown>[];
}
