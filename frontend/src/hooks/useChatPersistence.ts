/**
 * useChatPersistence Hook
 *
 * A React hook that bridges the chat store with existing UI components.
 * Provides a simplified interface for chat persistence operations.
 */

import toast from 'react-hot-toast';
import { useShallow } from 'zustand/react/shallow';
import { useChatStore } from '@/store/chat-store';
import { useAuthStore } from '@/stores/authStore';
import { workspaceService } from '@/services/workspaceService';
import {
  ChatMessage,
  MessageRole,
  Thread,
  ThreadStatus,
} from '@/types/workspace';
import { useCallback, useEffect, useMemo, useRef } from 'react';
import { MAX_REINIT_RETRIES } from '@/store/chat/initialState';

const debugLog =
  process.env.NODE_ENV === 'development'
    ? (...args: unknown[]) => console.log(...args)
    : () => {};

// UI Message type for compatibility with existing components
export interface UIMessage {
  id?: string;
  role: 'user' | 'assistant';
  content: string;
  timestamp: number;
  citations?: Array<{
    documentId?: string; // Optional: may be undefined for external references
    externalReferenceId?: string; // For non-database references (e.g., arXiv IDs)
    sourcePosition?: number;
    chunkId?: string;
    chunkIndex?: number;
    pageNumber?: number;
    title: string;
    score: number;
    content?: string; // Snippet content for preview
    source?: string; // Document type/source
  }>;
}

// UI Conversation type for compatibility with existing components
export interface UIConversation {
  id: string;
  title: string;
  messages: UIMessage[];
  modelId?: string;
  createdAt: number;
  updatedAt: number;
  threadId: string;
  conversationId: string;
  tags?: string[];
  isBookmarked?: boolean;
  isArchived?: boolean;
}

interface UseChatPersistenceReturn {
  // State
  isInitialized: boolean;
  isLoading: boolean;
  error: string | null;

  // Current selections
  currentWorkspaceId: string | null;
  currentConversationId: string | null;
  currentThreadId: string | null;

  // Data (UI-formatted)
  conversations: UIConversation[];
  messages: UIMessage[];

  // Actions
  initialize: () => Promise<void>;
  createNewChat: () => Promise<string | null>;
  selectConversation: (id: string) => void;
  sendMessage: (content: string) => Promise<UIMessage | null>;
  deleteConversation: (id: string) => Promise<boolean>;
  renameConversation: (id: string, title: string) => Promise<boolean>;
  archiveConversation: (id: string) => Promise<boolean>;
}

/**
 * Extract title from snippet content (for legacy citations without document_title)
 * Handles various formats:
 * - "Title: Some Title Authors: ..." (arXiv structured format)
 * - "arXiv:2401.12345 - Paper Title" or "[2401.12345] Title"
 * - "DOI: 10.xxxx/xxxxx - Title"
 * - Plain text (first meaningful line)
 */

// Pattern configuration for title extraction
const TITLE_PATTERNS: Array<{
  pattern: RegExp;
  group: number;
  minLength?: number;
  skipAuthorCheck?: boolean;
}> = [
  // "Title: <title>" pattern (common in arXiv papers)
  {
    pattern: /^Title:\s*(.+?)(?:\s*Authors:|$)/i,
    group: 1,
    skipAuthorCheck: true,
  },
  // arXiv ID formats: "arXiv:2401.12345 - Title"
  {
    pattern: /arXiv:\d{4}\.\d{4,5}(?:v\d+)?\s*[-:]\s*(.+?)(?:\n|$)/i,
    group: 1,
    minLength: 5,
  },
  // arXiv bracket format: "[2401.12345] Title"
  {
    pattern: /\[\d{4}\.\d{4,5}(?:v\d+)?\]\s*(.+?)(?:\n|$)/,
    group: 1,
    minLength: 5,
  },
  // arXiv with space: "arXiv:2401.12345 Title"
  {
    pattern: /arXiv:\d{4}\.\d{4,5}(?:v\d+)?\s+(.+?)(?:\n|$)/i,
    group: 1,
    minLength: 5,
  },
  // DOI format: "DOI: 10.xxxx/xxxxx - Title"
  { pattern: /DOI:\s*10\.\S+\s*[-:]\s*(.+?)(?:\n|$)/i, group: 1, minLength: 5 },
];

