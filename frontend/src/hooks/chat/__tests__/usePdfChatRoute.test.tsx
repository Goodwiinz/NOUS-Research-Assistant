import { renderHook, waitFor } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { usePdfChatRoute } from '../usePdfChatRoute';
import { useArtifactPanelStore } from '@/store/artifactPanelStore';

const mocks = vi.hoisted(() => ({
  params: new URLSearchParams(),
  replace: vi.fn(),
  setPdfContext: vi.fn(),
  getDocument: vi.fn(),
  consumeQuote: vi.fn(),
}));
vi.mock('next/navigation', () => ({
  useSearchParams: () => mocks.params,
  useRouter: () => ({ replace: mocks.replace }),
}));
vi.mock('@/hooks/chat/useArtifactScope', () => ({
  useArtifactScope: () => 'scope-A',
}));
vi.mock('@/components/chat/ChatRagContext', () => ({
  useChatRag: () => ({ setPdfContext: mocks.setPdfContext }),
}));
vi.mock('@/services/documentService', () => ({
  documentService: { getDocument: mocks.getDocument },
}));
vi.mock('@/lib/pdf/chat-intent', () => ({
  consumePdfQuoteIntent: (...args: unknown[]) => mocks.consumeQuote(...args),
}));

function wrapper({
  children,
}: {
  children: React.ReactNode;
}): React.ReactElement {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  return <QueryClientProvider client={client}>{children}</QueryClientProvider>;
}
const id = '00000000-0000-4000-8000-000000000001';

describe('document detail to new PDF chat', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mocks.consumeQuote.mockReset();
    useArtifactPanelStore.getState().setScope('scope-A');
    mocks.params = new URLSearchParams({
      new: '1',
      pdfDocumentId: id,
      pdfStartIndex: '0',
      pdfEndIndex: '2',
    });
    mocks.getDocument.mockResolvedValue({
      id,
      title: 'Paper',
      filename: 'paper.pdf',
      file_type: 'pdf',
    });
  });

  it('adopts only the resolved PDF into the composer and viewer and consumes the route intent', async () => {
    renderHook(() => usePdfChatRoute(true), { wrapper });
    await waitFor(() =>
      expect(mocks.setPdfContext).toHaveBeenCalledWith({
        documentId: id,
        title: 'Paper',
        selectionPageRange: [0, 2],
      })
    );
    expect(mocks.getDocument).toHaveBeenCalledWith(id);
    expect(useArtifactPanelStore.getState().artifact).toEqual({
      kind: 'document',
      id,
      title: 'Paper',
      targetPage: 1,
    });
    expect(mocks.replace).toHaveBeenCalledWith('/chat?new=1', {
      scroll: false,
    });
  });

  it('does not request an invalid range from the document service', () => {
    mocks.params.set('pdfEndIndex', '-1');
    const { result } = renderHook(() => usePdfChatRoute(true), { wrapper });
    expect(result.current.error).toMatch(/invalid document or page range/);
    expect(mocks.getDocument).not.toHaveBeenCalled();
    expect(mocks.setPdfContext).not.toHaveBeenCalled();
  });

  it('adopts an owned quote without turning its origin pages into a retrieval restriction', async () => {
    mocks.params.delete('pdfStartIndex');
    mocks.params.delete('pdfEndIndex');
    mocks.params.set('pdfQuoteIntent', 'opaque-token');
    mocks.params.set('pdfQuoteStartIndex', '1');
    mocks.params.set('pdfQuoteEndIndex', '2');
    mocks.consumeQuote.mockReturnValue('> private passage');
    renderHook(() => usePdfChatRoute(true), { wrapper });
    await waitFor(() =>
      expect(mocks.setPdfContext).toHaveBeenCalledWith({
        documentId: id,
        title: 'Paper',
        selectionQuoteRange: [1, 2],
        selectionQuote: '> private passage',
      })
    );
    expect(mocks.consumeQuote).toHaveBeenCalledExactlyOnceWith(
      'opaque-token',
      id,
      [1, 2]
    );
    expect(useArtifactPanelStore.getState().artifact).toMatchObject({
      targetPage: 2,
    });
    expect(mocks.replace).toHaveBeenCalledWith('/chat?new=1', {
      scroll: false,
    });
  });

  it('keeps explicit restrictions separate when a quote is also present', async () => {
    mocks.params.set('pdfQuoteIntent', 'opaque-token');
    mocks.params.set('pdfQuoteStartIndex', '1');
    mocks.params.set('pdfQuoteEndIndex', '1');
    mocks.consumeQuote.mockReturnValue('> passage');
    renderHook(() => usePdfChatRoute(true), { wrapper });
    await waitFor(() =>
      expect(mocks.setPdfContext).toHaveBeenCalledWith({
        documentId: id,
        title: 'Paper',
        selectionPageRange: [0, 2],
        selectionQuote: '> passage',
        selectionQuoteRange: [1, 1],
      })
    );
  });

  it('blocks malformed quote provenance before querying the document', () => {
    mocks.params.set('pdfQuoteStartIndex', '-1');
    const { result } = renderHook(() => usePdfChatRoute(true), { wrapper });
    expect(result.current.error).toMatch(/invalid document or page range/);
    expect(mocks.getDocument).not.toHaveBeenCalled();
  });
});
