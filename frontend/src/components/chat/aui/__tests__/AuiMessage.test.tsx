import { afterEach, describe, expect, it, vi } from 'vitest';
import React from 'react';
import {
  act,
  fireEvent,
  render,
  screen,
  waitFor,
  within,
  type RenderResult,
} from '@testing-library/react';

import { makeChatPageMessage } from '@/test/chatMessageFactory';
import { ChatRuntimeProvider } from '../ChatRuntimeProvider';
import {
  AuiMessages,
  AuiMessageByIndex,
  MessageByIndexBoundary,
  RETRY_UNAVAILABLE_REASON,
} from '../AuiMessage';

const noop = vi.fn();

afterEach(() => {
  vi.restoreAllMocks();
});

function Thrower({
  shouldThrow,
  message,
  children,
}: {
  shouldThrow: boolean;
  message: string;
  children?: React.ReactNode;
}): React.ReactElement {
  if (shouldThrow) throw new Error(message);
  return <>{children}</>;
}

function captureAnimationFrames(): {
  run: (id: number) => void;
  runEvenIfCancelled: (id: number) => void;
} {
  let nextId = 1;
  const callbacks = new Map<number, FrameRequestCallback>();
  const allCallbacks = new Map<number, FrameRequestCallback>();
  vi.spyOn(window, 'requestAnimationFrame').mockImplementation((callback) => {
    const id = nextId;
    nextId += 1;
    callbacks.set(id, callback);
    allCallbacks.set(id, callback);
    return id;
  });
  vi.spyOn(window, 'cancelAnimationFrame').mockImplementation((id) => {
    callbacks.delete(id);
  });

  return {
    run(id: number) {
      const callback = callbacks.get(id);
      callbacks.delete(id);
      callback?.(0);
    },
    runEvenIfCancelled(id: number) {
      allCallbacks.get(id)?.(0);
    },
  };
}

function renderMessages(
  messages: Array<Parameters<typeof makeChatPageMessage>[0]>
): RenderResult {
  const completeMessages = messages.map(makeChatPageMessage);
  return render(
    <ChatRuntimeProvider
      messages={completeMessages}
      isRunning={false}
      onSend={noop}
      onCancel={noop}
    >
      <AuiMessages />
    </ChatRuntimeProvider>
  );
}

describe('AuiMessage', () => {
  it('renders user and assistant text parts through MessagePrimitive.Parts', () => {
    renderMessages([
      { id: 'u1', role: 'user', content: 'What is RAG?', timestamp: 1 },
      {
        id: 'a1',
        role: 'assistant',
        content: 'Retrieval augmented generation.',
        timestamp: 2,
      },
    ]);

    expect(screen.getByText('What is RAG?')).toBeInTheDocument();
    expect(
      screen.getByText('Retrieval augmented generation.')
    ).toBeInTheDocument();
    expect(document.querySelector('[data-role="user"]')).toBeTruthy();
    expect(document.querySelector('[data-role="assistant"]')).toBeTruthy();
  });

  it('renders image and document attachments before user message parts', () => {
    renderMessages([
      {
        id: 'u1',
        role: 'user',
        content: 'Please inspect these.',
        timestamp: 1,
        attachments: [
          {
            id: 'img-1',
            document_id: 'doc-img',
            display_name: 'figure.png',
            thumbnail_url: 'https://example.test/figure.png',
            mime_type: 'image/png',
          },
          {
            id: 'doc-1',
            document_id: 'doc-pdf',
            document_title: 'paper.pdf',
            mime_type: 'application/pdf',
          },
        ],
      },
    ]);

    expect(screen.getByAltText('figure.png')).toHaveAttribute(
      'src',
      'https://example.test/figure.png'
    );
    expect(screen.getByText('paper.pdf')).toBeInTheDocument();
    expect(screen.getByText('PDF')).toBeInTheDocument();
  });

  it('renders tool-call parts with the shared ToolFallback UI', () => {
    renderMessages([
      {
        id: 'a1',
        role: 'assistant',
        content: 'Found two papers.',
        timestamp: 2,
        toolExecutions: [
          {
            tool: 'search_arxiv',
            label: 'Search arXiv',
            status: 'done',
            argsSummary: 'query: rag',
            resultSummary: '2 papers',
          },
        ],
      },
    ]);

    expect(
      document.querySelector('[data-slot="tool-fallback-root"]')
    ).toBeTruthy();
    // The swap label renders the tool name on both layers, so scope the
    // assertion to the resting one.
    expect(
      document.querySelector('[data-slot="tool-fallback-trigger-label"]')
    ).toHaveTextContent('Search arXiv');
  });

  it('adds an autohiding action bar inside MessagePrimitive.Root for hover-driven controls', async () => {
    renderMessages([
      { id: 'a1', role: 'assistant', content: 'Copy me', timestamp: 2 },
    ]);

    const root = document.querySelector('[data-role="assistant"]');
    expect(root).toBeTruthy();
    fireEvent.mouseEnter(root as Element);

    const actionBar = await waitFor(() => {
      const node = document.querySelector('[data-slot="aui-message-actions"]');
      expect(node).toBeTruthy();
      return node;
    });
    expect(actionBar).toBeTruthy();
    expect(actionBar).toHaveAttribute('data-aui-autohide', 'not-last');
    expect(
      screen.getByRole('button', { name: /copy assistant message/i })
    ).toBeInTheDocument();
  });
});

