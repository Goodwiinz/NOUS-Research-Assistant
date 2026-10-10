'use client';

import React, { useEffect, useState, type ReactElement } from 'react';

import { ChatMarkdown } from '@/components/chat/ChatMarkdown';
import {
  useArtifactCapabilities,
  useArtifactContent,
} from '@/hooks/chat/useThreadArtifacts';
import { useArtifactScope } from '@/hooks/chat/useArtifactScope';
import {
  artifactService,
  type ArtifactVersion,
} from '@/services/artifactService';

import { InteractiveHtmlPreview } from './InteractiveHtmlPreview';
import {
  PdfArtifactPreview,
  MAX_PDF_PREVIEW_BYTES,
} from './PdfArtifactPreview';

import {
  CsvArtifactPreview,
  JsonArtifactPreview,
} from './StaticArtifactPreviews';

/** Text previews above this size fall back to download to keep the DOM bounded. */
export const MAX_TEXT_PREVIEW_BYTES = 2 * 1024 * 1024;

export const MAX_BINARY_PREVIEW_BYTES = 20 * 1024 * 1024;
type PreviewKind =
  'markdown' | 'text' | 'image' | 'pdf' | 'csv' | 'json' | 'html' | 'download';

/**
 * Published Markdown is untrusted: an image reference would make the viewer's
 * browser fetch an attacker-chosen URL. Render the alt text instead.
 */
const NO_REMOTE_IMAGES: React.ComponentProps<
  typeof ChatMarkdown
>['components'] = {
  img: ({ alt }) => <span>{alt ? `[image: ${alt}]` : '[image]'}</span>,
};

export function previewKindFor(version: ArtifactVersion): PreviewKind {
  const mime = version.mimeType.toLowerCase().split(';')[0].trim();
  if (mime === 'application/pdf')
    return version.byteSize <= MAX_PDF_PREVIEW_BYTES ? 'pdf' : 'download';
  if (mime === 'image/png' || mime === 'image/jpeg') {
    if (version.byteSize > MAX_BINARY_PREVIEW_BYTES) return 'download';
    return 'image';
  }
  if (version.byteSize > MAX_TEXT_PREVIEW_BYTES) return 'download';
  if (mime === 'text/markdown') return 'markdown';
  if (mime === 'text/csv') return 'csv';
  if (mime === 'text/html') return 'html';
  if (mime === 'application/json') return 'json';
  return mime.startsWith('text/') ||
    ['application/javascript', 'application/xml', 'application/yaml'].includes(
      mime
    )
    ? 'text'
    : 'download';
}

type LoadState =
  | { key: string; status: 'error' }
  | { key: string; status: 'too-large' }
  | { key: string; status: 'text'; text: string }
  | { key: string; status: 'image'; objectUrl: string }
  | { key: string; status: 'pdf'; blob: Blob };

/**
 * Read-only preview of one committed version. Bytes are fetched with auth;
 * Markdown renders through ChatMarkdown (no raw HTML), everything else is
 * escaped text or an image. HTML execution requires an explicit sandbox opt-in.
 */
