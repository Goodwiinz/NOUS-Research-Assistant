/**
 * Export service for thread/conversation export functionality.
 *
 * Handles API calls for:
 * - Single thread export (Markdown, PDF, JSON, HTML)
 * - Batch thread export with ZIP packaging
 * - Export format metadata
 */

// api-client's baseURL already ends in /api/v1 (see API_CONFIG.BASE_URL) —
// paths here are relative to it. Prefixing /api/v1 again yields
// /api/v1/api/v1/export/... and a 404.
import { api, type BinaryDownloadExpectation } from '@/services/api-client';

export type ExportFormat = 'markdown' | 'pdf' | 'json' | 'html';

export interface ExportOptions {
  includeSystemMessages?: boolean;
  includeCitations?: boolean;
  includeMetadata?: boolean;
  includeFeedback?: boolean;
}

export interface ExportFormatInfo {
  id: ExportFormat;
  name: string;
  extension: string;
  contentType: string;
  description: string;
}

export interface ExportPreview {
  threadId: string;
  title: string | null;
  format: ExportFormat;
  messageCount: number;
  citationCount: number;
  estimatedSizeBytes: number;
  exportable: boolean;
}

export interface BatchExportRequest {
  threadIds: string[];
  format: ExportFormat;
  options?: ExportOptions;
  asZip?: boolean;
}

const EXPORT_CONTENT_TYPES: Record<ExportFormat, string> = {
  markdown: 'text/markdown',
  pdf: 'application/pdf',
  json: 'application/json',
  html: 'text/html',
};

function exportDownloadExpectation(
  format: ExportFormat,
  asZip = false
): BinaryDownloadExpectation {
  if (asZip) {
    return { contentType: 'application/zip', signature: 'PK' };
  }

  return {
    contentType: EXPORT_CONTENT_TYPES[format],
    ...(format === 'pdf' ? { signature: '%PDF-' } : {}),
  };
}

/**
 * Export a single thread and trigger download.
 */
export async function exportThread(
  threadId: string,
  format: ExportFormat = 'markdown',
  options: ExportOptions = {}
): Promise<void> {
  const params = new URLSearchParams({
    format,
    include_system_messages: String(options.includeSystemMessages ?? false),
    include_citations: String(options.includeCitations ?? true),
    include_metadata: String(options.includeMetadata ?? true),
    include_feedback: String(options.includeFeedback ?? false),
  });

  // The backend export endpoint is POST (format/options are Query params even
  // on POST). downloadPost sends an authenticated POST and saves the blob;
  // api.download would issue a GET and 405.
  await api.downloadPost(
    `/export/thread/${threadId}?${params.toString()}`,
    undefined,
    undefined,
    exportDownloadExpectation(format)
  );
}

/**
 * Export multiple threads as a ZIP file.
 */
export async function exportBatch(request: BatchExportRequest): Promise<void> {
  const asZip = request.asZip ?? true;
  await api.downloadPost(
    '/export/batch',
    undefined,
    {
      thread_ids: request.threadIds,
      format: request.format,
      options: {
        include_system_messages:
          request.options?.includeSystemMessages ?? false,
        include_citations: request.options?.includeCitations ?? true,
        include_metadata: request.options?.includeMetadata ?? true,
        include_feedback: request.options?.includeFeedback ?? false,
      },
      as_zip: asZip,
    },
    exportDownloadExpectation(request.format, asZip)
  );
}

/**
 * Get available export formats.
 */
export async function getExportFormats(): Promise<{
  formats: ExportFormatInfo[];
  options: Record<string, string>;
  limits: { maxBatchSize: number; maxThreadMessages: number };
}> {
  const data = await api.get<{
    formats: ExportFormatInfo[];
    options: Record<string, string>;
    limits: { max_batch_size: number; max_thread_messages: number };
  }>('/export/formats');

  return {
    formats: data.formats,
    options: data.options,
    limits: {
      maxBatchSize: data.limits.max_batch_size,
      maxThreadMessages: data.limits.max_thread_messages,
    },
  };
}

/**
 * Preview export metadata before downloading.
 */
export async function previewExport(
  threadId: string,
  format: ExportFormat = 'markdown'
): Promise<ExportPreview> {
  const data = await api.post<{
    thread_id: string;
    title: string | null;
    format: string;
    message_count: number;
    citation_count: number;
    estimated_size_bytes: number;
    exportable: boolean;
  }>(`/export/preview/${threadId}?format=${format}`);

  return {
    threadId: data.thread_id,
    title: data.title,
    format: data.format as ExportFormat,
    messageCount: data.message_count,
    citationCount: data.citation_count,
    estimatedSizeBytes: data.estimated_size_bytes,
    exportable: data.exportable,
  };
}

/**
 * Format file size for display.
 */
export function formatFileSize(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}
