'use client';

import {
  isThreadSwitchPending,
  mapDbMessageToChatPageMessage,
  selectDisplayedMessages,
} from '@/components/chat/shared/cloudMessageView';
import toast from 'react-hot-toast';

import type { ChatPageMessage } from '@/components/chat/shared/cloudMessageView';
import { getSelectedThreadUrl } from '@/components/chat/shared/chatNavigation';
import { ChatConversation } from '@/hooks/chat/chatTypes';
import { upsertConversationFromThread } from '@/components/chat/shared/threadConversationState';
import { workspaceService } from '@/services/workspaceService';
import { useChatStore } from '@/store/chat-store';
import { useAuthStore } from '@/stores/authStore';
import { getLoginPathWithRedirect } from '@/utils/authRedirect';
import {
  ChatMessage as DBChatMessage,
  Conversation as DBConversation,
  Thread,
  Workspace,
} from '@/types/workspace';
import { useRouter, useSearchParams } from 'next/navigation';
import { useCallback, useEffect, useMemo, useRef, useState } from 'react';

const THREADS_PAGE_SIZE = 50;

function getErrorStatus(error: unknown): number | undefined {
  if (typeof error !== 'object' || error === null) return undefined;
  const candidate = error as {
    error?: { status_code?: unknown };
    response?: { status?: unknown };
    status_code?: unknown;
  };
  const status =
    candidate.error?.status_code ??
    candidate.response?.status ??
    candidate.status_code;
  return typeof status === 'number' ? status : undefined;
}

function isUnavailableThreadError(error: unknown): boolean {
  const status = getErrorStatus(error);
  return status === 403 || status === 404;
}

// Shared by every workspace-thread call site (cold load, warm start, "show older")
// so the sidebar's ChatConversation shape can't drift between them.
function threadToConversation(thread: Thread): ChatConversation {
  return {
    id: thread.id,
    title: thread.title || 'New Chat',
    messages: [],
    createdAt: new Date(thread.created_at).getTime(),
    updatedAt: new Date(thread.updated_at).getTime(),
    threadId: thread.id,
    conversationId: thread.conversation_id,
    previewText: thread.last_message_preview || thread.summary || undefined,
    messageCount: thread.message_count,
  };
}

// The context rail and the agent's page_context read a thread's project
// binding (source_project_id) off the CHAT STORE's thread row, but the store
// only ever loads page 1 of one conversation (useChatPersistence, once per
// session). Every thread this hook fetches is indexed there too, so a thread
// from a later page or a deep link doesn't look permanently unbound.
function indexThreads(threads: Thread[]): void {
  const { registerThread } = useChatStore.getState();
  for (const thread of threads) registerThread(thread);
}

export interface ChatAuthRecoveryRoute {
  isReady: boolean;
  threadId: string | null;
  /** True only while the initial URL/workspace selection is settling. */
  isInitializing?: boolean;
  /** Keeps an explicit ?new=1 intent distinct from a bare /chat URL. */
  isNewChatIntent?: boolean;
}

export interface UseChatSessionReturn {
  // State
  conversations: ChatConversation[];
  setConversations: React.Dispatch<React.SetStateAction<ChatConversation[]>>;
  messages: ChatPageMessage[];
  setMessages: React.Dispatch<React.SetStateAction<ChatPageMessage[]>>;
  workspace: Workspace | null;
  dbConversation: DBConversation | null;
  isInitializing: boolean;
  initError: string | null;
  isLoadingMessages: boolean;
  /** Draft recovery may read session storage only after the explicit URL
   * intent and the selected thread converge on a successful initialization. */
  authRecoveryRoute: ChatAuthRecoveryRoute;

  // Refs
  isHydratedRef: React.MutableRefObject<boolean>;

  // Store bindings
  setCurrentThread: (threadId: string | null) => void;
  storeMessages: import('@/types/workspace').ChatMessage[] | null;
  addMessageToStore: (
    threadId: string,
    message: import('@/types/workspace').ChatMessage
  ) => void;
  isAuthenticated: boolean;

  // Derived
  activeThreadId: string | null;
  displayedMessages: ChatPageMessage[];

  // Pagination
  loadOlderMessages: (threadId: string) => Promise<void>;
  messagePagination: Record<
    string,
    { hasMore: boolean; loadingOlder: boolean; loadedCount: number }
  > | null;

