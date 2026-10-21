import { parsePageFilter } from '@/types/pdf-chat';
/**
 * Streaming actions.
 *
 * @deprecated Orphaned v2 streaming path. The active chat page uses
 * `agentChatService.streamMessage` directly against `/api/v1/agent/stream`
 * (see `app/(dashboard)/chat/page.tsx`). This slice is kept only so the
 * store interface and its existing unit tests stay intact; it has no UI
 * callers. Remove together with `services/streamingService.ts` in a
 * dedicated cleanup PR. Do not extend.
 *
 * The live streaming state fields (isStreaming, streamingContent, etc.) are
 * mutated directly via `useChatStore.setState()` from `useChatStreaming`
 * (not through these actions) — that external-write coupling is unaffected
 * by this split; the fields still live in ChatState (see types.ts).
 *
 * Split out of chat-store.ts (Task 5.4) with no behavior change.
 */
import type { ChatSliceCreator } from '../types';
import {
  activeAbortController,
  setActiveAbortController,
} from '../requestCoordinator';

export interface StreamingSlice {
  streamMessage: (
    content: string,
    threadId?: string,
    useRag?: boolean
  ) => Promise<void>;
  stopStreaming: () => void;
}

export const createStreamingSlice: ChatSliceCreator<StreamingSlice> = (
  set,
  get
) => ({
  streamMessage: async (content, threadId, useRag = true) => {
    const state = get();
    const targetThreadId = threadId || state.currentThreadId;

    if (!targetThreadId) {
      console.error('[ChatStore] No thread selected for streaming message');
      return;
    }

    // Single-ownership: starting a new stream supersedes — and must abort —
    // any previous one. Overwriting the controller without aborting left the
    // old stream running detached, racing this stream's state writes.
    activeAbortController?.abort();
    const controller = new AbortController();
    setActiveAbortController(controller);

    set((state) => {
      state.isStreaming = true;
      state.streamingContent = '';
      state.streamingMessageId = null;
      state.streamingCitations = [];
      state.streamingPageFilter = null;
      state.streamingDiagnosticsTraceId = null;
      state.error = null;
    });

    try {
      const { streamChatMessage } = await import('@/services/streamingService');

      for await (const event of streamChatMessage(
        targetThreadId,
        content,
        { useRag },
        controller.signal
      )) {
        // A buffered event can arrive after a newer stream took ownership —
        // drop it rather than writing into the new stream's state.
        if (activeAbortController !== controller) {
          break;
        }
        switch (event.type) {
          case 'message_start':
            set((state) => {
              state.streamingMessageId =
                (event.data.message_id as string) ?? null;
            });
            break;

          case 'rag_context':
            set((state) => {
              state.streamingPageFilter =
                parsePageFilter(event.data.page_filter) ?? null;
              state.streamingCitations =
                (event.data.citations as Array<Record<string, unknown>>) ?? [];
              state.streamingDiagnosticsTraceId =
                (event.data.diagnostics_trace_id as string) ?? null;
            });
            break;

          case 'token':
            set((state) => {
              state.streamingContent += (event.data.content as string) ?? '';
            });
            break;

          case 'message_done':
            await get().loadMessages(targetThreadId);
            break;

          case 'error':
            set((state) => {
              state.error =
                (event.data.message as string) ??
                'An error occurred during streaming';
            });
            break;
        }
      }
    } catch (err) {
      // A superseded stream (a newer streamMessage took ownership, or
      // stopStreaming already cleaned up) must not touch state that now
      // belongs to the newer stream.
      if (activeAbortController !== controller) {
        return;
      }
      // Silently catch AbortError (user clicked stop)
      const isAbort = err instanceof Error && err.name === 'AbortError';
      if (!isAbort) {
        console.error('[ChatStore] Error streaming message:', err);
        set((state) => {
          state.error = 'Failed to stream message';
        });
      }
      // On error/abort, clear streaming state immediately.
      // (For normal completion, the caller — handleSubmit — clears
      // streaming state AFTER syncing local messages to avoid a flash
      // where the virtual streaming message disappears before the
      // final messages are displayed.)
      set((state) => {
        state.isStreaming = false;
        state.streamingContent = '';
        state.streamingMessageId = null;
        state.streamingCitations = [];
        state.streamingPageFilter = null;
        state.streamingDiagnosticsTraceId = null;
      });
      setActiveAbortController(null);
    } finally {
      // Only release the controller we still own — unconditionally nulling
      // here used to clear a newer stream's controller.
      if (activeAbortController === controller) {
        setActiveAbortController(null);
      }
    }
  },

  stopStreaming: () => {
    if (activeAbortController) {
      activeAbortController.abort();
      setActiveAbortController(null);
    }
    set((state) => {
      state.isStreaming = false;
      state.streamingContent = '';
      state.streamingMessageId = null;
      state.streamingCitations = [];
      state.streamingPageFilter = null;
      state.streamingDiagnosticsTraceId = null;
      state.isRetrievingRag = false;
      state.streamingThreadId = null;
    });
  },
});