describe('AuiAssistantMessage committed-path chrome (ChatBubble parity)', () => {
  function renderByIndex(
    messages: Array<Parameters<typeof makeChatPageMessage>[0]>,
    opts: {
      index?: number;
      onCitationClick?: (
        citations: unknown[],
        clicked: unknown,
        traceId?: string
      ) => void;
    } = {}
  ): RenderResult {
    const completeMessages = messages.map(makeChatPageMessage);
    const index = opts.index ?? completeMessages.length - 1;
    return render(
      <ChatRuntimeProvider
        messages={completeMessages}
        isRunning={false}
        onSend={noop}
        onCancel={noop}
      >
        <AuiMessageByIndex
          index={index}
          message={completeMessages[index]}
          onCitationClick={opts.onCitationClick as never}
        />
      </ChatRuntimeProvider>
    );
  }

  it('keeps the rating outside the autohiding bar but on the same row', () => {
    // Index 0 of two: `autohide="not-last"` keeps the last message's bar
    // mounted, so an older turn is the one that exercises autohiding.
    renderByIndex(
      [
        { id: 'a1', role: 'assistant', content: 'A', timestamp: 2 },
        { id: 'u1', role: 'user', content: 'B', timestamp: 3 },
      ],
      { index: 0 }
    );

    // ActionBarPrimitive.Root unmounts when the message is not hovered, so a
    // rating rendered inside it would vanish — taking any half-typed feedback
    // note with it. It must be a sibling, sharing the row.
    const row = document.querySelector('.nous-msg-actionrow');
    expect(row).toBeTruthy();
    expect(row?.querySelector('.nous-msg-feedback')).toBeTruthy();
    expect(row?.querySelector('[aria-label="Good response"]')).toBeTruthy();

    // Not hovered: the bar is absent, the rating is not.
    expect(
      document.querySelector('[data-slot="aui-message-actions"]')
    ).toBeNull();
  });

  it('renders assistant markdown (bold), not raw asterisks', () => {
    renderByIndex([
      {
        id: 'a1',
        role: 'assistant',
        content: 'This is **important** context.',
        timestamp: 2,
      },
    ]);

    const strong = document.querySelector('strong');
    expect(strong).toBeTruthy();
    expect(strong?.textContent).toBe('important');
    expect(screen.queryByText(/\*\*important\*\*/)).not.toBeInTheDocument();
  });

  it('renders the tool strip with per-turn token usage and response time', () => {
    renderByIndex([
      {
        id: 'a1',
        role: 'assistant',
        content: 'Answer.',
        timestamp: 2,
        metadata: {
          responseTimeMs: 2300,
          tokenUsage: { input: 1234, output: 340 },
        },
      },
    ]);

    expect(screen.getByText('2.3s')).toBeInTheDocument();
    expect(screen.getByText(/1\.2k in/)).toBeInTheDocument();
    expect(screen.getByText(/340 out/)).toBeInTheDocument();
    expect(
      document.querySelectorAll('[data-slot="message-timing"]')
    ).toHaveLength(1);
  });

  it('keeps a committed failed tool failed even when it has a result payload', () => {
    renderByIndex([
      {
        id: 'a1',
        role: 'assistant',
        content: 'I continued with the available evidence.',
        timestamp: 2,
        toolExecutions: [
          {
            tool: 'search_arxiv',
            label: 'Search arXiv',
            status: 'error',
            resultSummary: 'arXiv timed out',
          },
        ],
      },
    ]);

    expect(
      document.querySelector('[data-slot="tool-fallback-trigger-label"]')
    ).toHaveTextContent('Failed Search arXiv');
    expect(
      document.querySelector('[data-slot="tool-fallback-trigger-check"]')
    ).toBeNull();
  });

  it('preserves a registered tool UI for a successful committed execution', async () => {
    renderByIndex([
      {
        id: 'a1',
        role: 'assistant',
        content: 'I found two documents.',
        timestamp: 2,
        toolExecutions: [
          {
            id: 'call-1',
            tool: 'search_documents',
            label: 'Search documents',
            status: 'done',
            args: { query: 'retrieval' },
            argsSummary: 'query: retrieval',
            result: { total: 2, documents: [{ id: 'd1' }, { id: 'd2' }] },
            resultSummary: '2 documents',
          },
        ],
      },
    ]);

    await waitFor(() => {
      expect(
        document.querySelector('[data-slot="search-documents-tool"]')
      ).toBeTruthy();
    });
    expect(screen.getByText('retrieval')).toBeInTheDocument();
    expect(screen.getByText('2 results')).toBeInTheDocument();
    expect(
      document.querySelector('[data-slot="tool-fallback-root"]')
    ).toBeNull();
  });

  it('renders the numbered sources list and forwards clicks', () => {
    const onCitationClick = vi.fn();
    renderByIndex(
      [
        {
          id: 'a1',
          role: 'assistant',
          content: 'Cited answer.',
          timestamp: 2,
          citations: [
            {
              documentId: 'd1',
              title: 'Attention Is All You Need',
              score: 0.92,
            },
          ],
        },
      ],
      { onCitationClick }
    );

    const row = screen.getByText('Attention Is All You Need');
    expect(row).toBeInTheDocument();
    // Relevance percentages moved to the retrieval diagnostics view.
    expect(screen.queryByText('92%')).toBeNull();
    fireEvent.click(row.closest('button') as HTMLButtonElement);
    expect(onCitationClick).toHaveBeenCalledTimes(1);
  });

  it('resolves raw Doc positions before applying document-grouped display numbers', () => {
    renderByIndex([
      {
        id: 'a1',
        role: 'assistant',
        content: 'First [Doc 1], second [Doc 2], same paper [Doc 3].',
        timestamp: 2,
        citations: [
          {
            documentId: 'doc-a',
            title: 'Paper A',
            score: 0.9,
            chunkId: 'a-1',
            chunkIndex: 1,
            pageNumber: 4,
          },
          {
            documentId: 'doc-b',
            title: 'Paper B',
            score: 0.8,
            chunkId: 'b-1',
            chunkIndex: 1,
            pageNumber: 8,
          },
          {
            documentId: 'doc-a',
            title: 'Paper A',
            score: 0.7,
            chunkId: 'a-2',
            chunkIndex: 2,
            pageNumber: 5,
          },
        ],
      },
    ]);

    // Raw Doc 1 and Doc 3 both resolve against the canonical three-item
    // array, then share Paper A's reader-facing numeral.
    expect(
      screen.getAllByRole('button', { name: 'Source 1: Paper A' })
    ).toHaveLength(2);
    expect(
      screen.getByRole('button', { name: 'Source 2: Paper B' })
    ).toBeInTheDocument();
    expect(
      within(screen.getByRole('list')).getAllByRole('listitem')
    ).toHaveLength(2);
  });

  it('renders the committed execution plan', () => {
    renderByIndex([
      {
        id: 'a1',
        role: 'assistant',
        content: 'Planned answer.',
        timestamp: 2,
        plan: [
          {
            step: 1,
            description: 'Search arXiv',
            tool: 'search_arxiv',
            args_hint: {},
            depends_on: [],
          },
        ],
      },
    ]);

    expect(screen.getByText('Execution plan · 0/1')).toBeInTheDocument();
  });

  it('renders persisted progress in a collapsed reasoning panel', () => {
    renderByIndex([
      {
        id: 'a1',
        role: 'assistant',
        content: 'Prepared answer.',
        timestamp: 2,
        progressSteps: [
          { phase: 'accepted', detail: 'Request accepted' },
          { phase: 'writing', detail: 'Drafting the response' },
        ],
      },
    ]);

    const trigger = screen.getByRole('button', {
      name: /how this answer was prepared/i,
    });
    expect(trigger).toHaveAttribute('data-state', 'closed');
    fireEvent.click(trigger);
    expect(trigger).toHaveAttribute('data-state', 'open');
    expect(screen.getByText('Request accepted')).toBeInTheDocument();
    expect(screen.getByText('Drafting the response')).toBeInTheDocument();
  });
});

