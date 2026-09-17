'use client';

import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import type { ReactElement } from 'react';
import { usePathname } from 'next/navigation';

import { AgentFAB } from '@/components/agent-chat/AgentFAB';
import { ChatSurface } from '@/components/chat/ChatSurface';
import type { ChatPageMessage } from '@/components/chat/shared/cloudMessageView';
import { ContextRail } from '@/components/context-rail';
import { SidebarLayout } from '@/components/layout/SidebarLayout';
import { AuthProvider } from '@/hooks';
import type { ChatConversation } from '@/hooks/chat/chatTypes';
import { useChatDrawer } from '@/hooks/chat/useChatDrawer';
import type { UseChatComposerActionsReturn } from '@/hooks/chat/useChatComposerActions';
import type { UseCitationPanelReturn } from '@/hooks/chat/useCitationPanel';
import type { UseChatSessionReturn } from '@/hooks/chat/useChatSession';
import type { UseChatStreamingReturn } from '@/hooks/chat/useChatStreaming';
import type { UseChatThreadActionsReturn } from '@/hooks/chat/useChatThreadActions';
import type { UseSlashCommandsReturn } from '@/hooks/chat/useSlashCommands';
import type { Workspace } from '@/types/workspace';
import { useChatStore } from '@/store/chat-store';
import { useAgentActivityStore } from '@/stores/agentActivityStore';

const THREAD_ID = 'visual-thread';
const WORKSPACE_ID = 'visual-workspace';
const LIVE_MESSAGE_TIMESTAMP = Date.now();
const LONG_TITLE =
  'Mobile evidence synthesis across retrieval quality, source provenance, research workflows, document ingestion, citation verification, collaboration notes, and a deliberately long conversation title that must share space with every available chat action';

const completedMessages: ChatPageMessage[] = [
  {
    runtimeId: 'visual-user-1',
    source: 'canonical',
    role: 'user',
    content:
      'Compare the cited studies and explain which retrieval strategy is best supported.',
    timestamp: Date.now() - 180000,
    attachments: [
      {
        id: 'visual-attachment-1',
        document_id: 'visual-document-1',
        display_name: 'retrieval-evidence.pdf',
        document_title: 'Retrieval evidence',
        document_type: 'pdf',
        mime_type: 'application/pdf',
      },
    ],
  },
  {
    runtimeId: 'visual-assistant-1',
    source: 'canonical',
    role: 'assistant',
    content:
      'The cited studies support hybrid retrieval: lexical matching preserves exact terminology while dense retrieval improves recall for related concepts.',
    timestamp: Date.now() - 120000,
    plan: [
      {
        step: 1,
        description: 'Compare retrieval methods',
        tool: 'search_documents',
        args_hint: { query: 'retrieval quality' },
        depends_on: [],
      },
      {
        step: 2,
        description: 'Summarize evidence',
        tool: 'respond',
        args_hint: {},
        depends_on: [1],
      },
    ],
    planReasoning:
      'I compared the retrieved studies by recall, terminology coverage, and evidence quality before writing the conclusion.',
    reasoningSummary:
      'The answer weighs both retrieval strategies against the cited evidence and keeps the recommendation bounded to this corpus.',
    progressSteps: [
      { phase: 'retrieving', detail: 'Retrieved cited studies' },
      { phase: 'writing', detail: 'Compared evidence quality' },
    ],
    toolExecutions: [
      {
        id: 'visual-tool-1',
        tool: 'search_documents',
        label: 'Search documents',
        status: 'done',
        durationMs: 820,
        argsSummary: 'query: retrieval quality',
        resultSummary: '3 cited documents',
      },
    ],
    metadata: { toolsUsed: ['Search documents'], sourcesCount: 3 },
  },
];

const visualWorkspace = {
  id: WORKSPACE_ID,
  name: 'Visual QA Workspace',
} as Workspace;

function makeConversation(messages: ChatPageMessage[]): ChatConversation {
  return {
    id: THREAD_ID,
    title: LONG_TITLE,
    messages,
    createdAt: Date.now() - 300000,
    updatedAt: Date.now() - 120000,
    threadId: THREAD_ID,
    conversationId: 'visual-conversation',
    previewText: 'Hybrid retrieval preserves terminology and improves recall.',
    messageCount: messages.length,
  };
}