  // Sidebar thread pagination (CX8) — the thread list is capped at
  // THREADS_PAGE_SIZE per fetch; loadMoreThreads fetches+appends the next page.
  hasMoreThreads: boolean;
  loadMoreThreads: () => Promise<void>;

  // Helpers
  mapDbMessageToUiMessage: (dbMsg: DBChatMessage) => ChatPageMessage;
  loadThreadsFromDb: (
    workspaceId: string,
    isRetry?: boolean
  ) => Promise<{ ok: boolean; threadCount: number }>;
}

/**
 * Custom hook encapsulating session/workspace initialization logic for the chat page.
 *
 * Manages: conversations, active thread, messages, workspace, DB conversation,
 * initialization lifecycle, and lazy message loading.
 *
 * Must be called inside a component wrapped in <Suspense> (uses useSearchParams).
 */
export function useChatSession(): UseChatSessionReturn {
  // ---- State ----
  const [conversations, setConversations] = useState<ChatConversation[]>([]);
  const [messages, setMessages] = useState<ChatPageMessage[]>([]);

  // Database state
  const [workspace, setWorkspace] = useState<Workspace | null>(null);
  const dbConversation: DBConversation | null = null;
  const [isInitializing, setIsInitializing] = useState(true);
  const [initError, setInitError] = useState<string | null>(null);

  // Sidebar thread pagination (CX8): which workspace + page the currently
  // loaded thread list reflects, so loadMoreThreads knows what to fetch next.
  const [hasMoreThreads, setHasMoreThreads] = useState(false);
  const threadsListWorkspaceIdRef = useRef<string | null>(null);
  const threadsPageRef = useRef(1);
  const threadsRequestGenerationRef = useRef(0);
  const firstPageThreadsRef = useRef<Thread[]>([]);

  // ---- Refs ----
  // URL synchronization reads the latest list without subscribing its effect
  // to conversation-cache writes, which are common during a thread handoff.
  const conversationsRef = useRef(conversations);
  const messagesRef = useRef(messages);
  const localMessagesThreadIdRef = useRef<string | null>(null);
  const isHydratedRef = useRef(false);
  // Every auth lifecycle owns a generation. Awaited work checks this before
  // publishing so logout/unmount cannot leak an old workspace into a later
  // session, while a re-login can start immediately instead of waiting on the
  // old request. Workspace service itself deduplicates same-session creates.
  const initGenerationRef = useRef(0);
  // When initialization replaces a stale initial ?thread= target, Next may
  // commit the local state update before its router.replace updates search
  // params. Suppress only that exact obsolete value so the URL effect cannot
  // replay the failed lookup in between those commits.
  const unavailableInitialUrlThreadRef = useRef<string | null>(null);

  // ---- Auth ----
  const { isAuthenticated } = useAuthStore();

  // ---- Store bindings ----
  const activeThreadId = useChatStore((state) => state.currentThreadId);
  const setCurrentThread = useChatStore((state) => state.setCurrentThread);
  // Select only the active thread's messages to avoid re-renders when
  // background threads change (streaming elsewhere, FIFO eviction, etc.).
  const activeThreadMessages = useChatStore((state) =>
    activeThreadId ? (state.messages[activeThreadId] ?? null) : null
  );
  const activeMessageFreshness = useChatStore((state) =>
    activeThreadId ? state.messageFreshness?.[activeThreadId] : undefined
  );
  const addMessageToStore = useChatStore((state) => state.addMessageToStore);
  const storeLoadOlderMessages = useChatStore(
    (state) => state.loadOlderMessages
  );
  const storeLoadingThreadId = useChatStore((state) => state.loadingThreadId);
  const messagePagination = useChatStore((state) => state.messagePagination);

  // ---- Derived values ----
  const displayedMessages = useMemo(
    () =>
      selectDisplayedMessages({
        localMessages: messages,
        storeMessages: activeThreadMessages ?? [],
        messageFreshness: activeMessageFreshness,
      }),
    [messages, activeThreadMessages, activeMessageFreshness]
  );

  // Skeleton gate: only an uncached switch into the active thread counts as
  // "loading" — a send-path fetch of a just-created thread (local turn
  // present) or a background refresh of a cached thread must keep rendering
  // the transcript (#1121 regressions).
  const isThreadLoadPending = isThreadSwitchPending({
    activeThreadId,
    loadingThreadId: storeLoadingThreadId,
    localMessageCount: messages.length,
    storeMessageCount: activeThreadMessages?.length ?? 0,
  });

  // ---- Callbacks ----

  // Map a DB/server message to the UI shape. Thin wrapper over the canonical
  // mapper so the lazy-load path (here) and the store path can't drift — a
  // prior second copy silently dropped plan + token_usage on thread reload.
  const mapDbMessageToUiMessage = useCallback(
    (dbMsg: DBChatMessage): ChatPageMessage =>
      mapDbMessageToChatPageMessage(dbMsg),
    []
  );

  // Pagination: load older messages for a thread (prepends to the store list).
  const loadOlderMessages = useCallback(
    async (threadId: string) => {
      try {
        await storeLoadOlderMessages(threadId);
      } catch (error) {
        // Mirror loadMoreThreads: a failed pagination fetch must not be an
        // unhandled rejection with zero feedback.
        console.error('[Chat] Failed to load older messages:', error);
        toast.error('Could not load older messages. Please try again.');
      }
    },
    [storeLoadOlderMessages]
  );

  // ---- Router / Search params ----
  const searchParams = useSearchParams();
  const searchParamsRef = useRef(searchParams);
  // Depend on the primitive value, not the search-params object: local state
  // renders may change object identity before router.push updates ?thread=.
  const threadFromUrl = searchParams.get('thread');
  const hasNewChatIntent = searchParams.get('new') === '1';
  const authRecoveryRoute = useMemo<ChatAuthRecoveryRoute>(() => {
    const routeThreadId = hasNewChatIntent ? null : threadFromUrl;
    return {
      threadId: routeThreadId,
      isInitializing,
      isNewChatIntent: hasNewChatIntent,
      isReady: Boolean(
        isAuthenticated &&
        !isInitializing &&
        initError === null &&
        routeThreadId &&
        activeThreadId === routeThreadId
      ),
    };
  }, [
    activeThreadId,
    hasNewChatIntent,
    initError,
    isAuthenticated,
    isInitializing,
    threadFromUrl,
  ]);
  const router = useRouter();
  // Read the router through a ref in callbacks/effects: depending on the
  // router object itself re-runs the init chain whenever its identity
  // changes, which can loop initialization.
  const routerRef = useRef(router);

  // ---- Effects ----

  // Latest-value refs, synced after every commit. These exist so effects and
  // callbacks can read current values WITHOUT taking them as dependencies —
  // depending on `conversations` re-runs URL sync on every cache write during
  // a thread handoff, and depending on `router` re-runs the init chain
  // whenever its identity changes, which loops initialization.
  //
  // Written here rather than during render (react-hooks/refs): a render-phase
  // ref write is a side effect that misbehaves under concurrent rendering.
  // This effect is declared before every other effect in this hook, and React
  // runs a commit's effects in declaration order, so the effects below observe
  // the values from the render they were scheduled by. Every read site is in
  // an effect or a callback — none during render — so nothing sees a stale
  // value. Keep this block first if you add effects above it.
  useEffect(() => {
    conversationsRef.current = conversations;
    messagesRef.current = messages;
    searchParamsRef.current = searchParams;
    routerRef.current = router;
  });

  // Reset refs when user changes (logout/login)
  useEffect(() => {
    if (!isAuthenticated) {
      isHydratedRef.current = false;
    }
  }, [isAuthenticated]);

  // Redirect to login if not authenticated
  useEffect(() => {
    if (!isAuthenticated && !isInitializing) {
      const timer = setTimeout(() => {
        router.push(
          getLoginPathWithRedirect(
            threadFromUrl ? getSelectedThreadUrl(threadFromUrl) : '/chat'
          )
        );
      }, 1500); // Short delay to show the "Redirecting..." state
      return () => clearTimeout(timer);
    }
  }, [isAuthenticated, isInitializing, router, threadFromUrl]);

  // Thread-scoped load-failure signal. The toast fires only when the failed
  // thread is the one on screen, and — unlike the old loading-flag transition
  // watcher — also for background stale-cache refreshes, which have no
  // loading flags. A failed thread the user has already navigated away from
  // never toasts on the wrong conversation. The nonce dedupes re-renders
  // while letting a genuinely new failure re-fire.
  const messageLoadError = useChatStore((state) => state.messageLoadError);
  const lastMessageLoadErrorNonceRef = useRef<number | null>(null);
  useEffect(() => {
    if (!messageLoadError) return;
    if (messageLoadError.threadId !== activeThreadId) return;
    if (lastMessageLoadErrorNonceRef.current === messageLoadError.nonce) return;
    lastMessageLoadErrorNonceRef.current = messageLoadError.nonce;
    toast.error('Could not load this conversation. Please try again.');
  }, [activeThreadId, messageLoadError]);

  // Keep sidebar metadata current without copying the transcript into a
  // second cache. Zustand is the sole canonical transcript owner.
  useEffect(() => {
    if (!activeThreadId || !activeThreadMessages?.length) return;
    const latest = activeThreadMessages[activeThreadMessages.length - 1];
    // This write has to *persist* after the active thread changes: thread A must
    // keep showing its latest message in the sidebar once you switch to B.
    // Deriving it with useMemo only knows about the thread currently loaded, so
    // A reverts to a stale DB preview. Real fix is dropping the local
    // `conversations` mirror and making the Zustand store its sole owner.
    // eslint-disable-next-line react-hooks/set-state-in-effect -- see above
    setConversations((prev) =>
      prev.map((conversation) =>
        conversation.id === activeThreadId
          ? {
              ...conversation,
              messages: [],
              previewText: latest.content,
              messageCount: Math.max(
                conversation.messageCount ?? 0,
                activeThreadMessages.length
              ),
              updatedAt: new Date(latest.created_at).getTime(),
            }
          : conversation
      )
    );
  }, [activeThreadId, activeThreadMessages]);

  // Handle thread switching from URL query param (single source of truth)
  useEffect(() => {
    if (isInitializing || !isAuthenticated) {
      return;
    }

    if (unavailableInitialUrlThreadRef.current) {
      if (unavailableInitialUrlThreadRef.current === threadFromUrl) return;
      unavailableInitialUrlThreadRef.current = null;
    }

    if (threadFromUrl) {
      console.log('[Chat] Thread switch requested:', threadFromUrl);
      const targetConv = conversationsRef.current.find(
        (c) => c.id === threadFromUrl
      );

      if (targetConv) {
        if (targetConv.id !== useChatStore.getState().currentThreadId) {
          setCurrentThread(targetConv.id);
          console.log('[Chat] Switched to thread:', targetConv.title);
        }
        return;
      }

      console.log(
        '[Chat] Thread not found in conversations, fetching detail:',
        threadFromUrl
      );

      let cancelled = false;
      const activeThreadAtRequestStart =
        useChatStore.getState().currentThreadId;

      (async () => {
        try {
          const thread = await workspaceService.getThread(threadFromUrl, {
            includeMessages: false,
          });
          // A sidebar selection can happen before router.push replaces the old
          // URL. Do not let this stale detail response overwrite that newer
          // local selection while the query string is catching up.
          if (
            cancelled ||
            useChatStore.getState().currentThreadId !==
              activeThreadAtRequestStart
          ) {
            return;
          }

          indexThreads([thread]);
          setConversations((prev) =>
            upsertConversationFromThread(prev, thread, [])
          );
          setCurrentThread(thread.id);
        } catch (error: unknown) {
          // A sidebar selection may supersede this request before either its
          // success or failure settles. Neither result owns navigation then.
          if (
            cancelled ||
            useChatStore.getState().currentThreadId !==
              activeThreadAtRequestStart
          ) {
            return;
          }

          console.error('[Chat] Failed to fetch requested thread:', error);
          if (isUnavailableThreadError(error)) {
            const currentThreadId = useChatStore.getState().currentThreadId;
            const fallbackConversation =
              conversationsRef.current.find(
                (conversation) => conversation.id === currentThreadId
              ) ?? conversationsRef.current[0];
            const fallbackThreadId = fallbackConversation?.id ?? null;
            setCurrentThread(fallbackThreadId);
            toast.error('That conversation is no longer available.');
            routerRef.current.replace(
              fallbackThreadId
                ? getSelectedThreadUrl(fallbackThreadId)
                : '/chat'
            );
            return;
          }

          // A transient/server failure is retryable. Keep the requested URL
          // intact instead of silently turning it into a different session.
          toast.error('Could not open that conversation. Please try again.');
        }
      })();

      return () => {
        cancelled = true;
      };
    }
  }, [threadFromUrl, isInitializing, isAuthenticated, setCurrentThread]);

  // Load threads and messages from database
  const loadThreadsFromDb = useCallback(
    async (
      workspaceId: string,
      _isRetry = false
    ): Promise<{ ok: boolean; threadCount: number }> => {
      const requestGeneration = threadsRequestGenerationRef.current + 1;
      const selectionAtRequestStart = useChatStore.getState().currentThreadId;
      const conversationIdsAtRequestStart = new Set(
        conversationsRef.current.map((conversation) => conversation.id)
      );
      threadsRequestGenerationRef.current = requestGeneration;
      threadsListWorkspaceIdRef.current = workspaceId;
      try {
        console.log(
          '[Chat] Loading threads from database for workspace:',
          workspaceId
        );
        const threadResponse = await workspaceService.listWorkspaceThreads(
          workspaceId,
          { page: 1, limit: THREADS_PAGE_SIZE }
        );

        // Mutation-verified by useChatSession.workspaceThreads.test.tsx:
        // removing this identity gate lets a late page from the prior
        // workspace replace the active workspace's sidebar.
        if (
          threadsListWorkspaceIdRef.current !== workspaceId ||
          threadsRequestGenerationRef.current !== requestGeneration
        ) {
          return { ok: false, threadCount: 0 };
        }

        indexThreads(threadResponse.threads);
        firstPageThreadsRef.current = threadResponse.threads;

        // CX8: record what this list reflects so "show older threads" knows
        // which workspace + page to fetch next.
        threadsPageRef.current = 1;
        setHasMoreThreads(threadResponse.has_more);

        // Map threads without loading messages (lazy-loaded on selection)
        const uiConversations: ChatConversation[] =
          threadResponse.threads.map(threadToConversation);

        setConversations((previous) => {
          const serverIds = new Set(
            uiConversations.map((conversation) => conversation.id)
          );
          // A first send is allowed while this page is in flight. Preserve
          // rows inserted after the request began so a late server snapshot
          // cannot make the just-created active thread disappear.
          const createdWhilePending = previous.filter(
            (conversation) =>
              !conversationIdsAtRequestStart.has(conversation.id) &&
              !serverIds.has(conversation.id)
          );
          return [...createdWhilePending, ...uiConversations];
        });
        console.log(
          '[Chat] Loaded',
          uiConversations.length,
          'threads from database'
        );

        // Restore active thread from URL param or default to first — UNLESS the
        // user explicitly started a new chat (?new=1), in which case load the
        // sidebar list but leave the composer blank (no auto-open).
        const threadFromUrl = searchParamsRef.current.get('thread');
        const isNewChat = searchParamsRef.current.get('new') === '1';

        const selectionIsStillOwned =
          useChatStore.getState().currentThreadId === selectionAtRequestStart;

        if (!selectionIsStillOwned) {
          console.log(
            '[Chat] Thread-page selection ignored after newer selection'
          );
        } else if (isNewChat && !threadFromUrl) {
          setCurrentThread(null);
          console.log('[Chat] New chat requested; not auto-selecting a thread');
        } else if (threadFromUrl) {
          const urlConversation = uiConversations.find(
            (conversation) => conversation.id === threadFromUrl
          );
          if (urlConversation) {
            setCurrentThread(urlConversation.id);
            console.log(
              '[Chat] Restored thread from URL param:',
              urlConversation.title
            );
          } else {
            // The URL effect owns uncached deep links, including when the
            // requested thread falls outside the first sidebar page.
            setCurrentThread(null);
          }
        } else if (
          uiConversations.length > 0 &&
          !useChatStore.getState().currentThreadId
        ) {
          // Mirror the guard every other selection write in this file uses:
          // if the layout hook (useChatPersistence) has already picked a
          // thread, don't stomp it with our independently-fetched first row.
          const selectedConversation = uiConversations[0];
          setCurrentThread(selectedConversation.id);
          // Keep the URL in sync with the auto-selection: a bare /chat URL
          // breaks Back-button restoration and loses the thread on share.
          routerRef.current.replace(
            getSelectedThreadUrl(selectedConversation.id)
          );
          console.log('[Chat] Active thread:', selectedConversation.title);
        }
        return { ok: true, threadCount: uiConversations.length };
      } catch (error: unknown) {
        if (
          threadsListWorkspaceIdRef.current !== workspaceId ||
          threadsRequestGenerationRef.current !== requestGeneration
        ) {
          return { ok: false, threadCount: 0 };
        }
        console.error('[Chat] Failed to load threads from database:', error);
        throw error;
      }
    },
    [setCurrentThread]
  );

  // CX8: fetch the next page of threads for whichever workspace the
  // sidebar list currently reflects, and append (never replace) — a stable
  // callback so it can be passed straight into ChatSidebar (React.memo, #1083).
  const loadMoreThreads = useCallback(async () => {
    const workspaceId = threadsListWorkspaceIdRef.current;
    if (!workspaceId) return;

    const nextPage = threadsPageRef.current + 1;
    const requestGeneration = threadsRequestGenerationRef.current;
    try {
      const response = await workspaceService.listWorkspaceThreads(
        workspaceId,
        {
          page: nextPage,
          limit: THREADS_PAGE_SIZE,
        }
      );

      if (
        threadsListWorkspaceIdRef.current !== workspaceId ||
        threadsRequestGenerationRef.current !== requestGeneration
      ) {
        return;
      }

      indexThreads(response.threads);

      setConversations((prev) => {
        const incoming = new Map(
          response.threads.map((thread) => [thread.id, thread] as const)
        );
        const updated = prev.map((conversation) => {
          const replacement = incoming.get(conversation.id);
          if (!replacement) return conversation;
          incoming.delete(conversation.id);
          return threadToConversation(replacement);
        });
        // Mutation-verified by useChatSession.workspaceThreads.test.tsx:
        // filtering/upserting by ID prevents offset-page overlap from
        // duplicating a thread after its activity order changes.
        return [
          ...updated,
          ...response.threads
            .filter((thread) => incoming.has(thread.id))
            .map(threadToConversation),
        ];
      });

      threadsPageRef.current = nextPage;
      setHasMoreThreads(response.has_more);
    } catch (error) {
      console.error('[Chat] Failed to load more threads:', error);
      toast.error('Could not load more threads. Please try again.');
    }
  }, []);

  // Initialize workspace and conversation from database
  useEffect(() => {
    const initGeneration = initGenerationRef.current + 1;
    initGenerationRef.current = initGeneration;
    const ownsInitialization = (): boolean =>
      initGenerationRef.current === initGeneration;

    // Watchdog: a hung request (socket open, no response) leaves init awaiting
    // forever and the UI stuck on "Initializing…". Surface a recoverable error
    // if init has not settled in time. Cleared once init resolves or unmounts.
    let settled = false;
    const watchdog = setTimeout(() => {
      if (settled || !ownsInitialization()) return;
      setInitError(
        'Connecting is taking longer than expected. Check your connection, then retry.'
      );
      setIsInitializing(false);
    }, 15000);

    const initializeFromDb = async (): Promise<void> => {
      // Initialization may overlap a first send or sidebar selection. Only the
      // selection that existed when this run began may be restored from warm
      // data; a newer synchronous store selection owns the UI.
      const selectionAtInitializationStart =
        useChatStore.getState().currentThreadId;

      if (!isAuthenticated) {
        console.log(
          '[Chat] Not authenticated, skipping database initialization'
        );
        settled = true;
        clearTimeout(watchdog);
        setIsInitializing(false);
        return;
      }

      setIsInitializing(true);
      setInitError(null);
      // The watchdog (above) may set a provisional "taking too long" error at
      // 15s while init is still running. If init then SUCCEEDS past that point,
      // `finally` must clear it — otherwise a slow-but-successful load is stuck
      // on the error screen forever. Only a genuine failure keeps the error.
      let didFail = false;
      console.log('[Chat] Initializing from database...');

      // Warm-start: the persisted selection may accelerate transcript restore,
      // but it never scopes the sidebar. The sidebar always comes from the
      // active workspace's globally ordered thread page.
      // Explicit "new chat" intent (?new=1) suppresses warm-start restore so
      // the page lands on a blank composer instead of the last thread.
      const isNewChat = searchParamsRef.current.get('new') === '1';
      const requestedThreadId = searchParamsRef.current.get('thread');
      const restoreThreadId = isNewChat
        ? null
        : (requestedThreadId ?? useChatStore.getState().currentThreadId);

      const wsPromise = workspaceService.getOrCreateDefaultWorkspace();

      try {
        const ws: Workspace = await wsPromise;
        if (!ownsInitialization()) return;
        setWorkspace(ws);

        console.log('[Chat] Workspace:', ws.name);

        const threadPagePromise = loadThreadsFromDb(ws.id);
        const messagePagePromise = restoreThreadId
          ? useChatStore.getState().loadMessages(restoreThreadId)
          : Promise.resolve();
        const [loadResult] = await Promise.all([
          threadPagePromise,
          messagePagePromise,
        ]);
        if (!ownsInitialization() || !loadResult.ok) return;

        if (restoreThreadId) {
          if (
            useChatStore.getState().currentThreadId !==
            selectionAtInitializationStart
          ) {
            isHydratedRef.current = true;
            setInitError(null);
            console.log(
              '[Chat] Warm-start data ignored after newer thread selection'
            );
            return;
          }

          let restoreThread = firstPageThreadsRef.current.find(
            (thread) => thread.id === restoreThreadId
          );
          if (!restoreThread) {
            try {
              restoreThread = await workspaceService.getThread(
                restoreThreadId,
                { includeMessages: false }
              );
            } catch (error: unknown) {
              if (!ownsInitialization()) return;

              // A late failure from the old restore target cannot turn a
              // newer first send/sidebar selection into a global init error.
              if (
                useChatStore.getState().currentThreadId !==
                selectionAtInitializationStart
              ) {
                if (requestedThreadId && isUnavailableThreadError(error)) {
                  unavailableInitialUrlThreadRef.current = requestedThreadId;
                }
                isHydratedRef.current = true;
                setInitError(null);
                return;
              }
              if (!isUnavailableThreadError(error)) throw error;

              const fallbackThreadId =
                firstPageThreadsRef.current[0]?.id ?? null;
              setCurrentThread(fallbackThreadId);
              if (requestedThreadId) {
                unavailableInitialUrlThreadRef.current = requestedThreadId;
                toast.error('That conversation is no longer available.');
              }
              routerRef.current.replace(
                fallbackThreadId
                  ? getSelectedThreadUrl(fallbackThreadId)
                  : '/chat'
              );
              isHydratedRef.current = true;
              setInitError(null);
              return;
            }
          }

          if (!ownsInitialization()) return;

          if (
            useChatStore.getState().currentThreadId !==
            selectionAtInitializationStart
          ) {
            isHydratedRef.current = true;
            setInitError(null);
            console.log(
              '[Chat] Warm-start metadata ignored after newer thread selection'
            );
            return;
          }

          indexThreads([restoreThread]);
          setConversations((prev) =>
            prev.some((conversation) => conversation.id === restoreThreadId)
              ? prev
              : upsertConversationFromThread(prev, restoreThread, [])
          );
          setMessages([]);
          setCurrentThread(restoreThreadId);
          if (!requestedThreadId) {
            routerRef.current.replace(getSelectedThreadUrl(restoreThreadId));
          }
        }

        isHydratedRef.current = true;
        setInitError(null);
        console.log('[Chat] Database initialization complete');
      } catch (error: unknown) {
        if (!ownsInitialization()) return;
        console.error('[Chat] Failed to initialize from database:', error);
        didFail = true;
        setInitError(
          error instanceof Error ? error.message : 'Failed to load chat data'
        );
      } finally {
        if (ownsInitialization()) {
          // Init settled (success or handled error): stand down the watchdog
          // so a slow-but-successful load doesn't flip to the timeout error.
          if (!settled) {
            settled = true;
            clearTimeout(watchdog);
            setIsInitializing(false);
          }
          // Clear the provisional watchdog error if init ultimately succeeded
          // — the 15s timeout may have fired before a slow load completed.
          if (!didFail) {
            setInitError(null);
          }
        }
      }
    };

    initializeFromDb();

    return () => {
      settled = true;
      clearTimeout(watchdog);
      if (initGenerationRef.current === initGeneration) {
        initGenerationRef.current += 1;
      }
      // Invalidate page responses independently of how far initialization got.
      threadsRequestGenerationRef.current += 1;
      threadsListWorkspaceIdRef.current = null;
    };
  }, [isAuthenticated, loadThreadsFromDb, setCurrentThread]);

  // React-local messages are an overlay only. Bind a just-created thread's
  // optimistic turn to its new id, and clear the overlay on every real thread
  // switch. Canonical rows render directly from Zustand.
  //
  // Exception: switching away from a thread MID-TURN parks its overlay
  // (optimistic user message + in-flight assistant state) instead of dropping
  // it — the stream deliberately keeps running across switches, and the store
  // page does not contain the just-sent user row yet. Returning to that thread
  // restores the park so the sent message never vanishes; once the turn
  // commits and the page refreshes to 'fresh', selectDisplayedMessages filters
  // the optimistic copy in favor of the canonical rows.
  const parkedMessagesRef = useRef(new Map<string, ChatPageMessage[]>());
  // Parks are only ever consumed by returning to the thread; a thread deleted
  // mid-turn never returns, so entries could accumulate for the session.
  // FIFO-cap the map — a park older than the last few switches is stale
  // anyway (the canonical rows have long since persisted).
  const MAX_PARKED_THREADS = 8;
  const parkOverlay = (threadId: string, overlay: ChatPageMessage[]): void => {
    const parked = parkedMessagesRef.current;
    parked.delete(threadId);
    parked.set(threadId, overlay);
    while (parked.size > MAX_PARKED_THREADS) {
      const oldest = parked.keys().next().value;
      if (oldest === undefined) break;
      parked.delete(oldest);
    }
  };
  useEffect(() => {
    const outgoingThreadId = localMessagesThreadIdRef.current;

    if (!activeThreadId) {
      // "New chat" mid-turn: park the outgoing thread's overlay too, so
      // returning to it restores the in-flight turn like any other switch.
      if (
        outgoingThreadId &&
        messagesRef.current.length > 0 &&
        useChatStore.getState().streamingThreadId === outgoingThreadId
      ) {
        parkOverlay(outgoingThreadId, messagesRef.current);
      }
      localMessagesThreadIdRef.current = null;
      // The reset is paired with the ref mutations above (parking the outgoing
      // overlay), so it cannot move to render without reintroducing the
      // render-phase ref writes this PR just removed. Moving it to event
      // handlers would mean enumerating every path that changes activeThreadId —
      // the fragility behind the #1121 regressions. Real fix is the same as
      // above: drop the local `messages` mirror.
      // eslint-disable-next-line react-hooks/set-state-in-effect -- see above
      setMessages([]);
      return;
    }

    if (outgoingThreadId === activeThreadId) {
      return;
    }

    if (
      outgoingThreadId &&
      messagesRef.current.length > 0 &&
      useChatStore.getState().streamingThreadId === outgoingThreadId
    ) {
      // Must go through parkOverlay, not a bare `.set` — this is the ordinary
      // switch-away path and therefore the one that actually parks a thread the
      // user may then delete mid-stream (round-3 L4). A raw set here left the
      // map uncapped, which is exactly the leak the cap exists to bound.
      parkOverlay(outgoingThreadId, messagesRef.current);
    }

    const isNewThreadHandoff =
      outgoingThreadId === null &&
      messagesRef.current.some((message) => message.source === 'optimistic');
    // Late handoff (round-3 M1): submit in a new chat, user clicks another
    // thread while createThread is in flight, then the created thread
    // activates. By that second run `outgoingThreadId` is the detour thread,
    // so the plain handoff check misses — but the live stream owns this
    // thread and the overlay runStreamTurn just rebuilt is its in-flight
    // turn. Wiping it here made the whole stream invisible with the composer
    // locked until commit.
    const isLateHandoff =
      useChatStore.getState().streamingThreadId === activeThreadId &&
      messagesRef.current.some((message) => message.source === 'optimistic');
    localMessagesThreadIdRef.current = activeThreadId;
    if (isNewThreadHandoff || isLateHandoff) {
      return;
    }
    const parked = parkedMessagesRef.current.get(activeThreadId);
    if (parked) {
      parkedMessagesRef.current.delete(activeThreadId);
      setMessages(parked);
      return;
    }
    setMessages([]);
  }, [activeThreadId]);

  return {
    // State
    conversations,
    setConversations,
    messages,
    setMessages,
    workspace,
    dbConversation,
    isInitializing,
    initError,
    isLoadingMessages: isThreadLoadPending,
    authRecoveryRoute,

    // Refs
    isHydratedRef,

    // Store bindings
    setCurrentThread,
    storeMessages: activeThreadMessages,
    addMessageToStore,
    isAuthenticated,

    // Derived
    activeThreadId,
    displayedMessages,

    // Pagination
    loadOlderMessages,
    messagePagination,
    hasMoreThreads,
    loadMoreThreads,

    // Helpers
    mapDbMessageToUiMessage,
    loadThreadsFromDb,
  };
}