describe('AuiMessageByIndex runtime-sync race', () => {
  // Mutation proof: removing AuiMessageByIndex's runtimeMessageId mismatch
  // guard must fail this test on the stale Approval needed dialog.
  // From frontend: vitest run src/components/chat/aui/__tests__/AuiMessage.test.tsx -t 'stale approval'
  it('does not render a stale approval in a completed answer slot before runtime sync', async () => {
    const approval = makeChatPageMessage({
      runtimeId: 'approval:thread-1',
      source: 'local-only',
      role: 'assistant',
      content: '',
      pendingApproval: {
        id: 'gate-1',
        tools: [{ name: 'create_project', args: { name: 'Research' } }],
      },
    });
    const answer = makeChatPageMessage({
      runtimeId: 'answer-1',
      role: 'assistant',
      content: 'Project created.',
    });
    const renderFrame = (
      runtimeMessages: (typeof approval)[],
      row = answer
    ): React.ReactElement => (
      <ChatRuntimeProvider
        messages={runtimeMessages}
        isRunning={false}
        onSend={noop}
        onCancel={noop}
      >
        <AuiMessageByIndex index={0} message={row} />
      </ChatRuntimeProvider>
    );

    // The list has the answer, but the post-commit runtime still has the gate
    // at the same in-bounds index. Never mount that gate in the answer row.
    const { rerender } = render(renderFrame([approval], approval));
    await screen.findByRole('alertdialog', { name: 'Approval needed' });
    rerender(renderFrame([approval]));
    expect(
      screen.queryByRole('alertdialog', { name: 'Approval needed' })
    ).not.toBeInTheDocument();
    rerender(renderFrame([answer]));
    await waitFor(() =>
      expect(screen.getByText('Project created.')).toBeInTheDocument()
    );
    expect(
      screen.queryByRole('alertdialog', { name: 'Approval needed' })
    ).not.toBeInTheDocument();
  });

  it('hands an optimistic row to its canonical replacement without a duplicate bubble', async () => {
    const optimistic = makeChatPageMessage({
      runtimeId: 'runtime-answer',
      source: 'optimistic',
      role: 'assistant',
      content: 'Answer survives reconciliation.',
      timestamp: 2,
    });
    const canonical = makeChatPageMessage({
      id: 'db-answer',
      runtimeId: 'runtime-answer',
      source: 'canonical',
      role: 'assistant',
      content: 'Answer survives reconciliation.',
      timestamp: 2,
    });

    const { rerender } = render(
      <ChatRuntimeProvider
        messages={[optimistic]}
        isRunning={false}
        onSend={noop}
        onCancel={noop}
      >
        <AuiMessages />
      </ChatRuntimeProvider>
    );
    const originalBubble = document.querySelector('[data-role="assistant"]');

    rerender(
      <ChatRuntimeProvider
        messages={[canonical]}
        isRunning={false}
        onSend={noop}
        onCancel={noop}
      >
        <AuiMessages />
      </ChatRuntimeProvider>
    );

    await waitFor(() =>
      expect(
        screen.getAllByText('Answer survives reconciliation.')
      ).toHaveLength(1)
    );
    expect(document.querySelector('[data-role="assistant"]')).toBe(
      originalBubble
    );
  });

  it('renders nothing instead of throwing when the runtime thread is behind the list', () => {
    // The external-store runtime syncs post-commit (useEffect), so the list
    // can render an index the runtime doesn't have yet — e.g. thread switch
    // or first send. Regression: useClientLookup "Index 0 out of bounds".
    const staleMessage = makeChatPageMessage({
      id: 'a1',
      role: 'assistant',
      content: 'Not yet in runtime.',
      timestamp: 2,
    });

    expect(() =>
      render(
        <ChatRuntimeProvider
          messages={[]}
          isRunning={false}
          onSend={noop}
          onCancel={noop}
        >
          <AuiMessageByIndex index={0} message={staleMessage} />
        </ChatRuntimeProvider>
      )
    ).not.toThrow();

    expect(screen.queryByText('Not yet in runtime.')).not.toBeInTheDocument();
  });

  it('renders nothing instead of throwing when the runtime shrinks under a mounted message (thread switch)', () => {
    // Thread switch: the list mounts MessageByIndex(0) for thread A, then the
    // active thread's messages are cleared/replaced. The external-store runtime
    // syncs post-commit, so for one frame the runtime empties while the
    // still-mounted MessageByIndex(0) subscription re-reads its snapshot —
    // useClientLookup(0) on an empty thread. The render-time count guard cannot
    // catch this: the throw originates in the child's store-driven update, not
    // the parent's render. Prod crash: "useClientLookup: Index 0 out of bounds
    // (length: 0)" on thread switch.
    const msg = makeChatPageMessage({
      id: 'a1',
      role: 'assistant',
      content: 'From thread A.',
      timestamp: 2,
    });

    const { rerender } = render(
      <ChatRuntimeProvider
        messages={[msg]}
        isRunning={false}
        onSend={noop}
        onCancel={noop}
      >
        <AuiMessageByIndex index={0} message={msg} />
      </ChatRuntimeProvider>
    );
    expect(screen.getByText('From thread A.')).toBeInTheDocument();

    expect(() =>
      rerender(
        <ChatRuntimeProvider
          messages={[]}
          isRunning={false}
          onSend={noop}
          onCancel={noop}
        >
          <AuiMessageByIndex index={0} message={msg} />
        </ChatRuntimeProvider>
      )
    ).not.toThrow();

    expect(screen.queryByText('From thread A.')).not.toBeInTheDocument();
  });
});

