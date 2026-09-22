'use client';

import {
  useCallback,
  useEffect,
  useRef,
  useState,
  type ReactElement,
} from 'react';
import { AnimatePresence, motion } from 'framer-motion';

// ChatInput resolves through the barrel (not its own module path) — the
// existing ChatPage.*.test.tsx suite mocks '@/components/chat' at exactly
// this specifier.
import { ChatInput } from '@/components/chat';
import { ChatDialogs } from '@/components/chat/ChatDialogs';
import { ChatHeader } from '@/components/chat/ChatHeader';
import { ChatSidebar } from '@/components/chat/ChatSidebar';
import { ChatTranscriptState } from '@/components/chat/ChatTranscriptState';
import { ChatRuntimeProvider } from '@/components/chat/aui/ChatRuntimeProvider';
import type { UseChatSessionReturn } from '@/hooks/chat/useChatSession';
import {
  confirmationBelongsToThread,
  type PendingConfirmation,
  type UseChatStreamingReturn,
} from '@/hooks/chat/useChatStreaming';
import type { UseChatThreadActionsReturn } from '@/hooks/chat/useChatThreadActions';
import type { UseChatDrawerReturn } from '@/hooks/chat/useChatDrawer';
import type { UseCitationPanelReturn } from '@/hooks/chat/useCitationPanel';
import type { UseChatComposerActionsReturn } from '@/hooks/chat/useChatComposerActions';
import { appendToDraft } from '@/components/chat/shared/appendToDraft';
import { QuoteToolbar } from '@/components/chat/shared/QuoteToolbar';
import type { UseSlashCommandsReturn } from '@/hooks/chat/useSlashCommands';

interface ChatSurfaceProps {
  session: UseChatSessionReturn;
  streaming: UseChatStreamingReturn;
  threadActions: UseChatThreadActionsReturn;
  drawer: UseChatDrawerReturn;
  citationPanel: UseCitationPanelReturn;
  composerActions: UseChatComposerActionsReturn;
  slashCommands: UseSlashCommandsReturn;
  enableRAG: boolean;
  setEnableRAG: (enabled: boolean) => void;
  /** Route-level: uses next/navigation's router, so it's built in page.tsx. */
  onSelectThread: (id: string) => void;
}

/**
 * The chat route's visible shell (Task 5.3): sidebar (mobile drawer +
 * desktop), the aui-runtime-wrapped header/transcript/composer column, the
 * citation panel, and thread dialogs. Pure composition over explicit
 * props — the hooks themselves (session/streaming/etc.) still own all
 * state and service calls; this component only renders and wires callbacks.
 */
