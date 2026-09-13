import { describe, expect, it, vi } from 'vitest';
import React from 'react';
import { render, screen, waitFor } from '@testing-library/react';

import {
  selectDisplayedMessages,
  type ChatPageMessage,
} from '@/components/chat/shared/cloudMessageView';
import { makeChatPageMessage } from '@/test/chatMessageFactory';
import { MessageRole, type ChatMessage } from '@/types/workspace';
import { AuiMessages } from '../AuiMessage';
import { ChatRuntimeProvider } from '../ChatRuntimeProvider';

const noop = vi.fn();

const turnIdentities = [
  ['turn-1-user', 'turn-1-assistant'],
  ['turn-2-user', 'turn-2-assistant'],
  ['turn-3-user', 'turn-3-assistant'],
] as const;

function optimisticTurn(index: number): ChatPageMessage[] {
  const [userId, assistantId] = turnIdentities[index];
  return [
    makeChatPageMessage({
      runtimeId: userId,
      source: 'optimistic',
      role: 'user',
      content: `Question ${index + 1}`,
      timestamp: index * 2 + 1,
    }),
    makeChatPageMessage({
      runtimeId: assistantId,
      source: 'optimistic',
      role: 'assistant',
      content: `Answer ${index + 1}`,
      timestamp: index * 2 + 2,
    }),
  ];
}

function canonicalTurn(index: number): ChatMessage[] {
  const [userId, assistantId] = turnIdentities[index];
  const second = String(index * 2 + 1).padStart(2, '0');
  return [
    {
      id: `db-${userId}`,
      thread_id: 'thread-identity',
      client_message_id: userId,
      role: MessageRole.USER,
      content: `Question ${index + 1}`,
      token_count: 3,
      created_at: `2026-09-13T12:00:${second}Z`,
      updated_at: `2026-09-13T12:00:${second}Z`,
      citations: [],
      attachments: [],
    },
    {
      id: `db-${assistantId}`,
      thread_id: 'thread-identity',
      client_message_id: assistantId,
      role: MessageRole.ASSISTANT,
      content: `Answer ${index + 1}`,
      token_count: 5,
      stopped: false,
      created_at: `2026-09-13T12:00:${String(index * 2 + 2).padStart(2, '0')}Z`,
      updated_at: `2026-09-13T12:00:${String(index * 2 + 2).padStart(2, '0')}Z`,
      citations: [],
      attachments: [],
    },
  ];
}

function RuntimeFrame({ messages }: { messages: ChatPageMessage[] }) {
  return (
    <ChatRuntimeProvider
      messages={messages}
      isRunning={false}
      onSend={noop}
      onCancel={noop}
    >
      <AuiMessages />
    </ChatRuntimeProvider>
  );
}

describe('message identity handoff', () => {
  // Mutation proof: removing the canonicalRuntimeIds overlay guard at
  // shared/cloudMessageView.ts:348 must make the duplicate-id assertion fail.
  // From frontend:
  //   vitest run src/components/chat/aui/__tests__/messageIdentityHandoff.test.tsx
  it('keeps one runtime row per turn through successive stale, refreshing, and fresh handoffs', async () => {
    const optimistic = turnIdentities.map((_, index) => optimisticTurn(index));
    const canonical = turnIdentities.map((_, index) => canonicalTurn(index));
    const frames = [
      {
        localMessages: optimistic[0],
        storeMessages: [] as ChatMessage[],
        messageFreshness: 'stale' as const,
        expectedIds: [...turnIdentities[0]],
      },
      {
        localMessages: optimistic[0],
        storeMessages: canonical[0],
        messageFreshness: 'refreshing' as const,
        expectedIds: [...turnIdentities[0]],
      },
      {
        localMessages: [...optimistic[0], ...optimistic[1]],
        storeMessages: canonical[0],
        messageFreshness: 'stale' as const,
        expectedIds: [...turnIdentities[0], ...turnIdentities[1]],
      },
      {
        localMessages: [...optimistic[0], ...optimistic[1]],
        storeMessages: [...canonical[0], ...canonical[1]],
        messageFreshness: 'refreshing' as const,
        expectedIds: [...turnIdentities[0], ...turnIdentities[1]],
      },
      {
        localMessages: [
          ...optimistic[0],
          ...optimistic[1],
          ...optimistic[2],
        ],
        storeMessages: [...canonical[0], ...canonical[1]],
        messageFreshness: 'stale' as const,
        expectedIds: [
          ...turnIdentities[0],
          ...turnIdentities[1],
          ...turnIdentities[2],
        ],
      },
      {
        localMessages: [
          ...optimistic[0],
          ...optimistic[1],
          ...optimistic[2],
        ],
        storeMessages: [...canonical[0], ...canonical[1], ...canonical[2]],
        messageFreshness: 'fresh' as const,
        expectedIds: [
          ...turnIdentities[0],
          ...turnIdentities[1],
          ...turnIdentities[2],
        ],
      },
    ];

    const first = selectDisplayedMessages(frames[0]);
    const view = render(<RuntimeFrame messages={first} />);

    for (const frame of frames) {
      const displayed = selectDisplayedMessages(frame);
      const ids = displayed.map((message) => message.runtimeId);
      expect(new Set(ids).size).toBe(ids.length);
      expect(ids).toEqual(frame.expectedIds);

      view.rerender(<RuntimeFrame messages={displayed} />);
      await waitFor(() => {
        frame.expectedIds.forEach((_, index) => {
          const turn = Math.floor(index / 2) + 1;
          const text = index % 2 === 0 ? `Question ${turn}` : `Answer ${turn}`;
          expect(screen.getAllByText(text)).toHaveLength(1);
        });
      });
    }
  });
});