describe('AuiUserMessage inline edit-and-resend', () => {
  function renderUserRow(
    opts: {
      onEdit?: (content: string) => void;
      editDisabled?: boolean;
    } = {}
  ): RenderResult {
    const message = makeChatPageMessage({
      id: 'u1',
      role: 'user',
      content: 'original question',
      timestamp: 1,
    });
    return render(
      <ChatRuntimeProvider
        messages={[message]}
        isRunning={false}
        onSend={noop}
        onCancel={noop}
      >
        <AuiMessageByIndex
          index={0}
          message={message}
          onEdit={opts.onEdit ?? noop}
          editDisabled={opts.editDisabled}
        />
      </ChatRuntimeProvider>
    );
  }

  // The action bar autohides; hovering the row is what mounts its controls.
  async function openEditor(): Promise<HTMLElement> {
    fireEvent.mouseEnter(document.querySelector('[data-role="user"]')!);
    const trigger = await screen.findByRole('button', {
      name: /edit and resend/i,
    });
    fireEvent.click(trigger);
    return trigger;
  }

  it('keeps the draft and disables Save while a turn is in flight', async () => {
    // Regression: the surface dropped the edit callback when the session
    // could not accept a resend, but the editor had already closed — the
    // user's rewritten message vanished with no explanation.
    const onEdit = vi.fn();
    renderUserRow({ onEdit, editDisabled: true });
    await openEditor();

    const textarea = screen.getByLabelText(
      'Edit your message'
    ) as HTMLTextAreaElement;
    fireEvent.change(textarea, { target: { value: 'rewritten question' } });

    const save = screen.getByRole('button', { name: /save & resend/i });
    expect(save).toBeDisabled();
    expect(save).toHaveAttribute('aria-disabled', 'true');
    expect(
      screen.getByText(/wait for the current response to finish/i)
    ).toBeInTheDocument();

    fireEvent.click(save);
    fireEvent.keyDown(textarea, { key: 'Enter' });

    expect(onEdit).not.toHaveBeenCalled();
    // Editor still open, draft intact.
    expect(
      (screen.getByLabelText('Edit your message') as HTMLTextAreaElement).value
    ).toBe('rewritten question');
  });

  it('submits the edit when the session can accept a resend', async () => {
    const onEdit = vi.fn();
    renderUserRow({ onEdit, editDisabled: false });
    await openEditor();

    const textarea = screen.getByLabelText('Edit your message');
    fireEvent.change(textarea, { target: { value: 'rewritten question' } });
    fireEvent.click(screen.getByRole('button', { name: /save & resend/i }));

    expect(onEdit).toHaveBeenCalledWith('rewritten question');
    expect(
      screen.queryByLabelText('Edit your message')
    ).not.toBeInTheDocument();
  });

  it('does not submit on Enter while an IME composition is active', async () => {
    // Japanese/Chinese/Korean input: Enter confirms the IME candidate. Acting
    // on it would submit half-typed text AND preventDefault would break the
    // composition itself.
    const onEdit = vi.fn();
    renderUserRow({ onEdit, editDisabled: false });
    await openEditor();

    const textarea = screen.getByLabelText('Edit your message');
    fireEvent.change(textarea, { target: { value: 'にほん' } });
    fireEvent.keyDown(textarea, { key: 'Enter', isComposing: true });

    expect(onEdit).not.toHaveBeenCalled();
    // Editor stays open with the draft intact.
    expect(
      (screen.getByLabelText('Edit your message') as HTMLTextAreaElement).value
    ).toBe('にほん');

    // The same key once composition has ended does submit.
    fireEvent.keyDown(textarea, { key: 'Enter' });
    expect(onEdit).toHaveBeenCalledWith('にほん');
  });

  it('lands focus on the message container after Save, even when the edit trigger unmounts', async () => {
    // After Save the resend starts and the action bar hides its buttons
    // (hideWhenRunning), so focusing the trigger would be transient — focus
    // has to go somewhere that survives the turn.
    const onEdit = vi.fn();
    const view = renderUserRow({ onEdit, editDisabled: false });
    await openEditor();

    const textarea = screen.getByLabelText('Edit your message');
    fireEvent.change(textarea, { target: { value: 'rewritten question' } });
    fireEvent.click(screen.getByRole('button', { name: /save & resend/i }));

    // Simulate the resend starting: the runtime goes running and the action
    // bar (and with it the edit trigger) unmounts.
    view.rerender(
      <ChatRuntimeProvider
        messages={[
          makeChatPageMessage({
            id: 'u1',
            role: 'user',
            content: 'original question',
            timestamp: 1,
          }),
        ]}
        isRunning={true}
        onSend={noop}
        onCancel={noop}
      >
        <AuiMessageByIndex index={0} message={undefined} onEdit={onEdit} />
      </ChatRuntimeProvider>
    );

    const container = document.querySelector(
      '[data-role="user"] [tabindex="-1"]'
    );
    expect(container).toBeTruthy();
    await waitFor(() => expect(document.activeElement).toBe(container));
    expect(document.activeElement).not.toBe(document.body);
  });

  it('returns focus to the edit trigger after Escape', async () => {
    renderUserRow({ onEdit: vi.fn() });
    await openEditor();

    const textarea = screen.getByLabelText('Edit your message');
    fireEvent.keyDown(textarea, { key: 'Escape' });

    await waitFor(() =>
      expect(
        screen.queryByLabelText('Edit your message')
      ).not.toBeInTheDocument()
    );
    // Without an explicit restore, unmounting the focused textarea drops
    // focus to <body> and keyboard users lose their place. The trigger is a
    // FRESH node (the whole non-editing branch remounts), so re-query it.
    await waitFor(() =>
      expect(document.activeElement).toBe(
        screen.getByRole('button', { name: /edit and resend/i })
      )
    );
  });
});