export function ArtifactPreview({
  version,
}: {
  version: ArtifactVersion;
}): ReactElement {
  const kind = previewKindFor(version);
  const scope = useArtifactScope();
  const content = useArtifactContent(version.versionId, kind !== 'download');
  const [attempt, setAttempt] = useState(0);
  // One load per (version, attempt); a stale `loaded` for another key renders
  // as loading, so no synchronous setState is needed when the key changes.
  const key = `${scope}:${version.versionId}:${attempt}`;
  const [loaded, setLoaded] = useState<LoadState | null>(null);
  const state: LoadState | { status: 'loading' } = content.isError
    ? { key, status: 'error' }
    : loaded && loaded.key === key
      ? loaded
      : { status: 'loading' };

  useEffect(() => {
    if (kind === 'download') return;
    let cancelled = false;
    let objectUrl: string | null = null;
    const blob = content.data;
    if (!blob) return;
    const prepare = async (): Promise<void> => {
      if (
        blob.size >
        (kind === 'image'
          ? MAX_BINARY_PREVIEW_BYTES
          : kind === 'pdf'
            ? MAX_PDF_PREVIEW_BYTES
            : MAX_TEXT_PREVIEW_BYTES)
      ) {
        setLoaded({ key, status: 'too-large' });
      } else if (kind === 'pdf') {
        setLoaded({ key, status: 'pdf', blob });
      } else if (kind === 'image') {
        objectUrl = window.URL.createObjectURL(
          new Blob([blob], { type: version.mimeType })
        );
        setLoaded({ key, status: 'image', objectUrl });
      } else {
        const text = await blobToText(blob);
        if (!cancelled) setLoaded({ key, status: 'text', text });
      }
    };
    void prepare().catch(() => {
      if (!cancelled) setLoaded({ key, status: 'error' });
    });
    return () => {
      cancelled = true;
      if (objectUrl) window.URL.revokeObjectURL(objectUrl);
    };
  }, [kind, key, version.versionId, version.mimeType, content.data]);

  const [downloadFailed, setDownloadFailed] = useState(false);
  const download = (): void => {
    setDownloadFailed(false);
    artifactService
      .downloadVersion(version)
      .catch(() => setDownloadFailed(true));
  };

  if (kind === 'download') {
    return (
      <div className="p-4 text-sm text-muted-foreground">
        <p>
          No inline preview for {version.mimeType} (
          {formatBytes(version.byteSize)}).
        </p>
        <button
          type="button"
          className="mt-3 rounded-md border px-3 py-2 text-sm text-foreground"
          onClick={download}
        >
          Download {version.title}
        </button>
        {downloadFailed && (
          <p role="alert" className="mt-2">
            Download failed.
          </p>
        )}
      </div>
    );
  }
  if (state.status === 'too-large')
    return (
      <div className="p-4 text-sm">
        <p>File exceeds the inline preview limit.</p>
        <button type="button" onClick={download}>
          Download {version.title}
        </button>
        {downloadFailed && <p role="alert">Download failed.</p>}
      </div>
    );
  if (state.status === 'loading') {
    return (
      <div role="status" className="p-4 text-sm text-muted-foreground">
        Loading…
      </div>
    );
  }
  if (state.status === 'error') {
    return (
      <div role="alert" className="p-4 text-sm">
        <p>Could not load this file.</p>
        <button
          type="button"
          className="mt-3 rounded-md border px-3 py-2 text-sm"
          onClick={() => {
            setAttempt((n) => n + 1);
            void content.refetch();
          }}
        >
          Retry
        </button>
      </div>
    );
  }
  if (state.status === 'pdf')
    return (
      <div className="p-4">
        <PdfArtifactPreview key={key} blob={state.blob} title={version.title} />
        <button
          type="button"
          className="mt-3 rounded-md border px-3 py-2 text-sm"
          onClick={download}
        >
          Download {version.title}
        </button>
        {downloadFailed && <p role="alert">Download failed.</p>}
      </div>
    );
  if (state.status === 'image') {
    return (
      <div className="p-4">
        {/* eslint-disable-next-line @next/next/no-img-element -- authenticated blob: URL; next/image cannot optimize it */}
        <img
          src={state.objectUrl}
          alt={version.title}
          className="max-w-full rounded-md"
        />
      </div>
    );
  }
  if (kind === 'html')
    return <HtmlArtifactPreview key={key} text={state.text} />;
  if (kind === 'csv') return <CsvArtifactPreview text={state.text} />;
  if (kind === 'json') return <JsonArtifactPreview text={state.text} />;
  if (kind === 'markdown') {
    return (
      <div className="nous-prose p-4">
        <ChatMarkdown content={state.text} components={NO_REMOTE_IMAGES} />
      </div>
    );
  }
  return (
    <pre className="overflow-x-auto whitespace-pre-wrap break-words p-4 text-sm">
      {state.text}
    </pre>
  );
}

/** `Blob.text()` is missing in some DOM implementations (jsdom); FileReader is universal. */
export function blobToText(blob: Blob): Promise<string> {
  if (typeof blob.text === 'function') return blob.text();
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(String(reader.result ?? ''));
    reader.onerror = () => reject(reader.error ?? new Error('read failed'));
    reader.readAsText(blob);
  });
}

export function formatBytes(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

function HtmlArtifactPreview({ text }: { text: string }): ReactElement {
  const capabilities = useArtifactCapabilities();
  const [running, setRunning] = useState(false);
  return (
    <>
      {capabilities.data?.previewEnabled === true &&
        (running ? (
          <InteractiveHtmlPreview source={text} />
        ) : (
          <button
            type="button"
            className="m-3 rounded-md border px-3 py-2 text-sm"
            onClick={() => setRunning(true)}
          >
            Run HTML preview
          </button>
        ))}
      <pre className="overflow-x-auto whitespace-pre-wrap break-words p-4 text-sm">
        {text}
      </pre>
    </>
  );
}
