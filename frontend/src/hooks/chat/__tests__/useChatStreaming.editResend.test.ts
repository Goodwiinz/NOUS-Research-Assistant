// Durable edit-and-resend: the wire contract.
//
// PR #1313's edit only truncated the REQUEST — the server kept the old answer
// and every later turn, so a reload contradicted what the user saw and the
// model could still see the replaced prompt. The turn now names the message it
// supersedes, and the server tombstones it.
//
// Two properties this pins down:
//  1. An edit sends `supersedes_client_message_id` = the edited turn's
//     persisted cmid, AND a FRESH `client_message_id` on the new user turn —
//     reusing the old id would hit the server's ON CONFLICT dedup and be
//     silently dropped.
//  2. A normal send omits the field entirely (not null, not empty string).
import { describe, expect, it, vi, beforeEach } from 'vitest';
import { act, renderHook, waitFor } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import {
  createElement,
  useState,
  type ReactElement,
  type ReactNode,
} from 'react';
import {
  selectDisplayedMessages,
  type ChatPageMessage,
} from '@/components/chat/shared/cloudMessageView';
import { makeChatPageMessage } from '@/test/chatMessageFactory';
import { useChatStore } from '@/store/chat-store';
import type { ChatMessage } from '@/types/workspace';

function wrapper({ children }: { children: ReactNode }): ReactElement {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  return createElement(QueryClientProvider, { client }, children);
}

vi.mock('next/navigation', () => ({
  useRouter: () => ({ push: vi.fn(), replace: vi.fn() }),
  useSearchParams: () => new URLSearchParams(),
}));

const streamMessageMock = vi.fn();
vi.mock('@/services/agentChatService', () => ({
  agentChatService: {
    streamMessage: (...args: unknown[]) => streamMessageMock(...args),
    streamConfirm: vi.fn(),
    resumeStream: vi.fn().mockResolvedValue({ status: 'idle' }),
  },
}));

vi.mock('@/services/workspaceService', () => ({
  workspaceService: {
    createThread: vi.fn(),
    createMessage: vi.fn().mockResolvedValue({ id: 'db-msg-1' }),
    listMessages: vi.fn().mockResolvedValue({ messages: [], has_more: false }),
  },
}));

const EDITED_CMID = '11111111-1111-4111-8111-111111111111';

const history: ChatPageMessage[] = [
  makeChatPageMessage({
    id: 'first-user',
    role: 'user',
    content: 'first turn',
    timestamp: 1,
    clientMessageId: EDITED_CMID,
  }),
  makeChatPageMessage({
    id: 'first-assistant',
    role: 'assistant',
    content: 'first reply',
    timestamp: 2,
  }),
];

const replacementStoreRows = [
  {
    id: 'before-user',
    client_message_id: 'runtime-before-user',
    role: 'user',
    content: 'Earlier question',
  },
  {
    id: 'before-assistant',
    client_message_id: 'runtime-before-assistant',
    role: 'assistant',
    content: 'Earlier answer',
  },
  {
    id: 'old-user',
    client_message_id: EDITED_CMID,
    role: 'user',
    content: 'Original question',
  },
  {
    id: 'old-assistant',
    client_message_id: 'runtime-old-assistant',
    role: 'assistant',
    content: 'Original answer',
  },
] as ChatMessage[];

function withStoreMetadata(
  rows: ChatMessage[],
  createdAtOffset = 0
): ChatMessage[] {
  return rows.map((row, index) => ({
    ...row,
    created_at: new Date(
      Date.UTC(2026, 7, 1, 0, 0, createdAtOffset + index)
    ).toISOString(),
    citations: [],
  }));
}

function makeParams(
  overrides: Record<string, unknown> = {}
): Record<string, unknown> {
  useChatStore.setState({ currentThreadId: 'thread-A' });
  return {
    messages: [] as ChatPageMessage[],
    displayedMessages: history,
    setMessages: vi.fn(),
    conversations: [],
    setConversations: vi.fn(),
    dbConversation: null,
    enableRAG: false,
    ...overrides,
  };
}

type Payload = {
  messages: Array<{
    role: string;
    content: string;
    client_message_id?: string;
  }>;
  supersedes_client_message_id?: string;
};

type ReplacementStreamCallbacks = {
  onToken?: (content: string) => void;
  onDone?: (payload?: Record<string, unknown>) => void;
  onError?: (error: string, category?: string, localFailure?: string) => void;
};

