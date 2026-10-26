'use client';

import { useQuery } from '@tanstack/react-query';
import { useCallback } from 'react';
import { useChatStore } from '@/store/chat-store';
import { documentService } from '@/services/documentService';

/** The chat store owns binding metadata; the document query freshly verifies
 * the bound resource. Neither a route hint nor a persisted local preference
 * can authorize a paper-bound submission. */
interface PdfChatBinding {
  bound: boolean;
  documentId: string | null | undefined;
  pending: boolean;
  error: string | null;
  document: Awaited<ReturnType<typeof documentService.getDocument>> | undefined;
  retry: () => void;
}

export function usePdfChatBinding(scope: string | null): PdfChatBinding {
  const threadId = useChatStore((state) => state.currentThreadId);
  const page = useChatStore((state) =>
    threadId ? state.messagePagination[threadId] : undefined
  );
  const freshness = useChatStore((state) =>
    threadId ? state.messageFreshness[threadId] : undefined
  );
  const bound = page?.message_branching === true;
  const documentId = bound ? page.branch_document_id : null;
  const document = useQuery({
    queryKey: ['document', documentId, 'pdf-chat-binding', scope],
    queryFn: () => documentService.getDocument(documentId!),
    enabled: Boolean(scope && documentId),
    staleTime: 0,
    refetchOnMount: 'always',
    retry: false,
  });
  const isPdf =
    document.data &&
    ((
      document.data.file_type ||
      document.data.document_type ||
      ''
    ).toLowerCase() === 'pdf' ||
      document.data.mime_type === 'application/pdf');
  const error =
    bound && (!documentId || document.isError || (document.data && !isPdf))
      ? 'This chat’s PDF is unavailable. Retry access or start a new chat.'
      : null;
  const pending = Boolean(
    threadId &&
    (!page ||
      page.selectingBranch ||
      (bound &&
        (freshness !== 'fresh' ||
          !document.isFetchedAfterMount ||
          document.isFetching ||
          error)))
  );
  const { refetch } = document;
  const retry = useCallback(() => {
    if (threadId) void useChatStore.getState().refreshMessages(threadId);
    if (documentId) void refetch();
  }, [threadId, documentId, refetch]);
  return {
    bound,
    documentId,
    pending,
    error,
    document: bound && !error && isPdf ? document.data : undefined,
    retry,
  };
}
