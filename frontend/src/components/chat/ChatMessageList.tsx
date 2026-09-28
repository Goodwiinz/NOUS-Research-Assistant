'use client';

import React, {
  useCallback,
  useEffect,
  useLayoutEffect,
  useMemo,
  useRef,
  useState,
} from 'react';
import { AnimatePresence, motion, useReducedMotion } from 'framer-motion';
import { ArrowDown } from 'lucide-react';
import { InlineAgentSummary } from '@/components/chat/shared/InlineAgentSummary';
import { ChatBubble } from '@/components/chat/shared/ChatBubble';
import { AuiMessageByIndex } from '@/components/chat/aui/AuiMessage';
import { VirtualizedMessageList } from '@/components/chat/VirtualizedMessageList';
import { CommandOutputBubble } from '@/components/chat/CommandOutputBubble';
import type { ChatPageMessage } from '@/components/chat/shared/cloudMessageView';
import type {
  CommandAction,
  CommandOutput,
} from '@/components/chat/commandOutput';
import type { Citation } from '@/utils/citationParser';

const MESSAGE_VIRTUALIZATION_THRESHOLD = 75;

export interface ChatMessageListProps {
  messages: ChatPageMessage[];
  activeThreadId: string | null;
  isLoading: boolean;
  storeIsStreaming: boolean;
  storeStreamingContent: string;
  onRegenerate: (index: number) => void;
  /** True while a regenerate cannot be accepted (a turn is in flight). */
  retryDisabled?: boolean;
  /** Edit-and-resend a user message at the given index. */
  onEditUserMessage?: (index: number, newContent: string) => void;
  /** True while a resend cannot be accepted (a turn is in flight). The inline
   * editor stays open and disables Save rather than dropping the draft. */
  editDisabled?: boolean;
  onCitationClick: (
    citations: Citation[],
    clickedCitation: Citation,
    traceId?: string
  ) => void;
  /** Ephemeral CLI command output, rendered at the bottom of the transcript. */
  commandOutputs?: CommandOutput[];
  onCommandItemAction?: (action: CommandAction) => void;
  /** True while RAG retrieval is in flight (drives the thinking-pill label). */
  isRetrievingRag?: boolean;
  /** Pagination: load older messages. */
  onLoadOlder?: () => void;
  /** Pagination: more messages available on the server. */
  hasMore?: boolean;
  /** Pagination: currently fetching older messages. */
  isLoadingOlder?: boolean;
}

