import { useCallback } from 'react';
import toast from 'react-hot-toast';

import type { ChatPageMessage } from '@/components/chat/shared/cloudMessageView';
import { enhancedDocumentService } from '@/services/enhancedDocumentService';
import type { MessageAttachment, Workspace } from '@/types/workspace';

// ============================================
// HOOK PARAMS
// ============================================

export interface UseChatComposerActionsParams {
  workspace: Workspace | null;
  setInput: React.Dispatch<React.SetStateAction<string>>;
  handleSubmit: (
    contentOverride?: string,
    historyOverride?: ChatPageMessage[],
    supersedesClientMessageId?: string,
    attachmentIds?: string[],
    attachments?: MessageAttachment[]
  ) => Promise<void>;
  isLoading: boolean;
  storeIsStreaming: boolean;
  displayedMessages: ChatPageMessage[];
}

/** Whether one attached file made it to storage, and the document it became. */
export interface AttachOutcome {
  ok: boolean;
  /** Present only when `ok`. The composer keeps it on the chip so the id can
   * ride along with the turn the user attached it to. */
  documentId?: string;
}

export interface UseChatComposerActionsReturn {
  /** Per-file upload outcomes, in the order the files were given. */
  handleAttach: (files: FileList) => Promise<AttachOutcome[]>;
  /** Re-send the user turn preceding an assistant message, truncating the
   * transcript to just before it. When that turn carries a persisted
   * `clientMessageId` it rides along as `supersedes_client_message_id`, so the
   * server tombstones the replaced turn instead of persisting a second pair. */
  handleRegenerate: (assistantMessageIndex: number) => void;
  /** Edit a prior user message in place and re-send. Truncates the transcript
   * to just before the edited message (dropping its old answer + any
   * local-only error bubbles) and re-submits with the new text — the same
   * slice-and-resend contract as {@link handleRegenerate}. When the edited turn
   * carries a persisted `clientMessageId`, that id also rides along as
   * `supersedes_client_message_id` so the server durably tombstones the old
   * turn and everything after it. */
  handleEditUserMessage: (
    userMessageIndex: number,
    newContent: string
  ) => void;
  retryLast: () => void;
  /** Bare submit — clears nothing, just forwards to the streaming path.
   * Callers that need to clear ephemeral output first (useSlashCommands)
   * wrap this rather than calling handleSubmit directly. */
  submit: (attachmentIds?: string[]) => void;
}

// ============================================
// HOOK
// ============================================

/**
 * Composer-level actions: attach, retry, regenerate, and submit. Delegates
 * all persistence/streaming to the existing useChatStreaming/store path
 * (handleSubmit) — this hook only coordinates which turn gets (re)sent and
 * uploads files, it never calls a second create-message endpoint.
 */
