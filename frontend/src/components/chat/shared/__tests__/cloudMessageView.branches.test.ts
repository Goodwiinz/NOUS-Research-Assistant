import { describe, expect, it } from 'vitest';
import { MessageRole, type ChatMessage } from '@/types/workspace';
import {
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
});
