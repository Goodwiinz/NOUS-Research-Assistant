'use client';

import React, { useEffect, useState, useCallback } from 'react';
import {
  AlertTriangle,
  Download,
  ExternalLink,
  FileQuestion,
  RefreshCw,
} from 'lucide-react';
import { api } from '@/services/api-client';
import { useAuthStore } from '@/stores/authStore';
import { PdfDocumentViewer } from './PdfDocumentViewer';
import type { PdfAskHandler } from '@/types/pdf-chat';

interface DocumentInlineViewerProps {
  documentId: string;
  filename: string;
  /** Frontend `file_type` or backend `document_type`, whichever is present. */
  kind?: string;
  mimeType?: string;
  /** One-based physical page requested by a citation; no highlight is inferred. */
  targetPage?: number;
  onAskAboutPdf?: PdfAskHandler;
}

type ViewerKind = 'pdf' | 'image' | 'audio' | 'video' | 'unsupported';

function resolveViewerKind(kind?: string, mimeType?: string): ViewerKind {
  const k = (kind || '').toLowerCase();
  const m = (mimeType || '').toLowerCase();
  if (k === 'pdf' || m === 'application/pdf') return 'pdf';
  if (k === 'image' || k === 'jpg' || k === 'png' || m.startsWith('image/'))
    return 'image';
  if (k === 'audio' || k === 'mp3' || m.startsWith('audio/')) return 'audio';
  if (k === 'video' || k === 'mp4' || m.startsWith('video/')) return 'video';
  return 'unsupported';
}

interface LoadState {
  /** Which (document, attempt) this state belongs to — stale async results
   * for any other key are discarded, so switching documents mid-download
   * can never show the wrong file under the new title. */
  key: string;
  status: 'loading' | 'ready' | 'error';
  objectUrl?: string;
  blob?: Blob;
  message?: string;
}

/**
 * Tenant-scoped inline media viewer (PDF / image / audio / video) over the
 * document download endpoint. Extracted from DocumentPreviewTab so the chat
 * artifact panel and the document detail page share one viewer; hosts supply
 * their own outer spacing.
 */
