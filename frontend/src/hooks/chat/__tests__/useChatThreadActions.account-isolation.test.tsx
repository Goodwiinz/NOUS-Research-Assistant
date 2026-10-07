import { act, renderHook } from '@testing-library/react';
import { expect, it, vi } from 'vitest';
import { useChatStore } from '@/store/chat-store';
import { useChatThreadActions } from '@/hooks/chat/useChatThreadActions';
import { workspaceService } from '@/services/workspaceService';
const toastError = vi.hoisted(() => vi.fn());
vi.mock('react-hot-toast', () => ({ default: { error: toastError } }));
vi.mock('next/navigation', () => ({ useRouter: () => ({ push: vi.fn() }) }));
vi.mock('@/services/workspaceService', () => ({
  workspaceService: {
    updateThread: vi.fn(),
    deleteThread: vi.fn(),
    bulkDeleteThreads: vi.fn(),
  },
}));
function setup(): {
  result: { current: ReturnType<typeof useChatThreadActions> };
  setConversations: ReturnType<typeof vi.fn>;
} {
  const setConversations = vi.fn();
  return {
    setConversations,
    ...renderHook(() =>
      useChatThreadActions({
        conversations: [{ id: 'A-thread', title: 'A private title' } as never],
        setConversations,
        activeThreadId: 'A-thread',
        setCurrentThread: vi.fn(),
      })
    ),
  };
}
it('clears account-scoped rename and deletion dialogs', async () => {
  const { result } = setup();
  await act(async () => {
    await result.current.handleRenameThread('A-thread');
    result.current.handleDeleteThread('A-thread');
    result.current.handleBulkDeleteThreads(['A-thread']);
  });
  act(() => {
    useChatStore.getState().reset();
  });
  expect(result.current.renameDialog).toEqual({
    open: false,
    threadId: '',
    currentTitle: '',
    value: '',
  });
  expect(result.current.deleteDialog).toEqual({ open: false, threadId: '' });
  expect(result.current.bulkDeleteDialog).toEqual({ open: false, ids: [] });
});
it('ignores a delayed rename response from the previous account', async () => {
  let finish!: (value: never) => void;
  vi.mocked(workspaceService.updateThread).mockReturnValueOnce(
    new Promise((resolve) => {
      finish = resolve;
    })
  );
  const { result, setConversations } = setup();
  act(() => {
    result.current.setRenameDialog({
      open: true,
      threadId: 'A-thread',
      currentTitle: 'old',
      value: 'A private revision',
    });
  });
  let pending!: Promise<void>;
  act(() => {
    pending = result.current.commitRename();
  });
  act(() => {
    useChatStore.getState().reset();
  });
  await act(async () => {
    finish({ title: 'A private revision' } as never);
    await pending;
  });
  expect(setConversations).not.toHaveBeenCalled();
  expect(toastError).not.toHaveBeenCalled();
});