export function useChatComposerActions({
  workspace,
  setInput,
  handleSubmit,
  isLoading,
  storeIsStreaming,
  displayedMessages,
}: UseChatComposerActionsParams): UseChatComposerActionsReturn {
  // Upload files selected via the Paperclip attach control.
  // Uses enhancedDocumentService (v1 /files/upload) since no workspace-scoped
  // attach endpoint exists yet. Each upload is isolated via .catch so one
  // failure does not cancel others.
  const handleAttach = useCallback(
    async (files: FileList) => {
      if (!workspace) {
        console.warn('[Chat] Cannot attach: no workspace');
        toast.error('No workspace available — attachment was not uploaded.');
        // Every file failed, so the composer's chips must settle on error
        // rather than sit in the uploading state forever.
        return Array.from(files).map(() => ({ ok: false as const }));
      }
      const uploads = Array.from(files).map((file) =>
        enhancedDocumentService
          .uploadDocument(file, {
            title: file.name,
            processing_priority: 'normal',
          })
          .then((result) => ({
            file,
            ok: true as const,
            documentId: result.response.document_id,
          }))
          .catch((err) => {
            console.error('[Chat] Upload failed for', file.name, err);
            return { file, ok: false as const };
          })
      );
      // Promise.all preserves order, so the caller can zip these onto the
      // chips it created from the same FileList.
      const results = await Promise.all(uploads);
      const failed = results.filter((r) => !r.ok);
      if (failed.length > 0) {
        toast.error(
          failed.length === 1
            ? `Upload failed for ${failed[0].file.name}.`
            : `Upload failed for ${failed.length} of ${results.length} files.`
        );
      }
      return results.map((r) => ({
        ok: r.ok,
        ...(r.ok && r.documentId ? { documentId: r.documentId } : {}),
      }));
    },
    [workspace]
  );

  // Regenerate a specific assistant turn — re-sends its prior user message
  // with the history truncated to just before it. Shared by the /retry
  // command and the ChatMessageList "Regenerate" action.
  const handleRegenerate = useCallback(
    (assistantMessageIndex: number) => {
      // Bail before preparing a replacement turn if a stream is in flight;
      // handleSubmit would otherwise reject it via its own single-flight guard.
      if (isLoading || storeIsStreaming) return;
      let priorUserIndex = -1;
      for (let index = assistantMessageIndex - 1; index >= 0; index -= 1) {
        if (displayedMessages[index]?.role === 'user') {
          priorUserIndex = index;
          break;
        }
      }
      if (priorUserIndex < 0) return;
      const priorUser = displayedMessages[priorUserIndex];
      const regenerationHistory = displayedMessages.slice(0, priorUserIndex);
      setInput(priorUser.content);
      // Bug 2: pass the content explicitly. `handleSubmit` reads `input` from
      // its closure, and `setInput` above only schedules a state update — the
      // deferred `handleSubmit` would otherwise see the stale pre-setInput value.
      const contentToSend = priorUser.content;
      // Durable regenerate: name the turn being replaced so the server
      // tombstones it, exactly as edit-and-resend does. Without it the
      // regenerated turn persists as an extra pair and the canonical page
      // renders both answers after reconcile/reload. Omitted for legacy rows
      // with no persisted client_message_id (FE-only truncation, as before).
      const supersedes = priorUser.clientMessageId;
      // The replacement keeps the turn's attachments: ids for the server,
      // metadata for the optimistic bubble's chips.
      const attachments = priorUser.attachments;
      const attachmentIds = attachments?.map((file) => file.document_id);
      setTimeout(
        () =>
          handleSubmit(
            contentToSend,
            regenerationHistory,
            supersedes,
            attachmentIds,
            attachments
          ),
        0
      );
    },
    [displayedMessages, handleSubmit, isLoading, storeIsStreaming, setInput]
  );

  // Edit a prior user message in place and re-send it. The history is
  // truncated to just before the edited message — identical to regenerate — so
  // the edited turn replaces the old answer (and any local-only error bubble).
  // handleSubmit drops non-optimistic overlays from newMessages automatically.
  const handleEditUserMessage = useCallback(
    (userMessageIndex: number, newContent: string) => {
      if (isLoading || storeIsStreaming) return;
      const content = newContent.trim();
      if (!content) return;
      const edited = displayedMessages[userMessageIndex];
      if (!edited || edited.role !== 'user') return;
      const editedHistory = displayedMessages.slice(0, userMessageIndex);
      setInput(content);
      // Durable edit: name the turn being replaced so the server tombstones it
      // (and everything after it) instead of only truncating the request
      // context. Omitted when the edited turn has no persisted
      // client_message_id — a legacy row or one that never reached the server;
      // the FE-only truncation is then the same behaviour as before.
      const supersedes = edited.clientMessageId;
      const attachments = edited.attachments;
      const attachmentIds = attachments?.map((file) => file.document_id);
      // Pass content explicitly — setInput only schedules an update, and the
      // deferred handleSubmit would otherwise read the stale input value.
      setTimeout(
        () =>
          handleSubmit(
            content,
            editedHistory,
            supersedes,
            attachmentIds,
            attachments
          ),
        0
      );
    },
    [displayedMessages, handleSubmit, isLoading, storeIsStreaming, setInput]
  );

  // Regenerate the most recent assistant response (the /retry command).
  const retryLast = useCallback(() => {
    const lastAssistantIdx = [...displayedMessages]
      .map((m, i) => ({ role: m.role, i }))
      .reverse()
      .find((x) => x.role === 'assistant')?.i;
    if (lastAssistantIdx !== undefined) handleRegenerate(lastAssistantIdx);
  }, [displayedMessages, handleRegenerate]);

  const submit = useCallback(
    (attachmentIds?: string[]) => {
      handleSubmit(undefined, undefined, undefined, attachmentIds);
    },
    [handleSubmit]
  );

  return {
    handleAttach,
    handleRegenerate,
    handleEditUserMessage,
    retryLast,
    submit,
  };
}