// Metadata patterns to skip when looking for title lines
const METADATA_SKIP_PATTERNS = [
  /^(Authors?:|Date:|Published:|Source:|URL:|Abstract:)/i,
  /^(arXiv:|DOI:|https?:\/\/)/i,
];

/**
 * Truncate text to max length with ellipsis
 */
function truncateTitle(text: string, maxLength: number = 150): string {
  return text.length > maxLength ? text.slice(0, maxLength - 3) + '...' : text;
}

/**
 * Check if text looks like an author list
 */
function looksLikeAuthorList(text: string): boolean {
  return /^[A-Z][a-z]+(?:\s+[A-Z]\.?\s*)+(?:,|and)/.test(text);
}

function extractTitleFromSnippet(snippet?: string): string | null {
  if (!snippet) return null;

  const trimmedSnippet = snippet.trim();
  if (!trimmedSnippet) return null;

  // Try configured patterns
  for (const {
    pattern,
    group,
    minLength = 0,
    skipAuthorCheck = false,
  } of TITLE_PATTERNS) {
    const match = trimmedSnippet.match(pattern);
    if (match?.[group] && match[group].length > minLength) {
      const extracted = match[group].trim();
      // Skip if it looks like an author list (unless configured to skip this check)
      if (!skipAuthorCheck && looksLikeAuthorList(extracted)) {
        continue;
      }
      return truncateTitle(extracted);
    }
  }

  // Fallback: Try to skip metadata lines and find first content line
  const lines = trimmedSnippet
    .split('\n')
    .map((l) => l.trim())
    .filter((l) => l.length > 0);
  for (const line of lines) {
    // Skip lines matching metadata patterns
    if (METADATA_SKIP_PATTERNS.some((p) => p.test(line))) {
      continue;
    }
    // Skip very short lines (likely not titles)
    if (line.length < 10) {
      continue;
    }
    return truncateTitle(line);
  }

  // Last resort: truncate snippet
  return truncateTitle(trimmedSnippet, 80);
}

/**
 * Map database message to UI message format
 */
export function mapDbMessageToUI(dbMsg: ChatMessage): UIMessage {
  return {
    id: dbMsg.id,
    role: dbMsg.role === MessageRole.USER ? 'user' : 'assistant',
    content: dbMsg.content,
    timestamp: new Date(dbMsg.created_at).getTime(),
    citations: dbMsg.citations?.map((c) => {
      // Build title with proper fallback chain:
      // 1. Explicit document_title from DB
      // 2. Extracted from snippet content
      // 3. External reference ID (e.g., arXiv ID like "2401.12345")
      // 4. Generic fallback
      const extractedTitle =
        extractTitleFromSnippet(c.snippet) ||
        extractTitleFromSnippet(c.snippet_preview);
      const title =
        c.document_title ??
        extractedTitle ??
        (c.external_reference_id
          ? `Reference: ${c.external_reference_id}`
          : null) ??
        'Untitled Document';

      return {
        documentId: c.document_id || undefined, // May be undefined for external refs
        externalReferenceId: c.external_reference_id || undefined, // For arXiv IDs, etc.
        sourcePosition: c.source_position,
        chunkId: c.chunk_id,
        chunkIndex: c.chunk_index,
        pageNumber: c.page_number,
        title,
        score: c.score || 0,
        content: c.snippet || c.snippet_preview,
        source: c.document_type,
      };
    }),
  };
}

/**
 * Map thread to UI conversation format
 */
