/**
 * Streaming actions.
 *
 * The active chat page streams through `agentChatService.streamMessage`
 * against `/api/v1/agent/stream` (driven by `useChatStreaming`). The live
 * streaming state fields (isStreaming, streamingContent, etc.) are mutated
 * directly via `useChatStore.setState()` from `useChatStreaming`; they still
 * live in ChatState (see types.ts). This slice only owns `stopStreaming`,
 * which resets that state.
 *
 * Split out of chat-store.ts (Task 5.4) with no behavior change.
 */
import type { ChatSliceCreator } from '../types';
import {
  activeAbortController,
  setActiveAbortController,
} from '../requestCoordinator';

export interface StreamingSlice {
  stopStreaming: () => void;
}

export const createStreamingSlice: ChatSliceCreator<StreamingSlice> = (
  set
) => ({
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
