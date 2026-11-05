import { ChatMessageActions } from '@nous/design-system';

const noop = () => {};
const assistant = { id: 'm2', role: 'assistant', content: 'FlashAttention tiles the softmax so it never materializes the full matrix.', timestamp: new Date('2026-11-03T09:32:00Z') };
const user = { id: 'm1', role: 'user', content: 'How does FlashAttention save memory?', timestamp: new Date('2026-11-03T09:31:00Z') };

// Hover actions under a chat message (llm-chat/ChatMessage).
export const AssistantMessage = () => (
  <ChatMessageActions message={assistant as never} index={1} onCopy={noop} onRegenerate={noop} onDelete={noop} isCopied={false} />
);

export const UserMessage = () => (
  <ChatMessageActions message={user as never} index={0} onCopy={noop} onEdit={noop} onDelete={noop} isCopied={false} />
);

export const Copied = () => (
  <ChatMessageActions message={assistant as never} index={1} onCopy={noop} onRegenerate={noop} isCopied />
);