function mapThreadToUIConversation(
  thread: Thread,
  messages: ChatMessage[],
  conversationId: string
): UIConversation {
  return {
    id: thread.id,
    title: thread.title || 'New Chat',
    messages: messages.map(mapDbMessageToUI),
    createdAt: new Date(thread.created_at).getTime(),
    updatedAt: new Date(thread.updated_at).getTime(),
    threadId: thread.id,
    conversationId: conversationId,
    isArchived: thread.status === ThreadStatus.ARCHIVED,
  };
}

// Module-level initialization guard. `useChatPersistence` is consumed by
// multiple sibling components simultaneously (the chat layout plus several
// context-rail panels), so a per-instance ref would let each consumer kick
// off its own parallel init. We gate on a shared promise instead so the
// first caller does the work and the rest await the same outcome.
//
// Invariants:
// - `_initCompleted` is set only after the shared promise resolves.
// - `_initInFlight` is assigned synchronously (no await between the null
//   check and the assignment) so two concurrent callers can never both enter
//   the "create promise" branch.
// - On failure, `_initInFlight` is cleared so a *future* fresh mount can
//   retry — but the current awaiters all observe the same rejection first.
let _initInFlight: Promise<void> | null = null;
let _initCompleted = false;

function resetChatPersistenceInitGuard() {
  _initInFlight = null;
  _initCompleted = false;
}

