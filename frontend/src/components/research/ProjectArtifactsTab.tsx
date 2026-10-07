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

export function ProjectArtifactsTab({
  projectId,
}: {
  projectId: string;
}): ReactElement {
  const { data, isLoading, isError } = useProjectArtifacts(projectId);
  if (isLoading) {
    return (
      <div
        aria-busy="true"
        aria-label="Loading project files"
        className="h-48 animate-pulse rounded-lg bg-muted"
      />
    );
  }
  if (isError) {
    return (
      <div
        role="alert"
        className="rounded-lg border border-border p-4 text-sm text-muted-foreground"
      >
        Files could not be loaded.
      </div>
    );
  }
  if (!data?.length) {
    return (
      <p className="rounded-lg border border-border p-4 text-sm text-muted-foreground">
        No files yet. Files produced in this project&apos;s chats appear here.
      </p>
    );
  }
  return (
    <ul className="space-y-2">
      {data.map((item) => (
        <ArtifactRow key={item.artifactId} item={item} />
      ))}
    </ul>
  );
}
