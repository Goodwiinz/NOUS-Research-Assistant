'use client';

import { useEffect, useState } from 'react';
import { usePathname } from 'next/navigation';

import { AgentFAB } from '@/components/agent-chat/AgentFAB';
import { ChatHeader } from '@/components/chat/ChatHeader';
import { ChatInput } from '@/components/chat/ChatInput';
import { ChatRuntimeProvider } from '@/components/chat/aui/ChatRuntimeProvider';
import { useAgentChatStore } from '@/store/agentChatStore';

const LONG_TITLE =
  'Mobile evidence synthesis across retrieval quality, source provenance, research workflows, document ingestion, citation verification, collaboration notes, and a deliberately long conversation title that must share space with every available chat action';
const INITIAL_DRAFT =
  'Compare the cited studies and explain which retrieval strategy is best supported.';
const HEADER_MESSAGES = [
  {
    role: 'user',
    content: 'Compare the cited studies.',
    timestamp: 1,
  },
];

export function ChatMobileControlsFixture(): React.ReactElement {
  const pathname = usePathname();
  const [draft, setDraft] = useState(INITIAL_DRAFT);
  const [submissions, setSubmissions] = useState(0);

  useEffect(() => {
    useAgentChatStore.getState().reset();
  }, []);

  return (
    <main
      data-testid="chat-mobile-controls-fixture"
      className="flex h-dvh min-w-0 flex-col overflow-hidden bg-(--nous-bg-1) text-(--nous-fg-1)"
    >
      <span data-testid="current-path" className="sr-only">
        {pathname}
      </span>
      <span data-testid="submission-count" className="sr-only">
        Submissions: {submissions}
      </span>

      <header data-testid="chat-mobile-header" className="min-w-0 shrink-0">
        <ChatHeader
          chatTitle={LONG_TITLE}
          messages={HEADER_MESSAGES}
          threadId="visual-thread"
          onMobileSidebarToggle={() => undefined}
          onCopyAll={() => undefined}
        />
      </header>

      <section
        aria-label="Fixture transcript"
        className="min-h-0 flex-1 overflow-hidden px-4 py-3 font-nous-body text-sm text-(--nous-fg-2)"
      >
        The composer below uses the production chat controls and responsive
        styles.
      </section>

      <ChatRuntimeProvider
        messages={[]}
        isRunning={false}
        onSend={() => undefined}
        onCancel={() => undefined}
      >
        <ChatInput
          value={draft}
          onChange={setDraft}
          onSubmit={() => {
            setSubmissions((count) => count + 1);
            setDraft('');
            return false;
          }}
          isLoading={false}
          enableRAG
          onRAGToggle={() => undefined}
        />
      </ChatRuntimeProvider>

      <AgentFAB />
    </main>
  );
}
