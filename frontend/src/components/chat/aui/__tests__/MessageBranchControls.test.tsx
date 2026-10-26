import { beforeEach, expect, it, vi } from 'vitest';
import { fireEvent, render, screen, waitFor } from '@/test/test-utils';
import { useChatStore } from '@/store/chat-store';
import { workspaceService } from '@/services/workspaceService';
import { MessageBranchControls } from '../MessageBranchControls';
import { AuiMessageByIndex } from '../AuiMessage';
import { ChatRuntimeProvider } from '../ChatRuntimeProvider';
import { makeChatPageMessage } from '@/test/chatMessageFactory';
import { MessageRole } from '@/types/workspace';

vi.mock('@/services/workspaceService', () => ({
  workspaceService: { selectMessageBranch: vi.fn(), listMessages: vi.fn() },
}));
const message = makeChatPageMessage({
  id: 'u1',
  role: 'user',
  content: 'First',
  siblingUserMessageIds: ['u1', 'u2'],
});
beforeEach(() => {
  useChatStore.getState().reset();
  vi.clearAllMocks();
  useChatStore.setState({
    currentThreadId: 'thread',
    messageFreshness: { thread: 'fresh' },
    messages: {
      thread: [
        {
          id: 'u1',
          thread_id: 'thread',
          role: MessageRole.USER,
          content: 'First',
          created_at: '',
          updated_at: '',
          token_count: 0,
          citations: [],
          attachments: [],
          sibling_user_message_ids: ['u1', 'u2'],
        },
      ],
    },
    messagePagination: {
      thread: {
        hasMore: false,
        loadingOlder: false,
        loadedCount: 1,
        message_branching: true,
        branch_document_id: 'pdf',
        selected_message_id: 'u1',
      },
    },
  });
});
it('offers named alternatives and switches through canonical selection then refresh', async () => {
  vi.mocked(workspaceService.selectMessageBranch).mockResolvedValueOnce();
  vi.mocked(workspaceService.listMessages).mockResolvedValueOnce({
    messages: [],
    total: 0,
    page: 1,
    limit: 50,
    has_more: false,
    message_branching: true,
    branch_document_id: 'pdf',
    selected_message_id: 'u2',
  });
  const { user } = render(<MessageBranchControls message={message} />);
  expect(screen.getByText('1 of 2')).toBeInTheDocument();
  expect(
    screen.getByRole('button', { name: 'Previous message alternative' })
  ).toBeDisabled();
  await user.click(
    screen.getByRole('button', { name: 'Next message alternative' })
  );
  await waitFor(() => expect(workspaceService.listMessages).toHaveBeenCalled());
  expect(workspaceService.selectMessageBranch).toHaveBeenCalledWith(
    'thread',
    'u2'
  );
});
it.each(['refreshing', 'streaming'])(
  'disables alternatives while %s',
  (condition) => {
    useChatStore.setState(
      condition === 'streaming'
        ? { isStreaming: true }
        : { messageFreshness: { thread: 'refreshing' } }
    );
    render(<MessageBranchControls message={message} />);
    expect(
      screen.getByRole('button', { name: 'Next message alternative' })
    ).toBeDisabled();
  }
);
it('does not show branch controls for linear messages', () => {
  useChatStore.setState({
    messagePagination: {
      thread: { hasMore: false, loadingOlder: false, loadedCount: 1 },
    },
  });
  render(<MessageBranchControls message={message} />);
  expect(
    screen.queryByRole('button', { name: 'Next message alternative' })
  ).not.toBeInTheDocument();
});

it('keeps alternatives before hover-mounted actions and selects without opening Edit', async () => {
  vi.mocked(workspaceService.selectMessageBranch).mockResolvedValueOnce();
  vi.mocked(workspaceService.listMessages).mockResolvedValueOnce({
    messages: [],
    total: 0,
    page: 1,
    limit: 50,
    has_more: false,
    message_branching: true,
    branch_document_id: 'pdf',
    selected_message_id: 'u2',
  });
  const { user } = render(
    <ChatRuntimeProvider
      messages={[
        message,
        makeChatPageMessage({ id: 'a1', role: 'assistant', content: 'Answer' }),
      ]}
      isRunning={false}
      onSend={vi.fn()}
      onCancel={vi.fn()}
    >
      <AuiMessageByIndex index={0} message={message} onEdit={vi.fn()} />
    </ChatRuntimeProvider>
  );
  const alternatives = screen.getByLabelText('Message alternatives');
  const bubble = document.querySelector('.nous-bubble-user');
  expect(bubble?.nextElementSibling).toBe(alternatives);
  expect(screen.queryByRole('button', { name: 'Edit and resend' })).toBeNull();

  fireEvent.mouseEnter(document.querySelector('[data-role="user"]')!);
  const edit = await screen.findByRole('button', { name: 'Edit and resend' });
  // jsdom does not lay out pixels. This checks the normal-flow order that
  // prevents the newly mounted action row from shifting the alternatives.
  expect(bubble?.nextElementSibling).toBe(alternatives);
  expect(alternatives.nextElementSibling).toContainElement(edit);

  await user.click(
    screen.getByRole('button', { name: 'Next message alternative' })
  );
  await waitFor(() =>
    expect(workspaceService.selectMessageBranch).toHaveBeenCalledWith(
      'thread',
      'u2'
    )
  );
  expect(screen.queryByLabelText('Edit your message')).toBeNull();
});