export function DocumentInlineViewer({
  documentId,
  filename,
  kind,
  mimeType,
  targetPage,
  onAskAboutPdf,
}: DocumentInlineViewerProps): React.ReactElement {
  const viewerKind = resolveViewerKind(kind, mimeType);
  const user = useAuthStore((state) => state.user);
  const authenticated = useAuthStore((state) => state.isAuthenticated);
  const [attempt, setAttempt] = useState(0);
  const requestKey = JSON.stringify([
    user?.id,
    user?.organization_id,
    authenticated,
    documentId,
    attempt,
  ]);

  // "Adjust state when a prop changes" pattern (not an effect): a document
  // switch resets to loading during render, so no synchronous setState in an
  // effect body and no flash of the previous document's state.
  const [load, setLoad] = useState<LoadState>({
    key: requestKey,
    status: 'loading',
  });
  if (load.key !== requestKey) {
    setLoad({ key: requestKey, status: 'loading' });
  }

  useEffect(() => {
    // Inline rendering is only meaningful for these media types; for anything
    // else we offer download instead of fetching bytes we can't display.
    if (viewerKind === 'unsupported') return;

    let cancelled = false;
    let createdUrl: string | null = null;
    const controller = new AbortController();

    api
      .fetchBlob(`/files/${documentId}/download`, controller.signal)
      .then((blob) => {
        if (cancelled) return;
        createdUrl = URL.createObjectURL(blob);
        setLoad({
          key: requestKey,
          status: 'ready',
          objectUrl: createdUrl,
          blob,
        });
      })
      .catch(() => {
        if (cancelled) return;
        setLoad({
          key: requestKey,
          status: 'error',
          message: 'Could not load this file for preview.',
        });
      });

    // Cancels the in-flight request's effects on document switch, retry, or
    // unmount, and releases the blob URL this effect instance created.
    return () => {
      cancelled = true;
      controller.abort();
      if (createdUrl) window.URL.revokeObjectURL(createdUrl);
    };
  }, [documentId, viewerKind, requestKey]);

  const handleDownload = useCallback(() => {
    api
      .download(`/files/${documentId}/download`, filename)
      .catch((err) => console.error('Download failed:', err));
  }, [documentId, filename]);

  // Unsupported media: honest affordance instead of a broken viewer.
  if (viewerKind === 'unsupported') {
    return (
      <div className="flex min-h-[400px] flex-col items-center justify-center rounded-xl border border-dashed border-border bg-card/50 px-6 text-center">
        <FileQuestion
          aria-hidden="true"
          className="mb-4 h-10 w-10 text-muted-foreground"
        />
        <p className="mb-1 text-sm font-medium text-foreground">
          Inline preview isn&apos;t available for this file type
        </p>
        <p className="mb-5 max-w-sm text-xs text-muted-foreground">
          Download the original to open it in an application that supports it.
        </p>
        <button
          type="button"
          onClick={handleDownload}
          className="inline-flex items-center gap-2 rounded-lg bg-primary px-4 py-2 text-sm font-medium text-primary-foreground transition-colors hover:bg-(--nous-helios) focus-visible:outline-hidden focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2"
        >
          <Download aria-hidden="true" className="h-4 w-4" />
          Download original
        </button>
      </div>
    );
  }

  if (load.status === 'loading') {
    return (
      <div>
        <div className="h-[70vh] min-h-[400px] animate-pulse rounded-xl border border-border bg-card" />
        <span className="sr-only" role="status">
          Loading preview…
        </span>
      </div>
    );
  }

  if (load.status === 'error' || !load.objectUrl) {
    return (
      <div
        role="alert"
        className="flex min-h-[400px] flex-col items-center justify-center rounded-xl border border-destructive/30 bg-destructive/5 px-6 text-center"
      >
        <AlertTriangle
          aria-hidden="true"
          className="mb-4 h-10 w-10 text-destructive"
        />
        <p className="mb-1 text-sm font-medium text-foreground">
          Couldn&apos;t load the preview
        </p>
        <p className="mb-5 max-w-sm text-xs text-muted-foreground">
          {load.message || 'The file may still be uploading or is unavailable.'}
        </p>
        <div className="flex items-center gap-3">
          <button
            type="button"
            onClick={() => setAttempt((a) => a + 1)}
            className="inline-flex items-center gap-2 rounded-lg border border-border bg-card px-4 py-2 text-sm font-medium text-foreground transition-colors hover:bg-muted focus-visible:outline-hidden focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2"
          >
            <RefreshCw aria-hidden="true" className="h-4 w-4" />
            Retry
          </button>
          <button
            type="button"
            onClick={handleDownload}
            className="inline-flex items-center gap-2 rounded-lg bg-primary px-4 py-2 text-sm font-medium text-primary-foreground transition-colors hover:bg-(--nous-helios) focus-visible:outline-hidden focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2"
          >
            <Download aria-hidden="true" className="h-4 w-4" />
            Download instead
          </button>
        </div>
      </div>
    );
  }

  const objectUrl = load.objectUrl;
  const pdfUrl =
    Number.isSafeInteger(targetPage) && (targetPage ?? 0) > 0
      ? `${objectUrl}#page=${targetPage}`
      : objectUrl;

  return (
    <div className="overflow-hidden rounded-xl border border-border bg-card shadow-xs">
      {/* Viewer toolbar */}
      <div className="flex items-center justify-between gap-3 border-b border-border bg-muted/30 px-4 py-2.5">
        <p
          className="truncate text-xs font-medium text-muted-foreground"
          title={filename}
        >
          {filename}
        </p>
        <div className="flex shrink-0 items-center gap-2">
          <a
            href={viewerKind === 'pdf' ? pdfUrl : objectUrl}
            target="_blank"
            rel="noopener noreferrer"
            className="inline-flex items-center gap-1.5 rounded-lg border border-border bg-card px-2.5 py-1.5 text-xs font-medium text-muted-foreground transition-colors hover:bg-muted hover:text-foreground focus-visible:outline-hidden focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2"
          >
            <ExternalLink aria-hidden="true" className="h-3.5 w-3.5" />
            Open in new tab
          </a>
          <button
            type="button"
            onClick={handleDownload}
            className="inline-flex items-center gap-1.5 rounded-lg border border-border bg-card px-2.5 py-1.5 text-xs font-medium text-muted-foreground transition-colors hover:bg-muted hover:text-foreground focus-visible:outline-hidden focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2"
          >
            <Download aria-hidden="true" className="h-3.5 w-3.5" />
            Download
          </button>
        </div>
      </div>

      {/* Viewer surface */}
      {viewerKind === 'pdf' && load.blob && (
        <PdfDocumentViewer
          key={requestKey}
          blob={load.blob}
          title={filename}
          targetPage={targetPage}
          onAsk={onAskAboutPdf}
        />
      )}

      {viewerKind === 'image' && (
        <div className="flex max-h-[70vh] min-h-[400px] items-center justify-center bg-muted/20 p-4">
          {/* eslint-disable-next-line @next/next/no-img-element */}
          <img
            src={objectUrl}
            alt={`Preview of ${filename}`}
            className="max-h-full max-w-full object-contain"
          />
        </div>
      )}

      {viewerKind === 'audio' && (
        <div className="flex min-h-[200px] items-center justify-center bg-muted/20 p-8">
          <audio controls src={objectUrl} className="w-full max-w-xl">
            Your browser does not support the audio element.
          </audio>
        </div>
      )}

      {viewerKind === 'video' && (
        <div className="flex max-h-[70vh] items-center justify-center bg-muted/20 p-4">
          <video
            controls
            src={objectUrl}
            className="max-h-[66vh] w-full rounded-lg"
          >
            Your browser does not support the video element.
          </video>
        </div>
      )}
    </div>
  );
}
