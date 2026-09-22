'use client';

import { useCallback, useEffect, useState } from 'react';
import { useRouter } from 'next/navigation';

import { CitationRenderer } from '@/components/chat/CitationRenderer';
import {
  CitationChips,
  numberCitations,
} from '@/components/chat/shared/CitationChips';
import type { ChatPageMessage } from '@/components/chat/shared/cloudMessageView';
import { useChatSession } from '@/hooks/chat/useChatSession';
import { useChatStreaming } from '@/hooks/chat/useChatStreaming';
import { api } from '@/services/api-client';
import { useChatStore } from '@/store/chat-store';
import { useAuthStore } from '@/stores/authStore';
import { normalizeCitation } from '@/utils/citationNormalizer';
import type { Citation } from '@/utils/citationParser';

const FIXTURE_PATH = '/visual-test/agent-citations';

function FixtureMessage({
  message,
}: {
  message: ChatPageMessage;
}): React.JSX.Element {
  if (message.role !== 'assistant') {
    return <p data-role="user">{message.content}</p>;
  }

  const citations = message.citations ?? [];
  const citationNumbers = numberCitations(citations).indexByKey;
  return (
    <article data-role="assistant" data-testid="committed-answer">
      <CitationRenderer
        content={message.content}
        citations={citations}
        citationNumbers={citationNumbers}
      />
      <CitationChips citations={citations} />
      <CitationLocatorProbe citations={citations} />
    </article>
  );
}

function CitationLocatorProbe({
  citations,
}: {
  citations: Citation[];
}): React.JSX.Element {
  return (
    <ol className="sr-only" data-testid="citation-locators">
      {citations.map((citation, index) => (
        <li
          key={`${citation.documentId ?? 'external'}-${index}`}
          data-source-position={citation.sourcePosition}
          data-chunk-id={citation.chunkId}
          data-chunk-index={citation.chunkIndex}
          data-page-number={citation.pageNumber}
        >
          {citation.title}
        </li>
      ))}
    </ol>
  );
}

function FixtureChat(): React.JSX.Element {
  const router = useRouter();
  const session = useChatSession();
  const getThreadUrl = useCallback(
    (threadId: string) =>
      `${FIXTURE_PATH}?thread=${encodeURIComponent(threadId)}`,
    []
  );
  const navigateToThread = useCallback(
    (threadId: string) =>
      window.history.replaceState(null, '', getThreadUrl(threadId)),
    [getThreadUrl]
  );
  const streaming = useChatStreaming({
    messages: session.messages,
    displayedMessages: session.displayedMessages,
    setMessages: session.setMessages,
    conversations: session.conversations,
    setConversations: session.setConversations,
    dbConversation: session.dbConversation,
    workspace: session.workspace,
    enableRAG: true,
    navigateToThread,
    authRecoveryRoute: session.authRecoveryRoute,
  });
  const liveCitations = useChatStore((state) => state.streamingCitations).map(
    normalizeCitation
  );
  const liveContent = useChatStore((state) => state.streamingContent);
  const liveNumbers = numberCitations(liveCitations).indexByKey;

  return (
    <main
      data-testid="agent-citations-fixture"
      className="min-h-screen bg-(--nous-bg-1) p-6 text-(--nous-fg-1)"
    >
      <div className="mx-auto grid max-w-6xl gap-8 md:grid-cols-[280px_1fr]">
        <aside aria-label="Workspace threads">
          <h1 className="mb-3 text-sm font-semibold">Workspace threads</h1>
          <ol data-testid="workspace-thread-list" className="space-y-2">
            {session.conversations.map((conversation) => (
              <li
                key={conversation.id}
                data-thread-id={conversation.id}
                data-conversation-id={conversation.conversationId}
              >
                <button
                  type="button"
                  className="w-full rounded border border-(--nous-border-1) p-2 text-left"
                  onClick={() => {
                    session.setCurrentThread(conversation.id);
                    router.replace(getThreadUrl(conversation.id));
                  }}
                >
                  {conversation.title}
                </button>
              </li>
            ))}
          </ol>
          {session.hasMoreThreads && (
            <button
              type="button"
              data-testid="load-more-threads"
              className="mt-3 rounded border border-(--nous-border-1) px-3 py-2"
              onClick={() => void session.loadMoreThreads()}
            >
              Show older threads
            </button>
          )}
        </aside>

        <section aria-label="Chat transcript" className="min-w-0">
          <div data-testid="active-thread-id" className="sr-only">
            {session.activeThreadId ?? 'new'}
          </div>
          <div className="space-y-5">
            {session.displayedMessages.map((message, index) => (
              <FixtureMessage
                key={message.runtimeId ?? message.id ?? index}
                message={message}
              />
            ))}
            {streaming.storeIsStreaming && liveContent && (
              <article data-role="assistant" data-testid="live-answer">
                <CitationRenderer
                  content={liveContent}
                  citations={liveCitations}
                  citationNumbers={liveNumbers}
                  freshTail
                />
                <CitationChips citations={liveCitations} />
                <CitationLocatorProbe citations={liveCitations} />
              </article>
            )}
          </div>

          <form
            className="mt-8 flex gap-2"
            onSubmit={(event) => {
              event.preventDefault();
              void streaming.handleSubmit();
            }}
          >
            <label className="sr-only" htmlFor="fixture-message">
              Message
            </label>
            <textarea
              id="fixture-message"
              data-testid="fixture-message"
              value={streaming.input}
              onChange={(event) => streaming.setInput(event.target.value)}
              className="min-h-20 flex-1 rounded border border-(--nous-border-1) bg-(--nous-bg-2) p-3"
            />
            <button
              type="submit"
              data-testid="fixture-send"
              disabled={streaming.isLoading || streaming.storeIsStreaming}
              className="self-end rounded bg-(--nous-sol) px-4 py-2 text-white disabled:opacity-50"
            >
              Send
            </button>
          </form>
        </section>
      </div>
    </main>
  );
}

export function AgentCitationsVisualFixture(): React.ReactElement {
  const [ready, setReady] = useState(false);

  useEffect(() => {
    let active = true;
    useChatStore.getState().reset();
    api.setAuth('visual-fixture-token');
    useAuthStore.setState({ isAuthenticated: true, isLoading: false });
    queueMicrotask(() => {
      if (active) setReady(true);
    });

    return () => {
      active = false;
      api.clearAuth();
      useAuthStore.setState({ isAuthenticated: false });
    };
  }, []);

  return ready ? <FixtureChat /> : <main>Preparing fixture…</main>;
}
