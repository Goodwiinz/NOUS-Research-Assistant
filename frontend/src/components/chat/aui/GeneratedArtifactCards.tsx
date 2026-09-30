'use client';

import { useMemo, type ReactElement } from 'react';

import { useThreadArtifacts } from '@/hooks/chat/useThreadArtifacts';
import type { ThreadArtifact } from '@/services/artifactService';
import { useArtifactPanelStore } from '@/store/artifactPanelStore';
import { useChatStore } from '@/store/chat-store';

import { formatBytes } from '../artifact-panel/ArtifactPreview';

/**
 * Generated-file cards under an assistant message. A card appears only for a
 * persisted version whose durable reference resolves to this message; files
 * whose message has not landed yet ("thread outputs") attach to the latest
 * assistant message so late publications stay discoverable without a stream.
 */
export function GeneratedArtifactCards({
  messageId,
}: {
  messageId: string | undefined;
}): ReactElement | null {
  const threadId = useChatStore((s) => s.currentThreadId);
  // Select the stable list reference; derive with useMemo so the selector
  // never returns a fresh array (which would re-render without end).
  const messages = useChatStore((s) =>
    s.currentThreadId ? s.messages[s.currentThreadId] : undefined
  );
  const { loadedIds, latestAssistantId } = useMemo(() => {
    const list = messages ?? [];
    const ids = list.map((m) => m.id).filter((id): id is string => Boolean(id));
    let latest: string | null = null;
    for (let i = list.length - 1; i >= 0; i -= 1) {
      if (list[i]?.role === 'assistant') {
        latest = list[i]?.id ?? null;
        break;
      }
    }
    return { loadedIds: ids, latestAssistantId: latest };
  }, [messages]);
  const isLatestAssistantMessage =
    messageId !== undefined && messageId === latestAssistantId;
  const { data } = useThreadArtifacts(threadId);
  const openArtifact = useArtifactPanelStore((s) => s.openArtifact);
  if (!data || data.length === 0 || !messageId) return null;
  const own = data.filter((a) => a.reference.messageId === messageId);
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
      { source: 'user' }
    );
  return (
    <section aria-label="Generated files" className="mt-2 flex flex-wrap gap-2">
      {items.map((item) => (
        <button
          key={item.version.versionId}
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
