import { act, render } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { DocumentPreviewTab } from '../DocumentPreviewTab';
import { consumePdfQuoteIntent } from '@/lib/pdf/chat-intent';
import type { PdfAskHandler } from '@/types/pdf-chat';

const mocks = vi.hoisted(() => ({
  push: vi.fn(),
  initialize: vi.fn(),
  workspace: null as string | null,
  error: null as string | null,
  epoch: 0,
  auth: {
    isAuthenticated: true,
    user: { id: 'user-A', organization_id: 'org-A' },
  },
  workspaceChanged: () => {},
  authChanged: () => {},
  reset: () => {},
  ask: (() => {}) as PdfAskHandler,
}));
vi.mock('next/navigation', () => ({ useRouter: () => ({ push: mocks.push }) }));
vi.mock('@/stores/authStore', () => ({
  useAuthStore: {
    getState: () => mocks.auth,
    subscribe: (listener: () => void) => {
      mocks.authChanged = listener;
    },
  },
}));
vi.mock('@/store/chat-store', () => ({
  useChatStore: {
    getState: () => ({
      currentWorkspaceId: mocks.workspace,
      initializeDefaultWorkspace: mocks.initialize,
      error: mocks.error,
      isLoadingConversations: false,
    }),
    subscribe: (listener: () => void) => {
      mocks.workspaceChanged = listener;
    },
  },
  captureChatSession: () => {
    const epoch = mocks.epoch;
    return () => mocks.epoch === epoch;
  },
  onChatSessionReset: (listener: () => void) => {
    mocks.reset = listener;
  },
}));
vi.mock('../DocumentInlineViewer', () => ({
  DocumentInlineViewer: ({
    onAskAboutPdf,
  }: {
    onAskAboutPdf: PdfAskHandler;
  }) => {
    mocks.ask = onAskAboutPdf;
    return null;
  },
}));
const quote = '> private selected passage';
function ready(): void {
  mocks.workspace = 'resolved-workspace';
  mocks.workspaceChanged();
}
function consumeRoutedQuote(): string | undefined {
  const url = new URL(mocks.push.mock.calls.at(-1)![0], 'https://nous.test');
  expect(url.pathname).toBe('/chat');
  expect(url.searchParams.get('pdfDocumentId')).toBe('document-A');
  expect(url.searchParams.get('pdfStartIndex')).toBeNull();
  expect(url.searchParams.get('pdfEndIndex')).toBeNull();
  expect(url.searchParams.get('pdfQuoteStartIndex')).toBe('1');
  expect(url.searchParams.get('pdfQuoteEndIndex')).toBe('1');
  expect(url.toString()).not.toContain('private');
  return consumePdfQuoteIntent(
    url.searchParams.get('pdfQuoteIntent'),
    'document-A',
    [1, 1]
  );
}

describe('document quote navigation', () => {
  beforeEach(() => {
    mocks.reset();
    vi.clearAllMocks();
    mocks.workspace = null;
    mocks.error = null;
    mocks.epoch = 0;
    mocks.auth = {
      isAuthenticated: true,
      user: { id: 'user-A', organization_id: 'org-A' },
    };
    mocks.initialize.mockImplementation(async () => ready());
  });

  it.each([null, 'old-workspace'])(
    'prepares the actual destination before binding the quote from %s',
    async (initial) => {
      mocks.workspace = initial;
      render(
        <DocumentPreviewTab documentId="document-A" filename="paper.pdf" />
      );
      await act(async () => {
        expect(await mocks.ask(undefined, quote, [1, 1])).toBe(true);
      });
      expect(mocks.initialize).toHaveBeenCalledOnce();
      expect(consumeRoutedQuote()).toBe(quote);
    }
  );

  it('does not navigate or stage a quote after failed setup and supports retry', async () => {
    mocks.initialize.mockRejectedValueOnce(new Error('offline'));
    render(<DocumentPreviewTab documentId="document-A" filename="paper.pdf" />);
    await act(async () => {
      await expect(mocks.ask(undefined, quote, [1, 1])).rejects.toThrow(
        'offline'
      );
    });
    expect(mocks.push).not.toHaveBeenCalled();
    await act(async () => {
      expect(await mocks.ask(undefined, quote, [1, 1])).toBe(true);
    });
    expect(consumeRoutedQuote()).toBe(quote);
  });

  it('routes an explicit restriction independently of quote provenance', async () => {
    render(<DocumentPreviewTab documentId="document-A" filename="paper.pdf" />);
    await act(async () => {
      expect(await mocks.ask([0, 4], quote, [1, 1])).toBe(true);
    });
    const url = new URL(mocks.push.mock.calls[0][0], 'https://nous.test');
    expect(url.searchParams.get('pdfStartIndex')).toBe('0');
    expect(url.searchParams.get('pdfEndIndex')).toBe('4');
    expect(url.searchParams.get('pdfQuoteStartIndex')).toBe('1');
    expect(url.searchParams.get('pdfQuoteEndIndex')).toBe('1');
    expect(
      consumePdfQuoteIntent(
        url.searchParams.get('pdfQuoteIntent'),
        'document-A',
        [1, 1]
      )
    ).toBe(quote);
  });

  it('does not accept a silently failed or unsettled workspace bootstrap', async () => {
    mocks.initialize.mockImplementation(async () => {
      ready();
      mocks.error = 'Failed to load conversations';
    });
    render(<DocumentPreviewTab documentId="document-A" filename="paper.pdf" />);
    await act(async () => {
      await expect(mocks.ask(undefined, quote, [1, 1])).rejects.toThrow(
        'workspace is unavailable'
      );
    });
    expect(mocks.push).not.toHaveBeenCalled();
  });

  // Mutation verified independently: remove the document lifetime (51), session
  // epoch (52), or actor identity (54-55) checks in DocumentPreviewTab.tsx.
  // pnpm --dir frontend test src/components/documents/__tests__/DocumentPreviewTab.pdfQuote.test.tsx -t 'stale (account|session|document|unmount)'
  it.each(['account', 'session', 'document', 'unmount'])(
    'cannot stage or navigate for a stale %s after pending setup',
    async (change) => {
      let resolve!: () => void;
      mocks.initialize.mockImplementation(
        () =>
          new Promise<void>((done) => {
            resolve = () => {
              ready();
              done();
            };
          })
      );
      const { rerender, unmount } = render(
        <DocumentPreviewTab documentId="document-A" filename="paper.pdf" />
      );
      let pending!: ReturnType<PdfAskHandler>;
      act(() => {
        pending = mocks.ask(undefined, quote, [1, 1]);
      });
      if (change === 'account') {
        mocks.auth = {
          isAuthenticated: true,
          user: { id: 'user-B', organization_id: 'org-B' },
        };
        mocks.authChanged();
      }
      if (change === 'session') mocks.epoch += 1;
      if (change === 'document')
        rerender(
          <DocumentPreviewTab documentId="document-B" filename="other.pdf" />
        );
      if (change === 'unmount') unmount();
      await act(async () => {
        resolve();
        expect(await pending).toBe(false);
      });
      expect(mocks.push).not.toHaveBeenCalled();
    }
  );
});
