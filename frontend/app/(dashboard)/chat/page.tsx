'use client';

import { ChatSurface } from '@/components/chat/ChatSurface';
import { getSelectedThreadUrl } from '@/components/chat/shared/chatNavigation';
import { Loader2 } from 'lucide-react';
import { useRouter } from 'next/navigation';
import { Suspense, useCallback, useState } from 'react';
import { useChatSession } from '@/hooks/chat/useChatSession';
import { useChatStreaming } from '@/hooks/chat/useChatStreaming';
import { useChatThreadActions } from '@/hooks/chat/useChatThreadActions';
import { useSlashCommands } from '@/hooks/chat/useSlashCommands';
import { useCitationPanel } from '@/hooks/chat/useCitationPanel';
import { useChatDrawer } from '@/hooks/chat/useChatDrawer';
import { useChatComposerActions } from '@/hooks/chat/useChatComposerActions';

// ============================================
// MAIN PAGE COMPONENT — composition root. Every hook below owns its own
// state/service calls; this component only wires their outputs together and
// hands the whole thing to ChatSurface (Task 5.3). Route-level concerns only:
// Suspense boundary (below) and the two navigation callbacks that need
// next/navigation's router.
// ============================================

function ChatPageContent() {
  const session = useChatSession();
  const [enableRAG, setEnableRAG] = useState(true);

  const streaming = useChatStreaming({
    messages: session.messages,
    displayedMessages: session.displayedMessages,
    setMessages: session.setMessages,
    conversations: session.conversations,
    setConversations: session.setConversations,
    dbConversation: session.dbConversation,
    workspace: session.workspace,
    enableRAG,
    authRecoveryRoute: session.authRecoveryRoute,
  });

  const threadActions = useChatThreadActions({
    conversations: session.conversations,
    setConversations: session.setConversations,
    activeThreadId: session.activeThreadId,
    setCurrentThread: session.setCurrentThread,
  });

  const drawer = useChatDrawer();

  const citationPanel = useCitationPanel();

  const composerActions = useChatComposerActions({
    workspace: session.workspace,
    setInput: streaming.setInput,
    handleSubmit: streaming.handleSubmit,
    isLoading: streaming.isLoading,
    storeIsStreaming: streaming.storeIsStreaming,
    displayedMessages: session.displayedMessages,
  });

  // Slash-command execution, project-context wiring, and the /new command.
  // /retry delegates to composer actions; plain sends flow through the
  // assistant-ui runtime in ChatSurface.
  const slashCommands = useSlashCommands({
    input: streaming.input,
    setInput: streaming.setInput,
    handleSubmit: streaming.handleSubmit,
    retryLast: composerActions.retryLast,
    conversations: session.conversations,
    activeThreadId: session.activeThreadId,
    setCurrentThread: session.setCurrentThread,
    chatInputRef: streaming.chatInputRef,
  });

  const router = useRouter();
  const { activeThreadId, setCurrentThread } = session;
  const { closeDrawer } = drawer;

  // Stable across renders so ChatSidebar's React.memo holds on every composer
  // keystroke (an inline closure re-rendered the sidebar per keystroke).
  // Shared by both sidebars — closing the mobile drawer is an idempotent no-op
  // on desktop, where it's already closed and hidden.
  const handleSelectThread = useCallback(
    (id: string) => {
      if (id === activeThreadId) {
        closeDrawer();
        return;
      }
      setCurrentThread(id);
      router.push(getSelectedThreadUrl(id));
      closeDrawer();
    },
    [router, setCurrentThread, activeThreadId, closeDrawer]
  );

  return (
    <ChatSurface
      session={session}
      streaming={streaming}
      threadActions={threadActions}
      drawer={drawer}
      citationPanel={citationPanel}
      composerActions={composerActions}
      slashCommands={slashCommands}
      enableRAG={enableRAG}
      setEnableRAG={setEnableRAG}
      onSelectThread={handleSelectThread}
    />
  );
}

// Wrapper component with Suspense boundary for useSearchParams
export default function ChatPage() {
  return (
    <Suspense
      fallback={
        <div className="flex-1 flex flex-col items-center justify-center p-8">
          <div className="text-center" role="status">
            <Loader2 className="w-10 h-10 text-(--nous-sol) animate-spin mx-auto mb-4" />
            <p
              className="text-sm text-(--nous-fg-3)"
              style={{ fontFamily: 'var(--nous-font-ui)' }}
            >
              Loading chat...
            </p>
          </div>
        </div>
      }
    >
      <ChatPageContent />
    </Suspense>
  );
}