export function ChatSurface({
  session,
  streaming,
  threadActions,
  drawer,
  citationPanel,
  composerActions,
  slashCommands,
  enableRAG,
  setEnableRAG,
  onSelectThread,
}: ChatSurfaceProps): ReactElement {
  const {
    conversations,
    workspace,
    activeThreadId,
    displayedMessages,
    isAuthenticated,
    isInitializing,
    initError,
    isLoadingMessages,
    hasMoreThreads,
    loadMoreThreads,
    loadOlderMessages,
    messagePagination,
  } = session;

  const {
    input,
    setInput,
    isLoading,
    handleSubmit,
    handleStop,
    pendingConfirmation,
    handleConfirmation,
    chatInputRef,
    storeIsStreaming,
    storeStreamingContent,
    storeIsRetrievingRag,
    streamingThreadId,
  } = streaming;

  const {
    renameDialog,
    setRenameDialog,
    deleteDialog,
    setDeleteDialog,
    bulkDeleteDialog,
    setBulkDeleteDialog,
    handleRenameThread,
    commitRename,
    handleDeleteThread,
    commitDeleteThread,
    handleBulkDeleteThreads,
    commitBulkDelete,
  } = threadActions;

  const {
    isOpen: mobileSidebarOpen,
    closeDrawer,
    toggleDrawer,
    drawerRef,
    handleDrawerKeyDown,
  } = drawer;

  const { handleCitationClick } = citationPanel;

  const { handleAttach, handleRegenerate, handleEditUserMessage } =
    composerActions;

  const {
    commandOutputs,
    handleSlashCommand,
    handleCommandItemAction,
    submitMessage,
    startNewChat,
  } = slashCommands;

  // CX5: storeIsStreaming is intentionally global (single-flight — the
  // composer stays blocked on the raw flag regardless of which thread is
  // displayed). This derived flag is ONLY for streaming-derived UI that must
  // not bleed into a thread that isn't actually streaming — a background
  // turn on thread A rendering into thread B's transcript.
  const isStreamingThisThread =
    storeIsStreaming && streamingThreadId === activeThreadId;

  // Only the thread that owns the pending confirmation shows the banner or
  // has its input locked — pendingConfirmation itself survives navigation so
  // returning to the owning thread re-shows it.
  const activeConfirmation = confirmationBelongsToThread(
    pendingConfirmation,
    activeThreadId
  )
    ? pendingConfirmation
    : null;

  const isBusy = isLoading || storeIsStreaming || !!activeConfirmation;

  // The first send starts before the server has assigned a thread id. Adopt
  // that id into the current runtime identity while an optimistic message is
  // present; changing the provider key here would remount it mid-turn. Real
  // navigation still replaces the identity and resets the runtime.
  const [runtimeIdentity, setRuntimeIdentity] = useState<{
    threadId: string | null;
    key: string;
    generation: number;
  }>({
    threadId: activeThreadId,
    key: activeThreadId ?? 'new:0',
    generation: 0,
  });
  let runtimeKey = runtimeIdentity.key;
  if (runtimeIdentity.threadId !== activeThreadId) {
    const isNewThreadAdoption =
      runtimeIdentity.threadId === null &&
      activeThreadId !== null &&
      displayedMessages.some((message) => message.source === 'optimistic');

    const nextGeneration = isNewThreadAdoption
      ? runtimeIdentity.generation
      : runtimeIdentity.generation + 1;
    const nextIdentity = {
      threadId: activeThreadId,
      key: isNewThreadAdoption
        ? runtimeIdentity.key
        : (activeThreadId ?? `new:${nextGeneration}`),
      generation: nextGeneration,
    };
    setRuntimeIdentity(nextIdentity);
    runtimeKey = nextIdentity.key;
  }

  // Export/copy-all must reflect the DISPLAYED thread's snapshot, so it may
  // only be blocked while THIS thread's transcript is still growing.
  // `isStreamingThisThread` covers the stream itself. `isLoading` does not:
  // it is session-scoped (one streaming hook per surface) and stays true for
  // the whole turn INCLUDING the pre-first-token window, so a turn sent on
  // thread A wrongly disabled export on thread B once the user switched.
  // `streamingThreadId` is the authoritative owner of an in-flight turn, but
  // it is only stamped once the SSE turn actually starts — it is still null
  // during the pre-first-token window (thread creation + connect). So the
  // owner of the loading state is `streamingThreadId` when it exists, and
  // otherwise the thread that was displayed at the isLoading false→true edge
  // (the thread that initiated the send).
  const prevIsLoadingRef = useRef(false);
  const [sendInitiatorThreadId, setSendInitiatorThreadId] = useState<
    string | null
  >(null);
  useEffect(() => {
    // Only the isLoading EDGE matters: switching threads mid-turn must not
    // re-point the initiator at whatever is now displayed.
    if (isLoading !== prevIsLoadingRef.current) {
      prevIsLoadingRef.current = isLoading;
      setSendInitiatorThreadId(isLoading ? activeThreadId : null);
    }
  }, [isLoading, activeThreadId]);
  const loadingThreadId = streamingThreadId ?? sendInitiatorThreadId;
  const isLoadingThisThread = isLoading && loadingThreadId === activeThreadId;
  const isTranscriptGrowing = isStreamingThisThread || isLoadingThisThread;

  // While the session can't accept a turn (unauthenticated, still
  // initializing, or failed to initialize) every submission path must be
  // inert — the transcript already renders the matching state, but the
  // composer callbacks used to fire anyway and start work against a session
  // that can't own it.
  const isSessionInteractive = isAuthenticated && !isInitializing && !initError;

  // An edit resend goes through the same single-flight path as a normal send:
  // handleEditUserMessage bails while a turn is in flight, so the editor must
  // KNOW that up front instead of closing on a save nothing will act on.
  const canSubmitEdit = isSessionInteractive && !isBusy;

  const handleStarterSelect = useCallback(
    (prompt: string): void => {
      // A starter is an editable prompt, not an implicit send. Keep anything
      // already typed and put focus back in the composer so the user can edit
      // before deciding what to send.
      setInput((current) => (current ? current : prompt));
      chatInputRef.current?.focus();
    },
    [chatInputRef, setInput]
  );

  // F2 (chat-bug-hunt 2026-08-23): the runtime key encodes THREAD IDENTITY
  // ONLY. It used to also encode a hydration phase (`new:empty` →
  // `new:hydrated`), so the first optimistic message landing on a brand-new
  // chat flipped the key mid-turn and React unmounted/remounted the whole
  // ChatRuntimeProvider subtree — dropping composer focus to <body> and
  // resetting internal runtime state on every new-chat first send.
  // Thread switches still remount (activeThreadId changes); within one
  // thread nothing needs a reset — ChatRuntimeProvider is a pure projection
  // of its props via useExternalStoreRuntime.

  // Listen for populate-chat-input events. Follow-up Suggestions send a bare
  // string (replace); the artifact panel's "Cite" sends {text, mode:'append'}
  // so citing never clobbers a draft the user is typing.
  useEffect(() => {
    const handlePopulateChatInput = (
      event: CustomEvent<string | { text: string; mode?: 'append' | 'replace' }>
    ): void => {
      const detail = event.detail;
      if (!detail) return;
      if (typeof detail === 'string') {
        setInput(detail);
      } else if (detail.text) {
        if (detail.mode === 'append') {
          setInput((cur) => appendToDraft(cur, detail.text));
        } else {
          setInput(detail.text);
        }
      } else {
        return;
      }
      // Focus the textarea after populating
      chatInputRef.current?.focus();
    };

    window.addEventListener(
      'populate-chat-input',
      handlePopulateChatInput as EventListener
    );
    return () => {
      window.removeEventListener(
        'populate-chat-input',
        handlePopulateChatInput as EventListener
      );
    };
  }, [setInput, chatInputRef]);

  // Return focus to the composer when a HITL confirmation resolves — the
  // in-band Approve/Deny part just unmounted, so without this focus would drop
  // to <body>. (The approval part handles its own Approve autofocus on appear.)
  const prevActiveConfirmationRef = useRef<PendingConfirmation | null>(null);
  useEffect(() => {
    const had = prevActiveConfirmationRef.current;
    if (!activeConfirmation && had) {
      chatInputRef.current?.focus();
    }
    prevActiveConfirmationRef.current = activeConfirmation;
  }, [activeConfirmation, chatInputRef]);

  return (
    <div className="flex h-full w-full overflow-hidden bg-(--nous-bg-1)">
      {/* Mobile sidebar backdrop + drawer */}
      <AnimatePresence>
        {mobileSidebarOpen && (
          <div className="fixed inset-0 z-50 xl:hidden">
            <motion.div
              className="absolute inset-0 bg-(--nous-erebus)/50"
              initial={{ opacity: 0 }}
              animate={{ opacity: 1 }}
              exit={{ opacity: 0 }}
              transition={{ duration: 0.2 }}
              onClick={closeDrawer}
            />
            <motion.div
              ref={drawerRef}
              tabIndex={-1}
              role="dialog"
              aria-modal="true"
              aria-label="Chat history"
              onKeyDown={handleDrawerKeyDown}
              initial={{ x: '-100%' }}
              animate={{ x: 0 }}
              exit={{ x: '-100%' }}
              transition={{ duration: 0.25, ease: [0.16, 1, 0.3, 1] }}
              className="absolute left-0 top-0 bottom-0 w-[min(280px,85vw)] bg-(--nous-bg-2) border-r border-(--nous-border-1) shadow-(--nous-shadow-lg) outline-hidden"
            >
              <ChatSidebar
                conversations={conversations}
                activeId={activeThreadId}
                onSelect={onSelectThread}
                onNew={() => {
                  startNewChat();
                  closeDrawer();
                }}
                onRename={handleRenameThread}
                onDelete={handleDeleteThread}
                onBulkDelete={handleBulkDeleteThreads}
                currentWorkspace={workspace}
                hasMoreThreads={hasMoreThreads}
                onLoadMoreThreads={loadMoreThreads}
              />
            </motion.div>
          </div>
        )}
      </AnimatePresence>

      {/* Desktop sidebar */}
      <div className="hidden xl:block h-full shrink-0">
        <ChatSidebar
          conversations={conversations}
          activeId={activeThreadId}
          onSelect={onSelectThread}
          onNew={startNewChat}
          onRename={handleRenameThread}
          onDelete={handleDeleteThread}
          onBulkDelete={handleBulkDeleteThreads}
          currentWorkspace={workspace}
          hasMoreThreads={hasMoreThreads}
          onLoadMoreThreads={loadMoreThreads}
        />
      </div>

      {/* Main Chat Area */}
      <div className="flex-1 flex flex-col relative h-full min-w-0 overflow-hidden">
        <ChatRuntimeProvider
          key={runtimeKey}
          messages={displayedMessages}
          isRunning={isBusy}
          isSendDisabled={!!activeConfirmation || !isSessionInteractive}
          onSend={(text, attachmentIds) => {
            if (isSessionInteractive) {
              void handleSubmit(text, undefined, undefined, attachmentIds);
            }
          }}
          onCancel={handleStop}
          onApproval={handleConfirmation}
        >
          <ChatHeader
            messages={displayedMessages}
            threadId={activeThreadId}
            chatTitle={
              conversations.find((c) => c.id === activeThreadId)?.title ||
              'Chat'
            }
            onCopyAll={() => {
              const text = displayedMessages
                .map((m) => `[${m.role}] ${m.content}`)
                .join('\n\n');
              navigator.clipboard.writeText(text).catch(() => {});
            }}
            isStreaming={isTranscriptGrowing}
            onMobileSidebarToggle={toggleDrawer}
          />

          <ChatTranscriptState
            isAuthenticated={isAuthenticated}
            isInitializing={isInitializing}
            initError={initError}
            isLoadingMessages={isLoadingMessages}
            activeThreadId={activeThreadId}
            displayedMessages={displayedMessages}
            commandOutputs={commandOutputs}
            isStreamingThisThread={isStreamingThisThread}
            isLoading={isLoading}
            storeStreamingContent={storeStreamingContent}
            storeIsRetrievingRag={storeIsRetrievingRag}
            onPromptSelect={handleStarterSelect}
            onRegenerate={(index) => {
              if (isSessionInteractive) handleRegenerate(index);
            }}
            retryDisabled={!isSessionInteractive || isBusy}
            onEditUserMessage={(index, content) => {
              if (canSubmitEdit) handleEditUserMessage(index, content);
            }}
            editDisabled={!canSubmitEdit}
            onCitationClick={handleCitationClick}
            onCommandItemAction={handleCommandItemAction}
            onLoadOlder={loadOlderMessages}
            messagePagination={messagePagination}
          />

          {/* Raises a Quote action on a selection inside a committed
              answer; mounted once rather than per message. */}
          <QuoteToolbar />

          <ChatInput
            value={input}
            onChange={setInput}
            onSubmit={(attachmentIds) => {
              if (isSessionInteractive) {
                return submitMessage(attachmentIds);
              } else {
                console.warn('[Chat] Send ignored: session not interactive', {
                  isAuthenticated,
                  isInitializing,
                  initError,
                });
                return false;
              }
            }}
            isLoading={isBusy}
            disabled={!isSessionInteractive}
            enableRAG={enableRAG}
            onRAGToggle={setEnableRAG}
            inputRef={chatInputRef}
            onAttach={handleAttach}
            onCommand={(id) => {
              if (isSessionInteractive) handleSlashCommand(id);
            }}
          />
        </ChatRuntimeProvider>
      </div>

      <ChatDialogs
        renameDialog={renameDialog}
        setRenameDialog={setRenameDialog}
        commitRename={commitRename}
        deleteDialog={deleteDialog}
        setDeleteDialog={setDeleteDialog}
        commitDeleteThread={commitDeleteThread}
        bulkDeleteDialog={bulkDeleteDialog}
        setBulkDeleteDialog={setBulkDeleteDialog}
        commitBulkDelete={commitBulkDelete}
      />
    </div>
  );
}
