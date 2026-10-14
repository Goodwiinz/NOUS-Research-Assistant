'use client';

import type { ReactElement } from 'react';
import { useQueryClient } from '@tanstack/react-query';
import {
  sharedArtifactKey,
  useArtifactShareScope,
  useSharedArtifact,
  type SharedArtifactRead,
} from '@/hooks/chat/useArtifactSharing';
import { artifactService } from '@/services/artifactService';
import { ArtifactBlobPreview, formatBytes } from './ArtifactPreview';

export function SharedArtifactView({
  shareId,
}: {
  shareId: string;
}): ReactElement {
  const scope = useArtifactShareScope();
  return (
    <ScopedSharedArtifactView
      key={`${scope}:${shareId}`}
      scope={scope}
      shareId={shareId}
    />
  );
}

function ScopedSharedArtifactView({
  scope,
  shareId,
}: {
  scope: string | null;
  shareId: string;
}): ReactElement {
  const query = useSharedArtifact(scope, shareId);
  const client = useQueryClient();
  if (!scope) return <p role="status">Sign in to view this shared file.</p>;
  // Unmount before rechecking access to release prepared text, object URLs,
  // sandbox documents and PDF workers immediately.
  if (query.isFetching || query.isPending)
    return <p role="status">Checking shared file access…</p>;
  if (query.isError || query.data?.status !== 'ready')
    return (
      <div role="alert" className="p-6">
        <p>
          Shared file is unavailable. It may have expired, been revoked, or be
          outside your audience.
        </p>
        <button
          type="button"
          onClick={() => void query.refetch()}
          className="mt-3 rounded-md border px-3 py-2 text-sm"
        >
          Retry
        </button>
      </div>
    );
  const { version, blob } = query.data;
  return (
    <main className="mx-auto max-w-5xl p-6">
      <div className="mb-4 flex flex-wrap items-center gap-3">
        <div className="min-w-0 flex-1">
          <h1 className="break-words text-xl font-semibold">{version.title}</h1>
          <p className="text-sm text-muted-foreground">
            Shared version · {version.mimeType} ·{' '}
            {formatBytes(version.byteSize)}
          </p>
        </div>
        <button
          type="button"
          onClick={() => void query.refetch()}
          className="rounded-md border px-3 py-2 text-sm"
        >
          Refresh shared version
        </button>
      </div>
      <ArtifactBlobPreview
        identity={`${scope}:${shareId}:${query.dataUpdatedAt}`}
        version={version}
        blob={blob}
        onRetry={() => void query.refetch()}
        onDownload={async () => {
          try {
            await artifactService.downloadSharedArtifact(
              shareId,
              version.title
            );
          } catch (error) {
            client.setQueryData<SharedArtifactRead>(
              sharedArtifactKey(scope, shareId),
              { status: 'unavailable' }
            );
            throw error;
          }
        }}
      />
    </main>
  );
}
