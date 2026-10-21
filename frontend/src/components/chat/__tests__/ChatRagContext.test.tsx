import { fireEvent, render, screen } from '@/test/test-utils';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { useRef, type ReactElement } from 'react';
import type { PdfChatContext } from '@/types/pdf-chat';

import { ChatRagProvider, useChatRag } from '../ChatRagContext';
import { useArtifactPanelStore } from '@/store/artifactPanelStore';

const scope = vi.hoisted(() => ({ current: 'account-A:thread-A' }));
vi.mock('@/hooks/chat/useArtifactScope', () => ({
  useArtifactScope: () => scope.current,
}));

function PdfHarness(): ReactElement {
  const {
    pdfContext,
    setPdfContext,
    consumePdfSelection,
    preparePdfThreadAdoption,
  } = useChatRag();
  const captured = useRef<PdfChatContext | null>(null);
  return (
    <>
      <p>{pdfContext?.title ?? 'No PDF selected'}</p>
      <output aria-label="Selection">
        {JSON.stringify({
          quote: pdfContext?.selectionQuote,
          range: pdfContext?.selectionPageRange,
          quoteRange: pdfContext?.selectionQuoteRange,
        })}
      </output>
      <button
        onClick={() =>
          setPdfContext({
            documentId: 'doc-1',
            title: 'Paper A',
            selectionPageRange: [0, 2],
            selectionQuote: '> old quote',
            selectionQuoteRange: [1, 1],
          })
        }
      >
        Choose PDF
      </button>
      <button
        onClick={() => {
          captured.current = pdfContext;
        }}
      >
        Capture send
      </button>
      <button
        onClick={() => {
          if (captured.current) consumePdfSelection(captured.current);
        }}
      >
        Complete send
      </button>
      <button
        onClick={() =>
          setPdfContext({
            documentId: 'doc-1',
            title: 'Paper A',
            selectionPageRange: [3, 3],
            selectionQuote: '> new quote',
          })
        }
      >
        Choose newer quote
      </button>
      <button onClick={() => preparePdfThreadAdoption('created-thread', null)}>
        Adopt created thread
      </button>
    </>
  );
}

function ToggleHarness(): ReactElement {
  const { ragEnabled, setRagEnabled } = useChatRag();
  return (
    <button type="button" onClick={() => setRagEnabled((enabled) => !enabled)}>
      {String(ragEnabled)}
    </button>
  );
}

describe('ChatRagProvider', () => {
  beforeEach(() => {
    scope.current = 'account-A:thread-A';
  });
  it('owns one toggle that can move in both directions', () => {
    render(
      <ChatRagProvider>
        <ToggleHarness />
      </ChatRagProvider>
    );

    const toggle = screen.getByRole('button');
    expect(toggle).toHaveTextContent('true');
    fireEvent.click(toggle);
    expect(toggle).toHaveTextContent('false');
    fireEvent.click(toggle);
    expect(toggle).toHaveTextContent('true');
  });

  it('consumes the quote and its provenance while preserving an explicit retrieval range', () => {
    render(
      <ChatRagProvider>
        <PdfHarness />
      </ChatRagProvider>
    );
    fireEvent.click(screen.getByRole('button', { name: 'Choose PDF' }));
    fireEvent.click(screen.getByRole('button', { name: 'Capture send' }));
    fireEvent.click(screen.getByRole('button', { name: 'Complete send' }));
    expect(screen.getByText('Paper A')).toBeInTheDocument();
    expect(screen.getByLabelText('Selection')).toHaveTextContent(
      '{"range":[0,2]}'
    );
  });

  it('never clears a newer quote when the captured send completes', () => {
    // Mutation-verified: removing ChatRagContext.ts:94's expected-context
    // identity check clears the newer quote and fails this assertion.
    // pnpm --dir frontend exec vitest run src/components/chat/__tests__/ChatRagContext.test.tsx -t 'never clears a newer quote'
    render(
      <ChatRagProvider>
        <PdfHarness />
      </ChatRagProvider>
    );
    fireEvent.click(screen.getByRole('button', { name: 'Choose PDF' }));
    fireEvent.click(screen.getByRole('button', { name: 'Capture send' }));
    fireEvent.click(screen.getByRole('button', { name: 'Choose newer quote' }));
    fireEvent.click(screen.getByRole('button', { name: 'Complete send' }));
    expect(screen.getByLabelText('Selection')).toHaveTextContent('new quote');
    expect(screen.getByLabelText('Selection')).toHaveTextContent('[3,3]');
  });

  // Mutation check: disabling ChatRagProvider's pdf.scope !== scope reset
  // resurrects Paper A when returning to the old scope and fails this test.
  // Verified by removing that reset, observing failure, restoring, then:
  // pnpm --dir frontend exec vitest run src/components/chat/__tests__/ChatRagContext.test.tsx -t 'clears PDF scope'
  it('clears PDF scope on navigation and does not resurrect it on returning', () => {
    const tree = (): ReactElement => (
      <ChatRagProvider>
        <PdfHarness />
      </ChatRagProvider>
    );
    const { rerender } = render(tree());
    fireEvent.click(screen.getByRole('button', { name: 'Choose PDF' }));
    expect(screen.getByText('Paper A')).toBeInTheDocument();
    scope.current = 'account-B:thread-B';
    rerender(tree());
    expect(screen.getByText('No PDF selected')).toBeInTheDocument();
    scope.current = 'account-A:thread-A';
    rerender(tree());
    expect(screen.getByText('No PDF selected')).toBeInTheDocument();
  });

  it('transfers only an explicitly created thread receipt and its PDF viewer', () => {
    const draft = JSON.stringify([
      'account-A',
      'org-A',
      'workspace-A',
      null,
      null,
    ]);
    const created = JSON.stringify([
      'account-A',
      'org-A',
      'workspace-A',
      null,
      'created-thread',
    ]);
    scope.current = draft;
    useArtifactPanelStore.getState().setScope(draft);
    useArtifactPanelStore.getState().openArtifact({
      kind: 'document',
      id: 'doc-1',
      title: 'Paper A',
      targetPage: 1,
    });
    const tree = (): ReactElement => (
      <ChatRagProvider>
        <PdfHarness />
      </ChatRagProvider>
    );
    const { rerender } = render(tree());
    fireEvent.click(screen.getByRole('button', { name: 'Choose PDF' }));
    fireEvent.click(
      screen.getByRole('button', { name: 'Adopt created thread' })
    );
    scope.current = created;
    rerender(tree());
    expect(screen.getByText('Paper A')).toBeInTheDocument();
    expect(useArtifactPanelStore.getState()).toMatchObject({
      scope: created,
      artifact: { kind: 'document', id: 'doc-1', targetPage: 1 },
    });
    scope.current = JSON.stringify([
      'account-A',
      'org-A',
      'workspace-A',
      null,
      'other-thread',
    ]);
    rerender(tree());
    expect(screen.getByText('No PDF selected')).toBeInTheDocument();
  });
});