function FixtureShell(): ReactElement {
  const pathname = usePathname();
  const [draft, setDraft] = useState('');
  const [messages, setMessages] =
    useState<ChatPageMessage[]>(completedMessages);
  const [activeThreadId, setActiveThreadId] = useState<string | null>(
    THREAD_ID
  );
  const [showLiveReasoning, setShowLiveReasoning] = useState(false);
  const [enableRAG, setEnableRAG] = useState(true);
  const [submissions, setSubmissions] = useState(0);
  const chatInputRef = useRef<HTMLTextAreaElement>(null);
  const drawer = useChatDrawer(chatInputRef);
  const pendingUploadsRef = useRef<
    Array<{
      files: FileList;
      resolve: (outcomes: { ok: boolean; documentId?: string }[]) => void;
    }>
  >([]);

  const conversations = useMemo(() => [makeConversation(messages)], [messages]);

  useEffect(() => {
    useChatStore.setState({
      currentWorkspaceId: WORKSPACE_ID,
      currentThreadId: THREAD_ID,
      workspaces: [visualWorkspace],
    });

    const activity = useAgentActivityStore.getState();
    activity.startRun(
      THREAD_ID,
      'Evidence synthesis',
      'Compare cited retrieval studies'
    );
    activity.setPlan(THREAD_ID, [
      { text: 'Retrieve cited studies', tool: 'search_documents' },
      { text: 'Compare evidence', tool: 'respond' },
    ]);
    activity.pushToolStart(THREAD_ID, 'search_documents', 'visual-tool-1');
    activity.pushToolEnd(THREAD_ID, 'search_documents', true, 'visual-tool-1');
    activity.finishRun(THREAD_ID, 'done');

    return () => {
      useAgentActivityStore.setState({ runs: {}, currentThreadId: null });
      useChatStore.setState({
        currentThreadId: null,
        currentWorkspaceId: null,
      });
    };
  }, []);

  const settleUploads = useCallback((ok: boolean): void => {
    const pending = pendingUploadsRef.current.splice(0);
    for (const upload of pending) {
      upload.resolve(
        Array.from(upload.files).map((file, index) => ({
          ok,
          ...(ok ? { documentId: `visual-upload-${file.name}-${index}` } : {}),
        }))
      );
    }
  }, []);

  const handleAttach = useCallback(
    (files: FileList): Promise<{ ok: boolean; documentId?: string }[]> =>
      new Promise((resolve) => {
        pendingUploadsRef.current.push({ files, resolve });
        // The timeout leaves a deterministic Uploading window for browser
        // evidence. QA can settle immediately through the exposed resolver.
        window.setTimeout(() => {
          const index = pendingUploadsRef.current.findIndex(
            (entry) => entry.files === files
          );
          if (index < 0) return;
          pendingUploadsRef.current.splice(index, 1);
          resolve(
            Array.from(files).map((file, fileIndex) => ({
              ok: !file.name.toLowerCase().includes('fail'),
              ...(!file.name.toLowerCase().includes('fail')
                ? { documentId: `visual-upload-${file.name}-${fileIndex}` }
                : {}),
            }))
          );
        }, 1600);
      }),
    []
  );

  useEffect(() => {
    const visualWindow = window as typeof window & {
      __nousVisualFixture?: {
        settleUploads: (ok: boolean) => void;
      };
    };
    visualWindow.__nousVisualFixture = { settleUploads };
    return () => {
      delete visualWindow.__nousVisualFixture;
    };
  }, [settleUploads]);

  const handleSubmit = useCallback(
    async (contentOverride?: string): Promise<void> => {
      const content = (contentOverride ?? draft).trim();
      if (!content) return;
      const now = Date.now();
      setMessages((previous) => [
        ...previous,
        {
          runtimeId: `visual-user-${now}`,
          source: 'local-only',
          role: 'user',
          content,
          timestamp: now,
        },
        {
          runtimeId: `visual-assistant-${now}`,
          source: 'local-only',
          role: 'assistant',
          content:
            'Fixture response: the deterministic composition accepted this turn.',
          timestamp: now + 1,
          reasoningSummary:
            'The fixture accepted the turn without making a network model call.',
        },
      ]);
      setSubmissions((count) => count + 1);
      setDraft('');
    },
    [draft]
  );

  const threadActions = {
    renameDialog: { open: false, threadId: '', currentTitle: '', value: '' },
    setRenameDialog: () => undefined,
    deleteDialog: { open: false, threadId: '' },
    setDeleteDialog: () => undefined,
    bulkDeleteDialog: { open: false, ids: [] },
    setBulkDeleteDialog: () => undefined,
    handleRenameThread: async () => undefined,
    commitRename: async () => undefined,
    handleDeleteThread: () => undefined,
    commitDeleteThread: async () => undefined,
    handleBulkDeleteThreads: () => undefined,
    commitBulkDelete: async () => undefined,
  } as unknown as UseChatThreadActionsReturn;

  const liveMessage: ChatPageMessage = {
    runtimeId: 'visual-live-assistant',
    source: 'optimistic',
    role: 'assistant',
    content: '',
    timestamp: LIVE_MESSAGE_TIMESTAMP,
    isStreaming: true,
  };
  const fixtureMessages = showLiveReasoning
    ? [...messages, liveMessage]
    : messages;
  const session = {
    conversations,
    setConversations: () => undefined,
    messages: fixtureMessages,
    setMessages,
    workspace: visualWorkspace,
    dbConversation: null,
    isInitializing: false,
    initError: null,
    isLoadingMessages: false,
    authRecoveryRoute: { isReady: true, threadId: activeThreadId },
    isHydratedRef: { current: true },
    setCurrentThread: setActiveThreadId,
    storeMessages: null,
    addMessageToStore: () => undefined,
    isAuthenticated: true,
    activeThreadId,
    displayedMessages: activeThreadId ? fixtureMessages : [],
    loadOlderMessages: async () => undefined,
    messagePagination: {},
    hasMoreThreads: false,
    loadMoreThreads: async () => undefined,
    mapDbMessageToUiMessage: (message: never) => message,
    loadThreadsFromDb: async () => ({
      ok: true,
      threadCount: conversations.length,
    }),
  } as unknown as UseChatSessionReturn;

  const streaming = {
    input: draft,
    setInput: setDraft,
    isLoading: showLiveReasoning,
    handleSubmit,
    handleStop: () => setShowLiveReasoning(false),
    pendingConfirmation: null,
    handleConfirmation: () => undefined,
    chatInputRef,
    storeIsStreaming: showLiveReasoning,
    storeStreamingContent: showLiveReasoning
      ? 'The live provider is comparing the retrieved studies…'
      : '',
    storeIsRetrievingRag: showLiveReasoning,
    streamingThreadId: showLiveReasoning ? activeThreadId : null,
  } as unknown as UseChatStreamingReturn;

  const composerActions = {
    handleAttach,
    handleRegenerate: () => undefined,
    handleEditUserMessage: () => undefined,
  } as unknown as UseChatComposerActionsReturn;
  const slashCommands = {
    commandOutputs: [],
    handleSlashCommand: () => undefined,
    handleCommandItemAction: () => undefined,
    // ChatRuntimeProvider invokes streaming.handleSubmit after this
    // preflight returns true. Keep this hook-shaped callback side-effect free
    // so a visual send creates one deterministic turn.
    submitMessage: () => true,
    startNewChat: () => {
      setActiveThreadId(null);
      setDraft('');
    },
  } as unknown as UseSlashCommandsReturn;
  const citationPanel = {
    handleCitationClick: () => undefined,
  } as unknown as UseCitationPanelReturn;

  useEffect(() => {
    if (showLiveReasoning) {
      useChatStore.setState({
        isStreaming: true,
        streamingThreadId: THREAD_ID,
        streamingContent:
          'The live provider is comparing the retrieved studies…',
        streamingReasoning:
          'The provider is weighing recall against exact terminology.',
        streamingPlan: [
          {
            step: 1,
            description: 'Compare retrieved studies',
            tool: 'search_documents',
            args_hint: {},
            depends_on: [],
          },
        ],
        streamingPlanReasoning:
          'The planner is checking both retrieval paths before writing.',
        streamingProgress: [
          { phase: 'planning', detail: 'Planning evidence comparison' },
          { phase: 'retrieving', detail: 'Reading cited sources' },
        ],
        streamingSteps: [
          {
            id: 'visual-live-tool',
            tool: 'search_documents',
            label: 'Search documents',
            status: 'running',
          },
        ],
        isRetrievingRag: true,
        streamingPhase: 'planning',
        streamingStatusDetail: 'Comparing cited sources',
      });
    } else {
      useChatStore.setState({
        isStreaming: false,
        streamingThreadId: null,
        streamingContent: '',
        streamingReasoning: '',
        streamingPlan: [],
        streamingPlanReasoning: '',
        streamingProgress: [],
        streamingSteps: [],
        isRetrievingRag: false,
        streamingPhase: null,
        streamingStatusDetail: null,
      });
    }
  }, [showLiveReasoning]);

  return (
    <main
      data-testid="chat-mobile-controls-fixture"
      className="relative h-dvh min-w-0 overflow-hidden bg-(--nous-bg-1) text-(--nous-fg-1)"
    >
      <span data-testid="current-path" className="sr-only">
        {pathname}
      </span>
      <span data-testid="submission-count" className="sr-only">
        Submissions: {submissions}
      </span>

      <SidebarLayout
        showHeader={false}
        rightPanel={
          <div className="hidden h-full shrink-0 flex-col border-l border-(--nous-border-1) md:flex md:w-[280px] lg:w-[320px]">
            <div className="flex shrink-0 items-center justify-between gap-2 border-b border-(--nous-border-1) px-3 py-2">
              <span className="font-nous-mono text-[10px] uppercase tracking-wider text-(--nous-fg-3)">
                Fixture states
              </span>
              <button
                type="button"
                data-testid="toggle-live-reasoning"
                onClick={() => setShowLiveReasoning((visible) => !visible)}
                className="min-h-11 rounded-md border border-(--nous-border-1) px-2 text-[10px] text-(--nous-fg-2) hover:bg-(--nous-bg-2)"
              >
                {showLiveReasoning ? 'Show completed' : 'Show live reasoning'}
              </button>
            </div>
            <ContextRail
              threadId={activeThreadId}
              ragEnabled={enableRAG}
              workspaceName={visualWorkspace.name}
              workspaceId={WORKSPACE_ID}
              projectId="visual-project"
              projectName="Evidence project"
              projectFileCount={3}
              className="min-h-0 flex-1 md:flex md:w-full"
            />
          </div>
        }
      >
        <div className="h-full min-w-0">
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
            onSelectThread={setActiveThreadId}
          />
        </div>
      </SidebarLayout>

      <AgentFAB />
    </main>
  );
}

export function ChatMobileControlsFixture(): ReactElement {
  return (
    <AuthProvider>
      <FixtureShell />
    </AuthProvider>
  );
}