describe('useChatStreaming edit-and-resend', () => {
  beforeEach(() => {
    streamMessageMock.mockReset();
    streamMessageMock.mockImplementation(
      (_req: unknown, cb: { onDone?: (p?: unknown) => void }) => {
        cb.onDone?.({});
        return Promise.resolve();
      }
    );
  });

  it('sends the superseded cmid plus a FRESH id on the new turn', async () => {
    const { useChatStreaming } = await import('@/hooks/chat/useChatStreaming');
    const { result } = renderHook(() => useChatStreaming(makeParams()), {
      wrapper,
    });

    await act(async () => {
      // Edit the first turn: history truncated to before it, and that turn's
      // persisted cmid named as superseded.
      await result.current.handleSubmit('first turn, edited', [], EDITED_CMID);
    });

    const payload = streamMessageMock.mock.calls[0][0] as Payload;
    expect(payload.supersedes_client_message_id).toBe(EDITED_CMID);

    const lastTurn = payload.messages[payload.messages.length - 1];
    expect(lastTurn.role).toBe('user');
    expect(lastTurn.content).toBe('first turn, edited');
    expect(lastTurn.client_message_id).toBeTruthy();
    // FRESH — a reused id would be swallowed by the server's ON CONFLICT dedup.
    expect(lastTurn.client_message_id).not.toBe(EDITED_CMID);
  });

  it('hides the replaced suffix while pending, then restores it on stream failure', async () => {
    let callbacks!: ReplacementStreamCallbacks;
    let release!: () => void;
    streamMessageMock.mockImplementation(
      (_request: unknown, nextCallbacks: ReplacementStreamCallbacks) => {
        callbacks = nextCallbacks;
        return new Promise<void>((resolve) => {
          release = resolve;
        });
      }
    );
    useChatStore.setState({
      currentThreadId: 'thread-A',
      isStreaming: false,
      streamingThreadId: null,
    });
    const { useChatStreaming } = await import('@/hooks/chat/useChatStreaming');
    const displayedFrames: Array<{
      ids: string[];
      replacementInFlight: boolean;
    }> = [];
    const { result } = renderHook(
      () => {
        const [messages, setMessages] = useState<ChatPageMessage[]>([]);
        const [storeRows] = useState(() =>
          withStoreMetadata(replacementStoreRows)
        );
        const displayedMessages = selectDisplayedMessages({
          localMessages: messages,
          storeMessages: storeRows,
          messageFreshness: 'stale',
        });
        const streaming = useChatStreaming({
          messages,
          displayedMessages,
          setMessages,
          conversations: [],
          setConversations: vi.fn(),
          dbConversation: null,
          enableRAG: false,
        });
        return { messages, displayedMessages, streaming };
      },
      { wrapper }
    );

    let submission!: Promise<void>;
    act(() => {
      submission = result.current.streaming.handleSubmit(
        'first turn, edited',
        [],
        EDITED_CMID
      );
    });
    await waitFor(() =>
      expect(
        result.current.messages.some(
          (message) => message.replacesClientMessageId === EDITED_CMID
        )
      ).toBe(true)
    );
    const pendingDisplay = result.current.displayedMessages.map(
      ({ runtimeId }) => runtimeId
    );
    displayedFrames.push({ ids: pendingDisplay, replacementInFlight: true });
    expect(pendingDisplay).not.toContain(EDITED_CMID);
    expect(pendingDisplay).not.toContain('runtime-old-assistant');
    expect(pendingDisplay).toContain('runtime-before-user');
    expect(pendingDisplay).toContain('runtime-before-assistant');

    callbacks.onError?.('synthetic replacement failure', 'stream-error');
    await act(async () => {
      release();
      await submission;
    });

    expect(
      result.current.messages.some(
        (message) => message.replacesClientMessageId !== undefined
      )
    ).toBe(false);
    const failedDisplay = result.current.displayedMessages.map(
      ({ runtimeId }) => runtimeId
    );
    displayedFrames.push({ ids: failedDisplay, replacementInFlight: false });
    expect(
      displayedFrames
        .filter(({ replacementInFlight }) => replacementInFlight)
        .every(
          ({ ids }) =>
            !ids.includes(EDITED_CMID) && !ids.includes('runtime-old-assistant')
        )
    ).toBe(true);
    expect(failedDisplay).toContain('runtime-before-user');
    expect(failedDisplay).toContain('runtime-before-assistant');
    expect(failedDisplay).toContain(EDITED_CMID);
    expect(failedDisplay).toContain('runtime-old-assistant');
    expect(
      result.current.messages.some(
        (message) => message.error?.category === 'stream-error'
      )
    ).toBe(true);
  });

  it('keeps the replacement hidden until a successful canonical refresh reconciles it', async () => {
    let callbacks!: ReplacementStreamCallbacks;
    let release!: () => void;
    streamMessageMock.mockImplementation(
      (_request: unknown, nextCallbacks: ReplacementStreamCallbacks) => {
        callbacks = nextCallbacks;
        return new Promise<void>((resolve) => {
          release = resolve;
        });
      }
    );
    useChatStore.setState({
      currentThreadId: 'thread-A',
      isStreaming: false,
      streamingThreadId: null,
    });
    const { useChatStreaming } = await import('@/hooks/chat/useChatStreaming');
    const displayedFrames: Array<{
      ids: string[];
      replacementInFlight: boolean;
    }> = [];
    const { result } = renderHook(
      () => {
        const [messages, setMessages] = useState<ChatPageMessage[]>([]);
        const [storeRows, setStoreRows] = useState(() =>
          withStoreMetadata(replacementStoreRows)
        );
        const [freshness, setFreshness] = useState<'stale' | 'fresh'>('stale');
        const displayedMessages = selectDisplayedMessages({
          localMessages: messages,
          storeMessages: storeRows,
          messageFreshness: freshness,
        });
        const streaming = useChatStreaming({
          messages,
          displayedMessages,
          setMessages,
          conversations: [],
          setConversations: vi.fn(),
          dbConversation: null,
          enableRAG: false,
        });
        return {
          messages,
          displayedMessages,
          setStoreRows,
          setFreshness,
          streaming,
        };
      },
      { wrapper }
    );

    let submission!: Promise<void>;
    act(() => {
      submission = result.current.streaming.handleSubmit(
        'first turn, edited',
        [],
        EDITED_CMID
      );
    });
    await waitFor(() => expect(callbacks).toBeDefined());
    callbacks.onToken?.('edited answer');
    callbacks.onDone?.({ assistant_message_id: 'new-assistant' });
    await act(async () => {
      release();
      await submission;
    });

    const pendingDisplay = result.current.displayedMessages.map(
      ({ runtimeId }) => runtimeId
    );
    displayedFrames.push({ ids: pendingDisplay, replacementInFlight: true });
    expect(pendingDisplay).not.toContain(EDITED_CMID);
    expect(pendingDisplay).not.toContain('runtime-old-assistant');
    const replacementUser = result.current.messages.find(
      (message) => message.role === 'user'
    );
    const replacementAssistant = result.current.messages.find(
      (message) => message.role === 'assistant'
    );
    expect(replacementUser).toBeDefined();
    expect(replacementAssistant).toBeDefined();

    act(() => {
      result.current.setStoreRows(
        withStoreMetadata(
          [
            replacementStoreRows[0],
            replacementStoreRows[1],
            {
              id: 'new-user',
              client_message_id: replacementUser!.runtimeId,
              role: 'user',
              content: 'first turn, edited',
            },
            {
              id: 'new-assistant',
              client_message_id: replacementAssistant!.runtimeId,
              role: 'assistant',
              content: 'edited answer',
            },
          ] as ChatMessage[],
          10
        )
      );
      result.current.setFreshness('fresh');
    });
    await waitFor(() =>
      expect(
        result.current.displayedMessages.map(({ runtimeId }) => runtimeId)
      ).toEqual([
        'runtime-before-user',
        'runtime-before-assistant',
        replacementUser!.runtimeId,
        replacementAssistant!.runtimeId,
      ])
    );
    displayedFrames.push({
      ids: result.current.displayedMessages.map(({ runtimeId }) => runtimeId),
      replacementInFlight: false,
    });
    expect(
      displayedFrames
        .filter(({ replacementInFlight }) => replacementInFlight)
        .every(
          ({ ids }) =>
            !ids.includes(EDITED_CMID) && !ids.includes('runtime-old-assistant')
        )
    ).toBe(true);
    expect(
      result.current.displayedMessages.filter(
        ({ runtimeId }) => runtimeId === replacementUser!.runtimeId
      )
    ).toHaveLength(1);
  });

  it('omits the field entirely on a normal send', async () => {
    const { useChatStreaming } = await import('@/hooks/chat/useChatStreaming');
    const { result } = renderHook(() => useChatStreaming(makeParams()), {
      wrapper,
    });

    await act(async () => {
      await result.current.handleSubmit('follow-up');
    });

    const payload = streamMessageMock.mock.calls[0][0] as Payload;
    expect('supersedes_client_message_id' in payload).toBe(false);
  });

  it('omits the field when the edited turn was never persisted with a cmid', async () => {
    const { useChatStreaming } = await import('@/hooks/chat/useChatStreaming');
    const { result } = renderHook(() => useChatStreaming(makeParams()), {
      wrapper,
    });

    await act(async () => {
      await result.current.handleSubmit('edited', [], undefined);
    });

    const payload = streamMessageMock.mock.calls[0][0] as Payload;
    expect('supersedes_client_message_id' in payload).toBe(false);
  });
});
