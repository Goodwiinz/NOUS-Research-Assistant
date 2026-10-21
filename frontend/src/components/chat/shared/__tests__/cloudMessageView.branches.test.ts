import { describe, expect, it } from 'vitest';
import { MessageRole, type ChatMessage } from '@/types/workspace';
import {
  dropBranchOverlays,
  mapDbMessageToChatPageMessage,
  selectDisplayedMessages,
} from '../cloudMessageView';
import { makeChatPageMessage } from '@/test/chatMessageFactory';

const canonical: ChatMessage = {
  id: 'selected-user',
  client_message_id: 'runtime-selected',
  thread_id: 'thread',
  content: 'Selected',
  role: MessageRole.USER,
  created_at: '2026-10-14T00:00:00Z',
  updated_at: '2026-10-14T00:00:00Z',
  parent_message_id: 'canonical-parent',
  sibling_user_message_ids: ['other-user', 'selected-user'],
  citations: [],
  attachments: [],
  token_count: 0,
};
describe('branch transcript projection', () => {
  it('keeps canonical parent and alternative IDs separate from runtime identity', () => {
    expect(mapDbMessageToChatPageMessage(canonical)).toMatchObject({
      id: 'selected-user',
      runtimeId: 'runtime-selected',
      parentMessageId: 'canonical-parent',
      siblingUserMessageIds: ['other-user', 'selected-user'],
    });
  });
  it('does not resurrect stale sibling overlays after an authoritative branch switch', () => {
    const localMessages = [
      makeChatPageMessage({
        role: 'user',
        content: 'Old edit',
        source: 'optimistic',
        replacesClientMessageId: 'runtime-selected',
      }),
      makeChatPageMessage({
        role: 'assistant',
        content: 'Old error',
        source: 'local-only',
      }),
    ];
    expect(
      selectDisplayedMessages({
        localMessages,
        storeMessages: [canonical],
        messageFreshness: 'fresh',
        branching: true,
      }).map((row) => row.content)
    ).toEqual(['Selected']);
    expect(
      selectDisplayedMessages({
        localMessages,
        storeMessages: [canonical],
        messageFreshness: 'stale',
        branching: true,
      }).map((row) => row.content)
    ).toEqual(['Old edit', 'Old error']);
  });

  const streamError = makeChatPageMessage({
    role: 'assistant',
    content: '',
    source: 'local-only',
    runtimeId: 'error-bubble',
    error: {
      message: 'This response failed to generate. Please try again.',
      category: 'stream-error',
    },
  });
  const approval = makeChatPageMessage({
    role: 'assistant',
    content: '',
    source: 'local-only',
    runtimeId: 'approval:thread:run',
    pendingApproval: {
      id: 'approval-1',
      tools: [{ name: 'create_note', args: {} }],
    },
  });

  it('keeps a stream-error bubble after the reconcile makes the branch page fresh', () => {
    const rows = selectDisplayedMessages({
      localMessages: [
        makeChatPageMessage({
          role: 'user',
          content: 'Selected',
          source: 'optimistic',
          runtimeId: 'runtime-selected',
        }),
        streamError,
      ],
      storeMessages: [canonical],
      messageFreshness: 'fresh',
      branching: true,
    });
    expect(rows.map((row) => row.runtimeId)).toEqual([
      'runtime-selected',
      'error-bubble',
    ]);
    // The error block is what renders the bubble's Retry control.
    expect(rows[0].source).toBe('canonical');
    expect(rows[1].error).toEqual(streamError.error);
  });

  it('keeps a pending approval card through a fresh branch refresh', () => {
    expect(
      selectDisplayedMessages({
        localMessages: [approval],
        storeMessages: [canonical],
        messageFreshness: 'fresh',
        branching: true,
      }).map((row) => row.runtimeId)
    ).toEqual(['runtime-selected', 'approval:thread:run']);
  });

  it('drops the old sibling overlays on a branch switch so the new page shows none of them', () => {
    const oldSibling = [
      makeChatPageMessage({
        role: 'user',
        content: 'Old edit',
        source: 'optimistic',
        replacesClientMessageId: 'runtime-selected',
      }),
      streamError,
      approval,
    ];
    const cleared = dropBranchOverlays(oldSibling);
    // The approval's run is still waiting on the user; only it survives.
    expect(cleared).toEqual([approval]);
    expect(
      selectDisplayedMessages({
        localMessages: cleared,
        storeMessages: [canonical],
        messageFreshness: 'fresh',
        branching: true,
      }).map((row) => row.content)
    ).toEqual(['Selected', '']);
    expect(dropBranchOverlays([])).toEqual([]);
  });
});