export function useChatPersistence(): UseChatPersistenceReturn {
  const { isAuthenticated } = useAuthStore();

  // Per-instance ref is still useful for noting whether *this* consumer has
  // already awaited the shared init (avoids re-subscribing in StrictMode).
  const initializationRef = useRef<{ started: boolean; completed: boolean }>({
    started: false,
    completed: false,
  });

  // Store state — scoped subscription. Reading the whole store here re-rendered
  // every consumer on each rAF-batched streaming write (streamingContent/Steps/
  // Citations, isRetrievingRag ~60x/sec) even though none are used below.
  const {
    currentWorkspaceId,
    currentConversationId,
    currentThreadId,
    workspaces,
    conversations: storeConversations,
    threads,
    messages: storeMessages,
    isLoadingWorkspaces,
    isLoadingConversations,
    isLoadingThreads,
    isLoadingMessages,
    isSendingMessage,
    error,
    // Actions (stable refs)
    initializeDefaultWorkspace,
    loadMessages,
    createThread,
    createConversation,
    registerConversation,
    sendMessage: storeSendMessage,
    setCurrentWorkspace,
    setCurrentConversation,
    setCurrentThread,
    updateThread,
    deleteThread,
    clearError,
  } = useChatStore(
    useShallow((s) => ({
      currentWorkspaceId: s.currentWorkspaceId,
      currentConversationId: s.currentConversationId,
      currentThreadId: s.currentThreadId,
      workspaces: s.workspaces,
      conversations: s.conversations,
      threads: s.threads,
      messages: s.messages,
      isLoadingWorkspaces: s.isLoadingWorkspaces,
      isLoadingConversations: s.isLoadingConversations,
      isLoadingThreads: s.isLoadingThreads,
      isLoadingMessages: s.isLoadingMessages,
      isSendingMessage: s.isSendingMessage,
      error: s.error,
      initializeDefaultWorkspace: s.initializeDefaultWorkspace,
      loadMessages: s.loadMessages,
      createThread: s.createThread,
      createConversation: s.createConversation,
      registerConversation: s.registerConversation,
      sendMessage: s.sendMessage,
      setCurrentWorkspace: s.setCurrentWorkspace,
      setCurrentConversation: s.setCurrentConversation,
      setCurrentThread: s.setCurrentThread,
      updateThread: s.updateThread,
      deleteThread: s.deleteThread,
      clearError: s.clearError,
    }))
  );

  // Derived loading state
  const isLoading =
    isLoadingWorkspaces ||
    isLoadingConversations ||
    isLoadingThreads ||
    isLoadingMessages ||
    isSendingMessage;

  // Check if initialized
  const isInitialized = !!currentWorkspaceId && workspaces.length > 0;

  // Map threads to UI conversations
  const uiConversations = useMemo((): UIConversation[] => {
    if (!currentConversationId) {
      debugLog(
        '[useChatPersistence] No currentConversationId, returning empty'
      );
      return [];
    }

    const conversationThreads = threads[currentConversationId] || [];
    debugLog(
      '[useChatPersistence] Mapping threads for conversation:',
      currentConversationId,
      'threads:',
      conversationThreads.length
    );

    return conversationThreads.map((thread) => {
      const threadMessages = storeMessages[thread.id] || [];
      return mapThreadToUIConversation(
        thread,
        threadMessages,
        currentConversationId
      );
    });
  }, [currentConversationId, threads, storeMessages]);

  // Get current messages
  const currentMessages = useMemo((): UIMessage[] => {
    if (!currentThreadId) return [];
    const threadMessages = storeMessages[currentThreadId] || [];
    return threadMessages.map(mapDbMessageToUI);
  }, [currentThreadId, storeMessages]);

  // Initialize workspace and conversation
  const initialize = useCallback(async () => {
    if (!isAuthenticated) {
      debugLog(
        '[useChatPersistence] Not authenticated, skipping initialization'
      );
      return;
    }

    // Fast path: another consumer already finished init.
    if (_initCompleted) {
      initializationRef.current = { started: true, completed: true };
      return;
    }

    // First consumer through creates the shared promise. The assignment must
    // happen synchronously (no `await` between the null-check and the
    // assignment) so subsequent concurrent callers observe the same promise.
    if (!_initInFlight) {
      initializationRef.current.started = true;

      _initInFlight = (async () => {
        debugLog('[useChatPersistence] Starting initialization...');
        let initialized = false;
        let initializationError: unknown;
        for (let attempt = 0; attempt < MAX_REINIT_RETRIES; attempt += 1) {
          try {
            await initializeDefaultWorkspace();
            initialized = true;
            break;
          } catch (error) {
            initializationError = error;
          }
        }

        if (!initialized) {
          throw (
            initializationError ?? new Error('Failed to initialize workspace')
          );
        }

        const state = useChatStore.getState();
        debugLog(
          '[useChatPersistence] After initializeDefaultWorkspace, workspaceId:',
          state.currentWorkspaceId
        );

        if (!state.currentWorkspaceId) {
          throw new Error('Workspace initialization returned no workspace');
        }

        if (useChatStore.getState().error === 'Failed to load conversations') {
          throw new Error('Failed to load conversations');
        }

        const updatedState = useChatStore.getState();
        const workspaceConversations =
          updatedState.conversations[state.currentWorkspaceId] || [];
        debugLog(
          '[useChatPersistence] Loaded conversations:',
          workspaceConversations.length
        );

        let conversationId = updatedState.currentConversationId;
        debugLog(
          '[useChatPersistence] Current conversationId:',
          conversationId
        );

        if (workspaceConversations.length > 0 && !conversationId) {
          conversationId = workspaceConversations[0].id;
          debugLog(
            '[useChatPersistence] Setting first conversation:',
            conversationId
          );
        } else if (workspaceConversations.length === 0) {
          debugLog(
            '[useChatPersistence] No conversations, creating new one...'
          );
          // Go through the deduped helper: useChatSession bootstraps the same
          // default conversation concurrently, and an unguarded create here
          // produced a second "New Chat" for every fresh user.
          const newConv = await workspaceService.getOrCreateDefaultConversation(
            state.currentWorkspaceId
          );
          if (!newConv) {
            // Surface this as a real failure instead of silently locking in
            // `_initCompleted = false` with no retry path for other waiters.
            throw new Error('Failed to create default conversation');
          }
          // The helper talks to the API directly, bypassing the store's
          // createConversation — index it so the sidebar and the reverse
          // lookup see it, exactly as a store-side create would have.
          registerConversation(newConv, state.currentWorkspaceId);
          conversationId = newConv.id;
        }

        if (!conversationId) {
          throw new Error(
            'Conversation initialization returned no conversation'
          );
        }

        const urlParams =
          typeof window !== 'undefined'
            ? new URLSearchParams(window.location.search)
            : null;
        const isNewChat = urlParams?.get('new') === '1';
        const requestedThreadId = urlParams?.get('thread');
        // The chat route restores an explicit deep link in parallel with this
        // layout initializer. `setCurrentConversation` normally clears the
        // downstream thread selection, so remember a URL-owned selection that
        // landed while the conversation read was in flight and restore it
        // after that reset completes.
        const selectedThreadBeforeConversationLoad = requestedThreadId
          ? useChatStore.getState().currentThreadId
          : null;

        await setCurrentConversation(conversationId);

        debugLog(
          '[useChatPersistence] Loaded threads for conversation:',
          conversationId
        );
        if (useChatStore.getState().error === 'Failed to load threads') {
          throw new Error('Failed to load threads');
        }

        const threadsState = useChatStore.getState();
        const conversationThreads = threadsState.threads[conversationId] || [];
        if (
          requestedThreadId &&
          selectedThreadBeforeConversationLoad === requestedThreadId &&
          threadsState.currentThreadId !== requestedThreadId
        ) {
          debugLog(
            '[useChatPersistence] Restoring deep-linked thread after conversation init:',
            requestedThreadId
          );
          setCurrentThread(requestedThreadId);
        }
        // An explicit "new chat" (?new=1) must land on a blank composer.
        // useChatSession already honors this; without the same check here
        // this hook writes a stale thread id into the store, and the next
        // send appends to that previous thread instead of starting a new
        // one (and the URL never becomes ?thread=).
        const hasExplicitThreadIntent = Boolean(urlParams?.get('thread'));
        if (
          conversationThreads.length > 0 &&
          !threadsState.currentThreadId &&
          !isNewChat &&
          !hasExplicitThreadIntent
        ) {
          const firstThread = conversationThreads[0];
          debugLog(
            '[useChatPersistence] Selecting first thread:',
            firstThread.id
          );
          setCurrentThread(firstThread.id); // This triggers loadMessages
        }
      })();

      // Attach terminal handlers once. All awaiting consumers observe the
      // same resolution; on failure we clear `_initInFlight` so a fresh mount
      // can retry, but only after the failure has propagated to every
      // current awaiter.
      _initInFlight
        .then(() => {
          _initCompleted = true;
          debugLog('[useChatPersistence] Initialization complete');
        })
        .catch((error) => {
          console.error('[useChatPersistence] Initialization failed:', error);
          const message =
            error instanceof Error
              ? error.message
              : 'Failed to initialize workspace';
          toast.error(message);
        })
        .finally(() => {
          if (!_initCompleted) {
            _initInFlight = null;
          }
        });
    }

    // Every consumer (including the one that just created the promise) awaits
    // the same outcome so `initializationRef.current.completed` reflects real
    // state. Errors are already surfaced by the owning `.catch` above, so
    // swallow here to avoid double-reporting.
    initializationRef.current.started = true;
    try {
      await _initInFlight;
      if (_initCompleted) {
        initializationRef.current.completed = true;
      } else {
        // Init resolved without completing (e.g., no workspaceId) or was
        // aborted; allow a later explicit retry.
        initializationRef.current.started = false;
      }
    } catch {
      initializationRef.current.started = false;
    }
  }, [
    isAuthenticated,
    initializeDefaultWorkspace,
    setCurrentConversation,
    setCurrentThread,
    registerConversation,
  ]);

  // Create new chat (thread)
  const createNewChat = useCallback(async (): Promise<string | null> => {
    debugLog('[useChatPersistence] createNewChat called', {
      currentConversationId,
      currentWorkspaceId,
      isInitialized: !!currentWorkspaceId,
    });

    let conversationId = currentConversationId;

    // If no conversation exists, create one first
    if (!conversationId) {
      debugLog('[useChatPersistence] No conversation, creating one first...');

      if (!currentWorkspaceId) {
        console.error(
          '[useChatPersistence] No workspace available - make sure to call initialize() first'
        );
        return null;
      }

      const newConv = await createConversation({
        workspace_id: currentWorkspaceId,
        title: 'New Chat',
      });

      if (newConv) {
        conversationId = newConv.id;
        setCurrentConversation(newConv.id);
        debugLog('[useChatPersistence] Created conversation:', newConv.id);
      } else {
        console.error(
          '[useChatPersistence] Failed to create conversation - API may have returned null'
        );
        return null;
      }
    }

    try {
      debugLog(
        '[useChatPersistence] Creating thread for conversation:',
        conversationId
      );
      const thread = await createThread({
        conversation_id: conversationId,
        title: 'New Chat',
      });

      if (thread) {
        debugLog(
          '[useChatPersistence] Thread created successfully:',
          thread.id
        );
        setCurrentThread(thread.id);
        return thread.id;
      }
      console.error(
        '[useChatPersistence] createThread returned null/undefined - check network tab for API errors'
      );
      return null;
    } catch (error) {
      console.error('[useChatPersistence] Failed to create new chat:', error);
      return null;
    }
  }, [
    currentConversationId,
    currentWorkspaceId,
    createConversation,
    createThread,
    setCurrentConversation,
    setCurrentThread,
  ]);

  // Select conversation (thread in our UI mapping)
  const selectConversation = useCallback(
    (id: string) => {
      setCurrentThread(id);
    },
    [setCurrentThread]
  );

  // Send message
  const sendMessage = useCallback(
    async (content: string): Promise<UIMessage | null> => {
      if (!currentThreadId && !currentConversationId) {
        console.error(
          '[useChatPersistence] No thread or conversation selected'
        );
        return null;
      }

      // If no thread exists, create one first
      let threadId = currentThreadId;
      if (!threadId && currentConversationId) {
        const newThread = await createThread({
          conversation_id: currentConversationId,
          title: content.substring(0, 50),
        });
        if (newThread) {
          threadId = newThread.id;
          setCurrentThread(newThread.id);
        }
      }

      if (!threadId) {
        console.error('[useChatPersistence] Failed to get or create thread');
        return null;
      }

      const message = await storeSendMessage(content, threadId);

      if (message) {
        return mapDbMessageToUI(message);
      }
      return null;
    },
    [
      currentThreadId,
      currentConversationId,
      createThread,
      setCurrentThread,
      storeSendMessage,
    ]
  );

  // Delete conversation (thread)
  const deleteConversation = useCallback(
    async (id: string): Promise<boolean> => {
      return await deleteThread(id);
    },
    [deleteThread]
  );

  // Rename conversation (thread)
  const renameConversation = useCallback(
    async (id: string, title: string): Promise<boolean> => {
      const result = await updateThread(id, { title });
      return !!result;
    },
    [updateThread]
  );

  // Archive conversation (thread)
  const archiveConversation = useCallback(
    async (id: string): Promise<boolean> => {
      const result = await updateThread(id, { status: ThreadStatus.ARCHIVED });
      return !!result;
    },
    [updateThread]
  );

  // Reset initialization state when user logs out. Both the per-instance ref
  // AND the module-level shared guards must clear so a subsequent login
  // re-runs initialization instead of short-circuiting on a stale completion.
  useEffect(() => {
    if (!isAuthenticated) {
      initializationRef.current = { started: false, completed: false };
      resetChatPersistenceInitGuard();
    }
  }, [isAuthenticated]);

  // Auto-initialize on mount - use ref guard instead of initialize in deps to prevent loops
  useEffect(() => {
    if (
      isAuthenticated &&
      !initializationRef.current.started &&
      !initializationRef.current.completed
    ) {
      initialize();
    }
  }, [isAuthenticated, initialize]);

  return {
    isInitialized,
    isLoading,
    error,
    currentWorkspaceId,
    currentConversationId,
    currentThreadId,
    conversations: uiConversations,
    messages: currentMessages,
    initialize,
    createNewChat,
    selectConversation,
    sendMessage,
    deleteConversation,
    renameConversation,
    archiveConversation,
  };
}

export default useChatPersistence;