describe('AuiAssistantMessage retry', () => {
  // AuiAssistantMessage reads message scope off ThreadPrimitive.MessageByIndex
  // context (mounting it directly throws "does not have a 'message'
  // property"), so this drives it the same way the edit-and-resend tests
  // above drive AuiUserMessage: through AuiMessageByIndex inside a real
  // ChatRuntimeProvider, revealing the autohiding action bar via hover.
  it('disables Regenerate with a reason while a turn is in flight', async () => {
    const assistantMessage = makeChatPageMessage({
      id: 'a1',
      role: 'assistant',
      content: 'answer',
      timestamp: 1,
    });
    render(
      <ChatRuntimeProvider
        messages={[assistantMessage]}
        isRunning={false}
        onSend={noop}
        onCancel={noop}
      >
        <AuiMessageByIndex
          index={0}
          message={assistantMessage}
          onRetry={vi.fn()}
          retryDisabled
        />
      </ChatRuntimeProvider>
    );
    fireEvent.mouseEnter(document.querySelector('[data-role="assistant"]')!);
    const btn = await screen.findByRole('button', {
      name: 'Regenerate response',
    });
    expect(btn).toBeDisabled();
    expect(btn).toHaveAttribute('title', RETRY_UNAVAILABLE_REASON);
  });
});

