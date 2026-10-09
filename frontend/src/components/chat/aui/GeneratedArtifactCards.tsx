'use client';

import { useMemo, type ReactElement } from 'react';

import type { ChatPageMessage } from '@/components/chat/shared/cloudMessageView';
import { useThreadArtifacts } from '@/hooks/chat/useThreadArtifacts';
import type { ThreadArtifact } from '@/services/artifactService';
import { useArtifactPanelStore } from '@/store/artifactPanelStore';
import { useArtifactScope } from '@/hooks/chat/useArtifactScope';
import { useChatStore } from '@/store/chat-store';

import { formatBytes } from '../artifact-panel/ArtifactPreview';

/**
 * Generated-file cards under an assistant message. A card appears only for a
 * persisted version whose durable reference resolves to this message; files
 * whose message has not landed yet ("thread outputs") attach to the latest
 * assistant message so late publications stay discoverable without a stream.
 */
export function GeneratedArtifactCards({
  message,
  isLatestAssistant = false,
}: {
  message: Pick<ChatPageMessage, 'id'> | undefined;
  /** Computed by the row wrapper from the runtime thread, so a failed run's
   * id-less error row can still anchor thread outputs. */
  isLatestAssistant?: boolean;
}): ReactElement | null {
  const messageId = message?.id;
  const threadId = useChatStore((s) => s.currentThreadId);
  // Select the stable list reference; derive with useMemo so the selector
  // never returns a fresh array (which would re-render without end).
  const messages = useChatStore((s) =>
    s.currentThreadId ? s.messages[s.currentThreadId] : undefined
  );
  const loadedIds = useMemo(
    () =>
      (messages ?? [])
        .map((m) => m.id)
        .filter((id): id is string => Boolean(id)),
    [messages]
  );
  const isLatestAssistantMessage = message !== undefined && isLatestAssistant;
  const { data, isError, refetch } = useThreadArtifacts(threadId);
  const openArtifact = useArtifactPanelStore((s) => s.openArtifact);
  const scope = useArtifactScope();
  if (!message) return null;
  if (isError) {
    // One stable notice on the latest turn only; a silent blank would read
    // as "no files" and hide the user's outputs.
    if (!isLatestAssistantMessage) return null;
    return (
      <div role="alert" className="mt-2 text-sm">
        <span>Could not load generated files.</span>{' '}
        <button
          type="button"
          className="rounded-md border px-2 py-1 text-xs"
          onClick={() => void refetch()}
        >
          Retry
        </button>
      </div>
    );
  }
  if (!data || data.length === 0) return null;
  const own = messageId
    ? data.filter((a) => a.reference.messageId === messageId)
    : [];
  // No message yet, or a message this view has not loaded (older history,
  // failed run): keep the file discoverable under the latest assistant turn.
  const orphans = isLatestAssistantMessage
    ? data.filter(
        (a) =>
          a.reference.messageId === null ||
          !loadedIds.includes(a.reference.messageId)
      )
    : [];
  const items = [...own, ...orphans];
  if (items.length === 0) return null;
  const open = (item: ThreadArtifact): void =>
    openArtifact(
      {
        kind: 'generated',
        artifactId: item.version.artifactId,
        versionId: item.version.versionId,
        title: item.version.title,
      },
      { source: 'user', scope }
    );
  return (
    <section aria-label="Generated files" className="mt-2 flex flex-wrap gap-2">
      {items.map((item) => (
        <button
          key={item.version.versionId}
          data-artifact-version={item.version.versionId}
          type="button"
          onClick={() => open(item)}
          className="flex max-w-full items-center gap-2 rounded-md border px-3 py-2 text-left text-sm hover:bg-muted"
        >
          <span className="sr-only">Open </span>
          <span className="truncate font-medium">{item.version.title}</span>
          <span className="shrink-0 text-xs text-muted-foreground">
            {item.version.mimeType} · {formatBytes(item.version.byteSize)}
            {own.includes(item) ? '' : ' · thread output'}
          </span>
        </button>
      ))}
    </section>
  );
}
