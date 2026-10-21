'use client';

import { useState, type ReactElement } from 'react';
import { ChevronLeft, ChevronRight } from 'lucide-react';
import { useChatStore } from '@/store/chat-store';
import type { ChatPageMessage } from '@/components/chat/shared/cloudMessageView';

/** Controls canonical user alternatives; selection persists on the backend
 * and the existing transcript cache reloads the selected ancestry. */
export function MessageBranchControls({
  message,
}: {
  message?: ChatPageMessage;
}): ReactElement | null {
  const threadId = useChatStore((state) => state.currentThreadId);
  const page = useChatStore((state) =>
    threadId ? state.messagePagination[threadId] : undefined
  );
  const freshness = useChatStore((state) =>
    threadId ? state.messageFreshness[threadId] : undefined
  );
  const streaming = useChatStore((state) => state.isStreaming);
  const [error, setError] = useState<{
    threadId: string;
    messageId: string;
  } | null>(null);
  const ids = message?.siblingUserMessageIds ?? [];
  const index = message?.id ? ids.indexOf(message.id) : -1;
  if (!threadId || !page?.message_branching || index < 0 || ids.length < 2)
    return null;
  const disabled = streaming || page.selectingBranch || freshness !== 'fresh';
  const select = async (id: string): Promise<void> => {
    if (disabled || !message?.id) return;
    const ok = await useChatStore.getState().selectMessageBranch(threadId, id);
    if (useChatStore.getState().currentThreadId === threadId && !ok)
      setError({ threadId, messageId: message.id });
  };
  return (
    <div
      className="flex flex-wrap items-center justify-end gap-1 text-xs text-(--nous-fg-2)"
      aria-label="Message alternatives"
    >
      <button
        type="button"
        aria-label="Previous message alternative"
        disabled={disabled || index === 0}
        onClick={() => void select(ids[index - 1])}
        className="min-h-11 min-w-11 rounded-md disabled:opacity-40"
      >
        <ChevronLeft className="mx-auto h-4 w-4" />
      </button>
      <span aria-live="polite">
        {index + 1} of {ids.length}
      </span>
      <button
        type="button"
        aria-label="Next message alternative"
        disabled={disabled || index === ids.length - 1}
        onClick={() => void select(ids[index + 1])}
        className="min-h-11 min-w-11 rounded-md disabled:opacity-40"
      >
        <ChevronRight className="mx-auto h-4 w-4" />
      </button>
      {error?.threadId === threadId && error.messageId === message?.id && (
        <p role="alert">
          Could not switch alternatives. Reload the chat and try again.
        </p>
      )}
    </div>
  );
}