describe('MessageByIndexBoundary', () => {
  // jsdom flushes too synchronously to reproduce the concurrent-scheduler torn
  // read that crashes prod on thread switch, so these test the boundary's
  // contract directly: it must swallow ONLY the transient out-of-bounds throw,
  // surface everything else, and re-attempt once the runtime settles.
  it('swallows the out-of-bounds throw and renders nothing', () => {
    const spy = vi.spyOn(console, 'error').mockImplementation(() => {});
    expect(() =>
      render(
        <MessageByIndexBoundary resetKey="a">
          <Thrower
            shouldThrow
            message="useClientLookup: Index 0 out of bounds (length: 0)"
          >
            should not render
          </Thrower>
        </MessageByIndexBoundary>
      )
    ).not.toThrow();
    expect(screen.queryByText('should not render')).not.toBeInTheDocument();
    spy.mockRestore();
  });

  it('rethrows non-out-of-bounds errors so real bugs surface', () => {
    const spy = vi.spyOn(console, 'error').mockImplementation(() => {});
    expect(() =>
      render(
        <MessageByIndexBoundary resetKey="a">
          <Thrower
            shouldThrow
            message="Cannot read properties of undefined (reading 'foo')"
          />
        </MessageByIndexBoundary>
      )
    ).toThrow(/Cannot read properties/);
    spy.mockRestore();
  });

  it('re-attempts rendering after the runtime settles (resetKey change)', () => {
    const spy = vi.spyOn(console, 'error').mockImplementation(() => {});
    const { rerender } = render(
      <MessageByIndexBoundary resetKey="a">
        <Thrower
          shouldThrow
          message="useClientLookup: Index 0 out of bounds (length: 0)"
        >
          recovered
        </Thrower>
      </MessageByIndexBoundary>
    );
    expect(screen.queryByText('recovered')).not.toBeInTheDocument();

    rerender(
      <MessageByIndexBoundary resetKey="b">
        <Thrower shouldThrow={false} message="unused">
          recovered
        </Thrower>
      </MessageByIndexBoundary>
    );
    expect(screen.getByText('recovered')).toBeInTheDocument();
    spy.mockRestore();
  });

  it('re-attempts one transient out-of-bounds render after commit without a key change', () => {
    const spy = vi.spyOn(console, 'error').mockImplementation(() => {});
    const frames = captureAnimationFrames();
    let shouldThrow = true;
    const MutableThrower = (): React.ReactElement => {
      if (shouldThrow) {
        throw new Error('useClientLookup: Index 0 out of bounds (length: 0)');
      }
      return <>recovered after adapter sync</>;
    };
    render(
      <MessageByIndexBoundary resetKey="stable">
        <MutableThrower />
      </MessageByIndexBoundary>
    );

    expect(screen.queryByText('recovered after adapter sync')).toBeNull();
    expect(window.requestAnimationFrame).toHaveBeenCalledOnce();

    shouldThrow = false;
    act(() => frames.run(1));

    expect(screen.getByText('recovered after adapter sync')).toBeVisible();
    expect(window.requestAnimationFrame).toHaveBeenCalledOnce();
    spy.mockRestore();
  });

  it('bounds a persistent out-of-bounds error to one retry and a null fallback', () => {
    const spy = vi.spyOn(console, 'error').mockImplementation(() => {});
    const frames = captureAnimationFrames();
    render(
      <MessageByIndexBoundary resetKey="persistent">
        <Thrower
          shouldThrow
          message="useClientLookup: Index 0 out of bounds (length: 0)"
        >
          never visible
        </Thrower>
      </MessageByIndexBoundary>
    );

    expect(window.requestAnimationFrame).toHaveBeenCalledOnce();
    act(() => frames.run(1));

    expect(screen.queryByText('never visible')).toBeNull();
    expect(window.requestAnimationFrame).toHaveBeenCalledOnce();
    spy.mockRestore();
  });

  it('cancels a pending retry when it unmounts', () => {
    const spy = vi.spyOn(console, 'error').mockImplementation(() => {});
    captureAnimationFrames();
    const view = render(
      <MessageByIndexBoundary resetKey="unmounting">
        <Thrower
          shouldThrow
          message="useClientLookup: Index 0 out of bounds (length: 0)"
        />
      </MessageByIndexBoundary>
    );
    expect(window.requestAnimationFrame).toHaveBeenCalledOnce();

    view.unmount();

    expect(window.cancelAnimationFrame).toHaveBeenCalledWith(1);
    spy.mockRestore();
  });

  it('cancels and rejects a stale retry after the reset key changes', () => {
    const spy = vi.spyOn(console, 'error').mockImplementation(() => {});
    const frames = captureAnimationFrames();
    const Stable = vi.fn((): React.ReactElement => <>new message</>);
    const view = render(
      <MessageByIndexBoundary resetKey="old">
        <Thrower
          shouldThrow
          message="useClientLookup: Index 0 out of bounds (length: 0)"
        />
      </MessageByIndexBoundary>
    );
    expect(window.requestAnimationFrame).toHaveBeenCalledOnce();

    view.rerender(
      <MessageByIndexBoundary resetKey="new">
        <Stable />
      </MessageByIndexBoundary>
    );
    expect(screen.getByText('new message')).toBeVisible();
    expect(window.cancelAnimationFrame).toHaveBeenCalledWith(1);
    expect(Stable).toHaveBeenCalledOnce();

    act(() => frames.runEvenIfCancelled(1));

    expect(Stable).toHaveBeenCalledOnce();
    expect(screen.getByText('new message')).toBeVisible();
    spy.mockRestore();
  });
});
