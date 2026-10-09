import toast from 'react-hot-toast';

import { getNewChatUrl } from '@/components/chat/shared/chatNavigation';
import { ChatConversation } from '@/hooks/chat/chatTypes';
import { workspaceService } from '@/services/workspaceService';
import { useChatSessionGuard } from './useChatSessionGuard';
import { useChatStore, onChatSessionReset } from '@/store/chat-store';
import { useCallback, useState, useEffect } from 'react';
import { useRouter } from 'next/navigation';

// ============================================
// DIALOG STATE TYPES (exported for ChatDialogs)
// ============================================

export interface RenameDialogState {
  open: boolean;
  threadId: string;
  currentTitle: string;
  value: string;
}

export interface DeleteDialogState {
  open: boolean;
  threadId: string;
}

export interface BulkDeleteDialogState {
  open: boolean;
  ids: string[];
}

// ============================================
// HOOK PARAMS
// ============================================

export interface UseChatThreadActionsParams {
  conversations: ChatConversation[];
  setConversations: React.Dispatch<React.SetStateAction<ChatConversation[]>>;
  activeThreadId: string | null;
  setCurrentThread: (threadId: string | null) => void;
}

export interface UseChatThreadActionsReturn {
  renameDialog: RenameDialogState;
  setRenameDialog: React.Dispatch<React.SetStateAction<RenameDialogState>>;
  deleteDialog: DeleteDialogState;
  setDeleteDialog: React.Dispatch<React.SetStateAction<DeleteDialogState>>;
  bulkDeleteDialog: BulkDeleteDialogState;
  setBulkDeleteDialog: React.Dispatch<
    React.SetStateAction<BulkDeleteDialogState>
  >;
  handleRenameThread: (threadId: string) => Promise<void>;
  commitRename: () => Promise<void>;
  handleDeleteThread: (threadId: string) => void;
  commitDeleteThread: () => Promise<void>;
  handleBulkDeleteThreads: (ids: string[]) => void;
  commitBulkDelete: () => Promise<void>;
}

// ============================================
// HOOK
// ============================================

export function useChatThreadActions({
  conversations,
  setConversations,
  activeThreadId,
}: UseChatThreadActionsParams): UseChatThreadActionsReturn {
  const router = useRouter();
  const isCurrentSession = useChatSessionGuard();

  // Dialog state for rename/delete — replaces window.prompt/confirm
  const [renameDialog, setRenameDialog] = useState<RenameDialogState>({
    open: false,
    threadId: '',
    currentTitle: '',
    value: '',
  });
  const [deleteDialog, setDeleteDialog] = useState<DeleteDialogState>({
    open: false,
    threadId: '',
  });
  const [bulkDeleteDialog, setBulkDeleteDialog] =
    useState<BulkDeleteDialogState>({ open: false, ids: [] });

  useEffect(
    () =>
      onChatSessionReset(() => {
        setRenameDialog({
          open: false,
          threadId: '',
          currentTitle: '',
          value: '',
        });
        setDeleteDialog({ open: false, threadId: '' });
        setBulkDeleteDialog({ open: false, ids: [] });
      }),
    []
  );

  const handleRenameThread = useCallback(
    async (threadId: string) => {
      if (!isCurrentSession()) return;
      const target = conversations.find((c) => c.id === threadId);
      setRenameDialog({
        open: true,
        threadId,
        currentTitle: target?.title ?? '',
        value: target?.title ?? '',
      });
    },
    [conversations, isCurrentSession]
  );

  const commitRename = useCallback(async () => {
    if (!isCurrentSession()) return;
    const { threadId, value, currentTitle } = renameDialog;
    const trimmed = value.trim();
    setRenameDialog((d) => ({ ...d, open: false }));
    if (!trimmed || trimmed === currentTitle) return;
    try {
      const updated = await workspaceService.updateThread(threadId, {
        title: trimmed,
      });
      if (!isCurrentSession()) return;
      const nextTitleValue = updated.title ?? trimmed;
      setConversations((prev) =>
        prev.map((c) =>
          c.id === threadId ? { ...c, title: nextTitleValue } : c
        )
      );
    } catch (err) {
      if (!isCurrentSession()) return;
      // The dialog already closed optimistically, so without this the title
      // silently stays the old value and the rename looks like it worked.
      console.error('[Chat] Rename failed', err);
      toast.error('Could not rename the conversation. Please try again.');
    }
  }, [renameDialog, setConversations, isCurrentSession]);

  const handleDeleteThread = useCallback((threadId: string) => {
    setDeleteDialog({ open: true, threadId });
  }, []);

  const commitDeleteThread = useCallback(async () => {
    if (!isCurrentSession()) return;
    const { threadId } = deleteDialog;
    setDeleteDialog({ open: false, threadId: '' });
    // Route through the store action: it aborts the thread's in-flight page
    // request and drops every per-thread cache (messages, pagination,
    // freshness, reverse indexes). Calling workspaceService directly leaked
    // all of those for the deleted thread.
    const deleted = await useChatStore.getState().deleteThread(threadId);
    if (!isCurrentSession()) return;
    if (!deleted) {
      toast.error('Could not delete the conversation. Please try again.');
      return;
    }
    setConversations((prev) => prev.filter((c) => c.id !== threadId));
    if (activeThreadId === threadId) {
      // The store already cleared currentThreadId; just leave the URL.
      router.push(getNewChatUrl());
    }
  }, [
    deleteDialog,
    activeThreadId,
    router,
    setConversations,
    isCurrentSession,
  ]);

  const handleBulkDeleteThreads = useCallback((ids: string[]) => {
    setBulkDeleteDialog({ open: true, ids });
  }, []);

  const commitBulkDelete = useCallback(async () => {
    if (!isCurrentSession()) return;
    const { ids } = bulkDeleteDialog;
    setBulkDeleteDialog({ open: false, ids: [] });
    // Same store-routing rationale as commitDeleteThread: one bulk API call,
    // then per-thread abort + cache cleanup inside the store action.
    const response = await useChatStore.getState().bulkDeleteThreads(ids);
    if (!isCurrentSession()) return;
    if (!response) {
      toast.error(
        'Could not delete the selected conversations. Please try again.'
      );
      return;
    }
    const deletedIds = new Set(
      response.results.filter((r) => r.success).map((r) => r.thread_id)
    );
    setConversations((prev) => prev.filter((c) => !deletedIds.has(c.id)));
    if (activeThreadId && deletedIds.has(activeThreadId)) {
      router.push(getNewChatUrl());
    }
  }, [
    bulkDeleteDialog,
    activeThreadId,
    router,
    setConversations,
    isCurrentSession,
  ]);

  return {
    // Dialog state
    renameDialog,
    setRenameDialog,
    deleteDialog,
    setDeleteDialog,
    bulkDeleteDialog,
    setBulkDeleteDialog,
    // Handlers
    handleRenameThread,
    commitRename,
    handleDeleteThread,
    commitDeleteThread,
    handleBulkDeleteThreads,
    commitBulkDelete,
  };
}
