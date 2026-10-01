'use client';

import React, { useEffect, useState, type ReactElement } from 'react';

import { ChatMarkdown } from '@/components/chat/ChatMarkdown';
import {
  artifactService,
  type ArtifactVersion,
} from '@/services/artifactService';

/** Text previews above this size fall back to download to keep the DOM bounded. */
export const MAX_TEXT_PREVIEW_BYTES = 2 * 1024 * 1024;

type PreviewKind = 'markdown' | 'text' | 'image' | 'download';

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
  const mime = version.mimeType.toLowerCase();
  if (mime === 'image/png' || mime === 'image/jpeg') return 'image';
  const textual =
    mime === 'text/markdown' ||
    mime === 'text/plain' ||
    mime === 'text/csv' ||
    mime === 'text/x-python' ||
    mime === 'application/json' ||
    mime === 'application/javascript';
  if (!textual || version.byteSize > MAX_TEXT_PREVIEW_BYTES) return 'download';
  return mime === 'text/markdown' ? 'markdown' : 'text';
}

type LoadState =
  | { key: string; status: 'error' }
  | { key: string; status: 'text'; text: string }
  | { key: string; status: 'image'; objectUrl: string };

/**
 * Read-only preview of one committed version. Bytes are fetched with auth;
 * Markdown renders through ChatMarkdown (no raw HTML), everything else is
 * escaped text or an image. HTML is never executed here (Task 5 sandboxes it).
 */
export function ArtifactPreview({
  version,
}: {
  version: ArtifactVersion;
}): ReactElement {
  const kind = previewKindFor(version);
  const [attempt, setAttempt] = useState(0);
  // One load per (version, attempt); a stale `loaded` for another key renders
  // as loading, so no synchronous setState is needed when the key changes.
  const key = `${version.versionId}:${attempt}`;
  const [loaded, setLoaded] = useState<LoadState | null>(null);
  const state: LoadState | { status: 'loading' } =
    loaded && loaded.key === key ? loaded : { status: 'loading' };

  useEffect(() => {
    if (kind === 'download') return;
    let cancelled = false;
    let objectUrl: string | null = null;
    artifactService
      .fetchVersionBlob(version.versionId)
      .then(async (blob) => {
        if (cancelled) return;
        if (kind === 'image') {
          objectUrl = window.URL.createObjectURL(blob);
          setLoaded({ key, status: 'image', objectUrl });
        } else {
          const text = await blobToText(blob);
          if (!cancelled) setLoaded({ key, status: 'text', text });
        }
      })
      .catch(() => {
        if (!cancelled) setLoaded({ key, status: 'error' });
      });
    return () => {
      cancelled = true;
      if (objectUrl) window.URL.revokeObjectURL(objectUrl);
    };
  }, [kind, key, version.versionId]);

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
          onClick={() => setAttempt((n) => n + 1)}
        >
          Retry
        </button>
      </div>
    );
  }
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
