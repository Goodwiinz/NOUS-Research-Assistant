import { act, renderHook, waitFor } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { type ReactNode, type ReactElement } from 'react';
import { useChatStore } from '@/store/chat-store';
import { usePdfChatBinding } from '../usePdfChatBinding';
import { ChatRagProvider, useChatRag } from '@/components/chat/ChatRagContext';
import { documentService } from '@/services/documentService';
import { useArtifactPanelStore } from '@/store/artifactPanelStore';

vi.mock('@/services/documentService', () => ({
  documentService: { getDocument: vi.fn() },
}));
const scope = vi.hoisted(() => ({ current: 'actor-A:thread' }));
vi.mock('../useArtifactScope', () => ({
  useArtifactScope: () => scope.current,
}));
const doc = {
  id: 'pdf',
  filename: 'paper.pdf',
  title: 'Saved paper',
  file_type: 'pdf',
  mime_type: 'application/pdf',
};
const getDocument = vi.mocked(documentService.getDocument);
const deferred = <T,>(): {
  promise: Promise<T>;
  resolve: (value: T) => void;
} => {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((yes) => {
    resolve = yes;
  });
  return { promise, resolve };
};
function setup(): {
  client: QueryClient;
  wrapper: (props: { children: ReactNode }) => ReactElement;
} {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false, gcTime: 0 } },
  });
  return {
    client,
    wrapper: ({ children }: { children: ReactNode }) => (
      <QueryClientProvider client={client}>{children}</QueryClientProvider>
    ),
  };
}
function bind(head = 'assistant'): void {
  useChatStore.setState({
    currentThreadId: 'thread',
    messageFreshness: { thread: 'fresh' },
    messagePagination: {
      thread: {
        hasMore: false,
        loadingOlder: false,
        loadedCount: 2,
        message_branching: true,
        branch_document_id: 'pdf',
        selected_message_id: head,
      },
    },
  });
}
beforeEach(() => {
  useChatStore.getState().reset();
  getDocument.mockReset();
  scope.current = 'actor-A:thread';
});

describe('durable PDF binding', () => {
  it('blocks while thread metadata is unresolved, without inventing a document', () => {
    useChatStore.setState({ currentThreadId: 'thread' });
    const { result } = renderHook(
      () => usePdfChatBinding(scope.current),
      setup()
    );
    expect(result.current.pending).toBe(true);
    expect(result.current.document).toBeUndefined();
    expect(getDocument).not.toHaveBeenCalled();
  });

  it('restores bound context from fresh server metadata and document access without a route hint', async () => {
    bind();
    const pending =
      deferred<Awaited<ReturnType<typeof documentService.getDocument>>>();
    getDocument.mockReturnValueOnce(pending.promise);
    const { wrapper } = setup();
    useArtifactPanelStore.getState().setScope(scope.current);
    const { result } = renderHook(() => useChatRag(), {
      wrapper: ({ children }) =>
        wrapper({ children: <ChatRagProvider>{children}</ChatRagProvider> }),
    });
    expect(result.current.pdfIntentPending).toBe(true);
    expect(result.current.pdfContext).toBeNull();
    await act(async () =>
      pending.resolve(
        doc as Awaited<ReturnType<typeof documentService.getDocument>>
      )
    );
    await waitFor(() => expect(result.current.pdfIntentPending).toBe(false));
    expect(result.current.pdfIsBound).toBe(true);
    expect(result.current.pdfContext).toEqual({
      documentId: 'pdf',
      title: 'Saved paper',
    });
    expect(useArtifactPanelStore.getState().artifact).toMatchObject({
      kind: 'document',
      id: 'pdf',
    });
    act(() => result.current.setPdfContext(null));
    expect(result.current.pdfContext?.documentId).toBe('pdf');
    act(() =>
      result.current.setPdfContext({
        documentId: 'other',
        title: 'Other paper',
      })
    );
    expect(result.current.pdfContext?.documentId).toBe('pdf');
  });

  it('keeps submission closed on revoked document access', async () => {
    bind();
    getDocument.mockRejectedValueOnce(new Error('403'));
    const { result } = renderHook(
      () => usePdfChatBinding(scope.current),
      setup()
    );
    await waitFor(() => expect(result.current.error).toBeTruthy());
    expect(result.current.pending).toBe(true);
    expect(result.current.document).toBeUndefined();
  });

  it('does not adopt an old account document lookup after its scope changes', async () => {
    bind();
    const old =
      deferred<Awaited<ReturnType<typeof documentService.getDocument>>>();
    const current =
      deferred<Awaited<ReturnType<typeof documentService.getDocument>>>();
    getDocument
      .mockReturnValueOnce(old.promise)
      .mockReturnValueOnce(current.promise);
    const { result, rerender } = renderHook(
      ({ owner }) => usePdfChatBinding(owner),
      { ...setup(), initialProps: { owner: 'A' } }
    );
    rerender({ owner: 'B' });
    await act(async () =>
      old.resolve(
        doc as Awaited<ReturnType<typeof documentService.getDocument>>
      )
    );
    expect(result.current.pending).toBe(true);
    expect(result.current.document).toBeUndefined();
    await act(async () =>
      current.resolve({ ...doc, title: 'Current owner paper' } as Awaited<
        ReturnType<typeof documentService.getDocument>
      >)
    );
    await waitFor(() => expect(result.current.pending).toBe(false));
    expect(result.current.document?.title).toBe('Current owner paper');
  });

  it('retains document binding while branch selection refreshes, but disables submissions', async () => {
    bind();
    getDocument.mockResolvedValueOnce(
      doc as Awaited<ReturnType<typeof documentService.getDocument>>
    );
    const { result } = renderHook(
      () => usePdfChatBinding(scope.current),
      setup()
    );
    await waitFor(() => expect(result.current.pending).toBe(false));
    act(() =>
      useChatStore.setState({ messageFreshness: { thread: 'refreshing' } })
    );
    expect(result.current.bound).toBe(true);
    expect(result.current.document?.id).toBe('pdf');
    expect(result.current.pending).toBe(true);
  });
});
