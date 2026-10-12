'use client';

import Link from 'next/link';
import { useState } from 'react';
import type { ReactElement } from 'react';

import { getSelectedThreadUrl } from '@/components/chat/shared/chatNavigation';
import { Button } from '@/components/ui/button';
import { useProjectArtifacts } from '@/hooks/chat/useThreadArtifacts';
import {
  artifactService,
  type ProjectArtifact,
} from '@/services/artifactService';

async function downloadArtifact(item: ProjectArtifact): Promise<void> {
  const blob = await artifactService.fetchVersionBlob(item.version.versionId);
  const url = URL.createObjectURL(blob);
  const anchor = document.createElement('a');
  anchor.href = url;
  anchor.download = item.version.title;
  document.body.appendChild(anchor);
  anchor.click();
  document.body.removeChild(anchor);
  URL.revokeObjectURL(url);
}

function ArtifactRow({ item }: { item: ProjectArtifact }): ReactElement {
  const [busy, setBusy] = useState(false);
  const [failed, setFailed] = useState(false);
  const onDownload = async (): Promise<void> => {
    setBusy(true);
    setFailed(false);
    try {
      await downloadArtifact(item);
    } catch {
      setFailed(true);
    } finally {
      setBusy(false);
    }
  };
  return (
    <li className="flex flex-wrap items-center justify-between gap-3 rounded-lg border border-border p-3">
      <div className="min-w-0">
        <p className="truncate text-sm font-medium text-foreground">
          {item.title}
        </p>
        <p className="text-xs text-muted-foreground">
          {item.kind} · {item.version.mimeType} ·{' '}
          {new Date(item.version.createdAt).toLocaleString()} ·{' '}
          <code>{item.version.sha256.slice(0, 12)}</code>
        </p>
        {failed && (
          <p role="alert" className="text-xs text-destructive">
            Download failed. Try again.
          </p>
        )}
      </div>
      <div className="flex items-center gap-2">
        {item.threadId && (
          <Button asChild variant="ghost" size="sm">
            <Link href={getSelectedThreadUrl(item.threadId)}>Open in chat</Link>
          </Button>
        )}
        <Button
          variant="outline"
          size="sm"
          disabled={busy}
          onClick={() => void onDownload()}
        >
          Download
        </Button>
      </div>
    </li>
  );
}

/**
 * One row per artifact. A publication between two page loads shifts the
 * offsets, so the row on the page boundary arrives twice; keep its first copy.
 * (A row republished above the loaded window is missed until a refetch.)
 */
function uniqueArtifacts(
  pages: ProjectArtifact[][] | undefined
): ProjectArtifact[] {
  const seen = new Set<string>();
  return (pages ?? []).flat().filter((item) => {
    if (seen.has(item.artifactId)) return false;
    seen.add(item.artifactId);
    return true;
  });
}

export function ProjectArtifactsTab({
  projectId,
}: {
  projectId: string;
}): ReactElement {
  const {
    data,
    isLoading,
    isError,
    hasNextPage,
    isFetchingNextPage,
    isFetchNextPageError,
    fetchNextPage,
  } = useProjectArtifacts(projectId);
  const items = uniqueArtifacts(data?.pages);
  if (isLoading) {
    return (
      <div
        aria-busy="true"
        aria-label="Loading project files"
        className="h-48 animate-pulse rounded-lg bg-muted"
      />
    );
  }
  // A failed later page keeps the files already shown; the button retries.
  if (isError && !items.length) {
    return (
      <div
        role="alert"
        className="rounded-lg border border-border p-4 text-sm text-muted-foreground"
      >
        Files could not be loaded.
      </div>
    );
  }
  if (!items.length) {
    return (
      <p className="rounded-lg border border-border p-4 text-sm text-muted-foreground">
        No files yet. Files produced in this project&apos;s chats appear here.
      </p>
    );
  }
  return (
    <div className="space-y-3">
      <ul className="space-y-2">
        {items.map((item) => (
          <ArtifactRow key={item.artifactId} item={item} />
        ))}
      </ul>
      {hasNextPage && (
        <div className="flex flex-wrap items-center gap-3">
          <Button
            variant="outline"
            size="sm"
            disabled={isFetchingNextPage}
            onClick={() => void fetchNextPage()}
          >
            {isFetchingNextPage ? 'Loading more files…' : 'Load more files'}
          </Button>
          {isFetchNextPageError && (
            <p role="alert" className="text-xs text-destructive">
              Could not load more files. Try again.
            </p>
          )}
        </div>
      )}
    </div>
  );
}