export const ChatMessageList = React.memo(function ChatMessageList({
  messages,
  activeThreadId,
  isLoading,
  storeIsStreaming,
  storeStreamingContent,
  onRegenerate,
  retryDisabled,
  onEditUserMessage,
  editDisabled,
  onCitationClick,
  commandOutputs,
  onCommandItemAction,
  isRetrievingRag,
  onLoadOlder,
  hasMore,
  isLoadingOlder,
}: ChatMessageListProps) {
  const messagesEndRef = useRef<HTMLDivElement>(null);
  const scrollContainerRef = useRef<HTMLDivElement>(null);
  const [showScrollButton, setShowScrollButton] = useState(false);
  const prefersReducedMotion = useReducedMotion();
  const scrollRafRef = useRef<number | null>(null);
  const scheduledScrollThreadIdRef = useRef<string | null>(null);
  const positionedThreadIdRef = useRef<string | null>(null);
  const scrollTickRef = useRef(false);
  const scrollTickRafRef = useRef<number | null>(null);
  const loadOlderTriggeredRef = useRef(false);
  const prevMessageCountRef = useRef(messages.length);
  const prevLastIdRef = useRef<string | undefined>(
    messages[messages.length - 1]?.runtimeId
  );
  const prevFirstIdRef = useRef<string | undefined>(messages[0]?.runtimeId);
  const prevScrollHeightRef = useRef(0);

  // Track which messages are "new" for entrance animation. Computed before the
  // auto-scroll effect so that effect can distinguish an appended message (snap
  // to bottom) from a prepended older batch (preserve the user's scroll anchor).
  // "New" = length grew AND the tail id changed (append); a prepended older
  // batch grows the length but keeps the same tail id, so it is not "new".
  const currentLastId = messages[messages.length - 1]?.runtimeId;
  const isNewMessage =
    messages.length > prevMessageCountRef.current &&
    currentLastId !== prevLastIdRef.current;
  // The user's own send is the one append the "scrolled away" guard must not
  // suppress: bailing out of the auto-scroll made every send look like it did
  // nothing (the message stacked below the fold) once the user had scrolled
  // up. Content the user did NOT initiate — streaming tokens, background
  // growth — still respects the guard.
  const isOwnNewMessage =
    isNewMessage && messages[messages.length - 1]?.role === 'user';

  // Prepending an older page grows the content above the viewport; without
  // compensation the messages the user is reading jump down by the added
  // height. Detect prepend (length grew, tail id unchanged, head id changed)
  // and restore the visual anchor by the scrollHeight delta. Layout effect so
  // the correction lands before paint — no flicker.
  useLayoutEffect(() => {
    const container = scrollContainerRef.current;
    if (!container) return;
    const firstId = messages[0]?.runtimeId;
    const isPrepend =
      messages.length > prevMessageCountRef.current &&
      currentLastId === prevLastIdRef.current &&
      firstId !== prevFirstIdRef.current;
    if (isPrepend) {
      container.scrollTop +=
        container.scrollHeight - prevScrollHeightRef.current;
    }
    prevScrollHeightRef.current = container.scrollHeight;
    prevFirstIdRef.current = firstId;
  }, [messages, currentLastId]);

  // The scroll container survives cached thread switches. Its transient UI
  // guards must not: a previous thread's "scrolled away" state would block the
  // new thread's initial positioning, and its load-older guard could suppress
  // pagination in the newly selected thread.
  useEffect(() => {
    if (scrollTickRafRef.current !== null) {
      cancelAnimationFrame(scrollTickRafRef.current);
      scrollTickRafRef.current = null;
    }
    scrollTickRef.current = false;
    loadOlderTriggeredRef.current = false;
    setShowScrollButton(false);
  }, [activeThreadId]);

  // Throttled auto-scroll: at most one scrollIntoView per animation frame.
  // `storeStreamingContent` is in the deps so the view follows tokens as the
  // answer streams, but the rAF guard coalesces the per-token re-renders into
  // a single scroll per frame — the follow is restored without the per-token
  // jank. Skipped entirely once the user scrolls up (showScrollButton), so we
  // never fight a user reading history — except for the user's own new
  // message, which always pulls the view back down (and clears the guard).
  useEffect(() => {
    if (showScrollButton && !isOwnNewMessage) return;
    if (!activeThreadId || messages.length === 0) return;
    if (isOwnNewMessage) setShowScrollButton(false);

    // A selected thread's first page should appear at its newest message
    // immediately. Smooth-scrolling that initial batch makes the UI visibly
    // traverse the whole transcript on every sidebar click. Keep smooth
    // following only after this thread has already been positioned.
    const isInitialThreadPosition =
      positionedThreadIdRef.current !== activeThreadId;

    // A rapid switch can happen before the previous frame runs. Replace that
    // stale scheduled scroll so it cannot position the newly selected thread
    // using the previous thread's lifecycle.
    if (scrollRafRef.current !== null) {
      if (scheduledScrollThreadIdRef.current === activeThreadId) return;
      cancelAnimationFrame(scrollRafRef.current);
      scrollRafRef.current = null;
    }
    scheduledScrollThreadIdRef.current = activeThreadId;
    scrollRafRef.current = requestAnimationFrame(() => {
      messagesEndRef.current?.scrollIntoView({
        behavior: isInitialThreadPosition ? 'auto' : 'smooth',
      });
      positionedThreadIdRef.current = activeThreadId;
      scheduledScrollThreadIdRef.current = null;
      scrollRafRef.current = null;
    });
  }, [
    activeThreadId,
    messages.length,
    isNewMessage,
    isOwnNewMessage,
    storeIsStreaming,
    storeStreamingContent,
    commandOutputs,
    showScrollButton,
  ]);

  useEffect(() => {
    return () => {
      if (scrollRafRef.current !== null) {
        cancelAnimationFrame(scrollRafRef.current);
      }
      scheduledScrollThreadIdRef.current = null;
      // Also cancel a pending scroll-event throttle frame so its callback
      // (setShowScrollButton / onLoadOlder) can't fire after unmount.
      if (scrollTickRafRef.current !== null) {
        cancelAnimationFrame(scrollTickRafRef.current);
      }
    };
  }, []);

  useEffect(() => {
    prevMessageCountRef.current = messages.length;
    prevLastIdRef.current = currentLastId;
  }, [messages.length, currentLastId]);

  const handleScroll = useCallback(() => {
    if (scrollTickRef.current) return;
    scrollTickRef.current = true;
    scrollTickRafRef.current = requestAnimationFrame(() => {
      scrollTickRef.current = false;
      scrollTickRafRef.current = null;
      const container = scrollContainerRef.current;
      if (!container) return;
      const { scrollTop, scrollHeight, clientHeight } = container;
      const isNearBottom = scrollHeight - scrollTop - clientHeight < 100;
      setShowScrollButton(!isNearBottom && messages.length > 0);

      // Auto-trigger load older when scrolled near the top
      if (
        onLoadOlder &&
        hasMore &&
        !isLoadingOlder &&
        !loadOlderTriggeredRef.current &&
        scrollTop < 150 &&
        messages.length > 0
      ) {
        loadOlderTriggeredRef.current = true;
        onLoadOlder();
      }
      // Reset the trigger guard when scrolling back down
      if (scrollTop > 300) {
        loadOlderTriggeredRef.current = false;
      }
    });
  }, [messages.length, onLoadOlder, hasMore, isLoadingOlder]);

  const scrollToBottom = useCallback(() => {
    messagesEndRef.current?.scrollIntoView({
      behavior: prefersReducedMotion ? 'auto' : 'smooth',
    });
    setShowScrollButton(false);
  }, [prefersReducedMotion]);

  const lastIndex = messages.length - 1;
  const isVirtualized = messages.length > MESSAGE_VIRTUALIZATION_THRESHOLD;
  // Phase-aware "thinking" label (the pill only shows before any token arrives).
  const thinkingLabel = isRetrievingRag ? 'Reading sources' : 'Thinking';

  // Memoize the committed message list so it doesn't re-map on every
  // streaming token. The in-flight turn streams via its own placeholder
  // message (AuiStreamingBody reads the store directly), so this memo's
  // dependencies are only things that actually change the committed list.
  const renderedMessages = useMemo(
    () =>
      messages.map((message, index) => {
        const isLast = index === lastIndex;
        const shouldAnimate = isLast && isNewMessage;

        const bubble = (
          <>
            {message.role === 'assistant' && isLast && !storeIsStreaming && (
              <InlineAgentSummary threadId={activeThreadId} />
            )}
            {isLast &&
            isLoading &&
            !storeIsStreaming &&
            message.role === 'assistant' ? (
              <ChatBubble
                message={message}
                index={index}
                modelName="NOUS"
                isTyping
                onRetry={() => onRegenerate(index)}
                onCitationClick={onCitationClick}
                thinkingLabel={thinkingLabel}
              />
            ) : (
              <AuiMessageByIndex
                index={index}
                message={message}
                onRetry={
                  message.role === 'assistant'
                    ? () => onRegenerate(index)
                    : undefined
                }
                retryDisabled={retryDisabled}
                onEdit={
                  message.role === 'user' && onEditUserMessage
                    ? (content) => onEditUserMessage(index, content)
                    : undefined
                }
                editDisabled={editDisabled}
                onCitationClick={onCitationClick}
              />
            )}
          </>
        );

        if (shouldAnimate) {
          return (
            <motion.div
              key={message.runtimeId}
              data-runtime-id={message.runtimeId}
              data-persisted-id={message.id}
              initial={{ opacity: 0, y: 16 }}
              animate={{ opacity: 1, y: 0 }}
              transition={{
                duration: 0.3,
                ease: [0.25, 0.46, 0.45, 0.94],
              }}
            >
              {bubble}
            </motion.div>
          );
        }

        return (
          <div
            key={message.runtimeId}
            data-runtime-id={message.runtimeId}
            data-persisted-id={message.id}
          >
            {bubble}
          </div>
        );
      }),
    [
      messages,
      lastIndex,
      isNewMessage,
      storeIsStreaming,
      activeThreadId,
      isLoading,
      onRegenerate,
      retryDisabled,
      onEditUserMessage,
      editDisabled,
      onCitationClick,
      thinkingLabel,
    ]
  );

  return (
    <div className="flex-1 relative min-h-0">
      <div
        ref={scrollContainerRef}
        onScroll={handleScroll}
        role="region"
        aria-label="Conversation transcript"
        className="h-full overflow-y-auto overflow-x-hidden nous-scrollbar"
      >
        <div className="max-w-(--nous-chat-col) mx-auto pt-3 sm:pt-4 px-2 sm:px-4 pb-4 sm:pb-6">
          {/* Load older messages indicator */}
          {!isVirtualized &&
            hasMore &&
            !isLoadingOlder &&
            messages.length > 0 && (
              <div className="flex justify-center py-2">
                <button
                  onClick={onLoadOlder}
                  className="text-xs font-medium text-(--nous-fg-2) hover:text-(--nous-sol) transition-colors"
                >
                  Load older messages
                </button>
              </div>
            )}
          {!isVirtualized && isLoadingOlder && (
            <div className="flex justify-center py-2">
              <span className="text-xs font-medium text-(--nous-fg-2)">
                Loading older messages...
              </span>
            </div>
          )}

          {/* Key the message-row subtree by thread id so a thread switch
              MOUNTS a fresh row tree instead of reconciling the previous
              thread's index-addressed MessageByIndex fibers against the new
              thread. Rapid switching otherwise interleaves partial commits and
              trips React's reconciler ("Tried to unmount a fiber that is
              already unmounted") — a reconciler-internal error the #1096
              MessageByIndexBoundary cannot catch. A clean remount also drops
              the old thread's store subscriptions in one unit, shrinking the
              window for the useClientLookup torn read (which the boundary still
              backstops). Appends within a thread keep the same key — no
              remount, no flicker. */}
          <React.Fragment key={`rows-${activeThreadId ?? 'new'}`}>
            {isVirtualized ? (
              <VirtualizedMessageList
                messages={messages}
                activeThreadId={activeThreadId}
                isLoading={isLoading}
                storeIsStreaming={storeIsStreaming}
                onRegenerate={onRegenerate}
                retryDisabled={retryDisabled}
                onEditUserMessage={onEditUserMessage}
                editDisabled={editDisabled}
                onCitationClick={onCitationClick}
                isRetrievingRag={isRetrievingRag}
                onLoadOlder={onLoadOlder}
                hasMore={hasMore}
                isLoadingOlder={isLoadingOlder}
              />
            ) : (
              renderedMessages
            )}
          </React.Fragment>

          {/* Ephemeral CLI command output (not persisted, not sent to agent) */}
          {commandOutputs?.map((output) => (
            <CommandOutputBubble
              key={output.id}
              output={output}
              onItemAction={(action) => onCommandItemAction?.(action)}
            />
          ))}
          <div ref={messagesEndRef} />
        </div>
      </div>

      {/* Scroll to bottom button */}
      <AnimatePresence>
        {showScrollButton && (
          <div className="absolute bottom-4 left-1/2 -translate-x-1/2 z-50 pointer-events-none">
            <motion.button
              initial={{ opacity: 0, y: 10, scale: 0.9 }}
              animate={{ opacity: 1, y: 0, scale: 1 }}
              exit={{ opacity: 0, y: 10, scale: 0.9 }}
              onClick={scrollToBottom}
              aria-label="Scroll to newest messages"
              className="flex items-center justify-center min-w-11 min-h-11 sm:w-auto sm:h-auto sm:gap-2 sm:px-4 sm:py-2 rounded-full bg-(--nous-sol) text-(--nous-erebus) text-xs font-semibold shadow-md hover:shadow-lg transition-all pointer-events-auto"
              style={{ fontFamily: 'var(--nous-font-ui)' }}
            >
              <ArrowDown className="w-4 h-4" />
              <span className="hidden sm:inline">New messages</span>
            </motion.button>
          </div>
        )}
      </AnimatePresence>
    </div>
  );
});
