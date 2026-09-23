import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { useAgentChatStore } from '@/store/agentChatStore';
import type { AgentStreamCallbacks } from '@/services/agentChatService';

/** Internal `_abortController` slot isn't part of the public store types
 * (AgentChatState/AgentChatActions) — read it via a narrow structural cast,
 * mirroring the store's own `as unknown as AgentChatStore` internal casts. */
function getAbortController(): AbortController | null {
  return (
    useAgentChatStore.getState() as unknown as {
      _abortController: AbortController | null;
    }
  )._abortController;
}

const serviceMocks = vi.hoisted(() => ({
  listThreads: vi.fn(async () => ({ threads: [] })),
  getThreadMessages: vi.fn(async () => ({ messages: [] })),
  streamMessage: vi.fn(async () => {}),
  startDurableRun: vi.fn(async () => ({ runId: 'run-stub' })),
}));

// loadThreads/loadThreadMessages dynamically import agentChatService and call
// the backend. In jsdom that request never resolves, so loadThreads otherwise
// hangs to the 15s test timeout. Mock the service to keep these unit tests
// hermetic and fast. (Mirrors the fix in PR #734.)
//
// isTerminalJobStatus is re-exported from the real module: the store
// destructures it from this dynamic import, and a missing export makes
// vitest throw at the destructure — inside confirmAction's outer
// try/catch, which silently no-ops the entire confirm flow.
vi.mock('@/services/agentChatService', async (importOriginal) => {
  const actual =
    await importOriginal<typeof import('@/services/agentChatService')>();
  return {
    isTerminalJobStatus: actual.isTerminalJobStatus,
    agentChatService: {
      listThreads: serviceMocks.listThreads,
      getThreadMessages: serviceMocks.getThreadMessages,
      streamMessage: serviceMocks.streamMessage,
      startDurableRun: serviceMocks.startDurableRun,
      streamConfirm: vi.fn(async () => {}),
      completeDurableConfirmation: vi.fn(async () => {}),
      getDurableRunStatus: vi.fn(async () => ({ status: 'PENDING' })),
      confirmAction: vi.fn(async () => {}),
    },
  };
});

describe('agentChatStore', () => {
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  beforeEach(() => {
    useAgentChatStore.getState().reset();
    serviceMocks.getThreadMessages.mockReset();
    serviceMocks.getThreadMessages.mockResolvedValue({ messages: [] });
  });

  describe('initial state', () => {
    it('starts with closed UI mode', () => {
      const state = useAgentChatStore.getState();
      expect(state.uiMode).toBe('closed');
    });

    it('has no active thread', () => {
      const state = useAgentChatStore.getState();
      expect(state.activeThreadId).toBeNull();
    });

    it('has empty messages', () => {
      const state = useAgentChatStore.getState();
      expect(state.messages).toEqual([]);
    });

    it('has empty threads', () => {
      const state = useAgentChatStore.getState();
      expect(state.threads).toEqual([]);
    });

    it('has unknown page context with Dashboard label', () => {
      const state = useAgentChatStore.getState();
      expect(state.pageContext).toEqual({
        type: 'unknown',
        label: 'Dashboard',
      });
    });

    it('is not streaming', () => {
      const state = useAgentChatStore.getState();
      expect(state.isStreaming).toBe(false);
    });

    it('has empty input value', () => {
      const state = useAgentChatStore.getState();
      expect(state.inputValue).toBe('');
    });

    it('has no unread messages', () => {
      const state = useAgentChatStore.getState();
      expect(state.hasUnread).toBe(false);
    });

    it('is not loading threads or messages', () => {
      const state = useAgentChatStore.getState();
      expect(state.isLoadingThreads).toBe(false);
      expect(state.isLoadingMessages).toBe(false);
    });
  });

  describe('UI mode transitions', () => {
    it('openPanel sets mode to panel', () => {
      useAgentChatStore.getState().openPanel();
      expect(useAgentChatStore.getState().uiMode).toBe('panel');
    });

    it('openSidebar sets mode to sidebar', () => {
      useAgentChatStore.getState().openSidebar();
      expect(useAgentChatStore.getState().uiMode).toBe('sidebar');
    });

    it('close sets mode to closed', () => {
      useAgentChatStore.getState().openPanel();
      useAgentChatStore.getState().close();
      expect(useAgentChatStore.getState().uiMode).toBe('closed');
    });

    it('toggle cycles closed -> panel -> closed', () => {
      useAgentChatStore.getState().toggle();
      expect(useAgentChatStore.getState().uiMode).toBe('panel');
      useAgentChatStore.getState().toggle();
      expect(useAgentChatStore.getState().uiMode).toBe('closed');
    });

    it('toggle from sidebar closes', () => {
      useAgentChatStore.getState().openSidebar();
      useAgentChatStore.getState().toggle();
      expect(useAgentChatStore.getState().uiMode).toBe('closed');
    });

    it('openPanel clears unread', () => {
      useAgentChatStore.setState({ hasUnread: true });
      useAgentChatStore.getState().openPanel();
      expect(useAgentChatStore.getState().hasUnread).toBe(false);
    });

    it('openSidebar clears unread', () => {
      useAgentChatStore.setState({ hasUnread: true });
      useAgentChatStore.getState().openSidebar();
      expect(useAgentChatStore.getState().hasUnread).toBe(false);
    });

    it('toggle to open clears unread', () => {
      useAgentChatStore.setState({ hasUnread: true });
      useAgentChatStore.getState().toggle();
      expect(useAgentChatStore.getState().hasUnread).toBe(false);
    });
  });

  describe('input management', () => {
    it('setInputValue updates input', () => {
      useAgentChatStore.getState().setInputValue('hello');
      expect(useAgentChatStore.getState().inputValue).toBe('hello');
    });

    it('setInputValue can set empty string', () => {
      useAgentChatStore.getState().setInputValue('hello');
      useAgentChatStore.getState().setInputValue('');
      expect(useAgentChatStore.getState().inputValue).toBe('');
    });
  });

  describe('page context', () => {
    it('setPageContext updates context', () => {
      useAgentChatStore.getState().setPageContext({
        type: 'project',
        label: 'Test Project',
        projectId: 'abc-123',
        projectName: 'Test Project',
      });
      const ctx = useAgentChatStore.getState().pageContext;
      expect(ctx.type).toBe('project');
      expect(ctx.label).toBe('Test Project');
      expect(ctx.projectId).toBe('abc-123');
      expect(ctx.projectName).toBe('Test Project');
    });

    it('setPageContext skips update when context is identical', () => {
      const context = {
        type: 'documents' as const,
        label: 'Documents',
      };
      useAgentChatStore.getState().setPageContext(context);
      const stateAfterFirst = useAgentChatStore.getState();
      useAgentChatStore.getState().setPageContext(context);
      const stateAfterSecond = useAgentChatStore.getState();
      // pageContext object reference should be the same (no unnecessary update)
      expect(stateAfterFirst.pageContext).toBe(stateAfterSecond.pageContext);
    });
  });

  describe('thread management', () => {
    it('newThread clears active thread, messages, and input', () => {
      useAgentChatStore.setState({
        activeThreadId: 'thread-1',
        messages: [
          { id: '1', role: 'user', content: 'hi', timestamp: new Date() },
        ],
        inputValue: 'draft message',
      });
      useAgentChatStore.getState().newThread();
      expect(useAgentChatStore.getState().activeThreadId).toBeNull();
      expect(useAgentChatStore.getState().messages).toEqual([]);
      expect(useAgentChatStore.getState().inputValue).toBe('');
    });

    it('selectThread sets activeThreadId', () => {
      useAgentChatStore.getState().selectThread('thread-42');
      expect(useAgentChatStore.getState().activeThreadId).toBe('thread-42');
    });

    it('keeps the latest selected thread when an earlier fetch resolves last', async () => {
      let resolveThreadA!: (value: {
        messages: Array<Record<string, unknown>>;
      }) => void;
      let resolveThreadB!: (value: {
        messages: Array<Record<string, unknown>>;
      }) => void;
      const threadA = new Promise<{ messages: Array<Record<string, unknown>> }>(
        (resolve) => {
          resolveThreadA = resolve;
        }
      );
      const threadB = new Promise<{ messages: Array<Record<string, unknown>> }>(
        (resolve) => {
          resolveThreadB = resolve;
        }
      );
      // Serve the SAME deferred payloads at BOTH layers. In this suite one
      // loadThreadMessages call resolves the mocked agentChatService while the
      // other reaches the real module's api-client fetch (vitest module-graph
      // quirk, stack-verified) — with only the fetch stub, the mock-routed load
      // resolved instantly (empty) and the "earlier fetch resolves LAST" race
      // never happened: the test passed even with the epoch guard deleted.
      // Mutation-verified: with both layers deferred, deleting the guard fails
      // this test.
      serviceMocks.getThreadMessages.mockImplementation(
        async (threadId: string) =>
          threadId.includes('thread-A') ? threadA : threadB
      );
      vi.stubGlobal(
        'fetch',
        vi.fn((input: RequestInfo | URL) => {
          const response = String(input).includes('thread-A')
            ? threadA
            : threadB;
          return response.then((body) =>
            Promise.resolve(
              new Response(JSON.stringify(body), {
                status: 200,
                headers: { 'Content-Type': 'application/json' },
              })
            )
          );
        })
      );

      useAgentChatStore.setState({
        messages: [
          {
            id: 'old',
            role: 'assistant',
            content: 'old transcript',
            timestamp: new Date(),
          },
        ],
      });

      useAgentChatStore.getState().selectThread('thread-A');
      const loadA = useAgentChatStore.getState().loadThreadMessages('thread-A');
      useAgentChatStore.getState().selectThread('thread-B');
      const loadB = useAgentChatStore.getState().loadThreadMessages('thread-B');

      expect(useAgentChatStore.getState().messages).toEqual([]);
      expect(useAgentChatStore.getState().isLoadingMessages).toBe(true);

      resolveThreadB({
        messages: [
          {
            id: 'b1',
            role: 'assistant',
            content: 'thread B',
            created_at: new Date().toISOString(),
          },
        ],
      });
      await loadB;
      resolveThreadA({
        messages: [
          {
            id: 'a1',
            role: 'assistant',
            content: 'thread A',
            created_at: new Date().toISOString(),
          },
        ],
      });
      await loadA;

      const state = useAgentChatStore.getState();
      expect(state.activeThreadId).toBe('thread-B');
      expect(state.messages.map((message) => message.content)).toEqual([
        'thread B',
      ]);
      expect(state.isLoadingMessages).toBe(false);
    });
  });

  describe('clearMessages', () => {
    it('clears messages, activeThreadId, and isStreaming', () => {
      useAgentChatStore.setState({
        messages: [
          {
            id: '1',
            role: 'assistant',
            content: 'hello',
            timestamp: new Date(),
          },
        ],
        activeThreadId: 'thread-1',
        isStreaming: true,
      });
      useAgentChatStore.getState().clearMessages();
      const state = useAgentChatStore.getState();
      expect(state.messages).toEqual([]);
      expect(state.activeThreadId).toBeNull();
      expect(state.isStreaming).toBe(false);
    });
  });

  describe('async stubs', () => {
    it('sendMessage is callable and returns a promise', async () => {
      await expect(
        useAgentChatStore.getState().sendMessage()
      ).resolves.toBeUndefined();
    });

    it('loadThreads is callable and returns a promise', async () => {
      await expect(
        useAgentChatStore.getState().loadThreads()
      ).resolves.toBeUndefined();
    });

    it('loadThreadMessages is callable and returns a promise', async () => {
      await expect(
        useAgentChatStore.getState().loadThreadMessages('thread-1')
      ).resolves.toBeUndefined();
    });
  });

  describe('durable poll path (sendMessage)', () => {
    // src/trigger/agent/execute-agent.ts (repo root) publishes
    // metadata.status = 'awaiting_confirmation' synchronously and only sets
    // waitTokenId after awaiting wait.createToken() — a real, short async
    // race window. These tests cover: surface immediately once a token is
    // present; tolerate a tokenless snapshot by continuing to poll until the
    // token lands; and fail safe (surface tokenless) if the token never
    // shows up, so a parked confirmation is never silently dropped forever
    // (audit finding A, trace 019ff314-c29c).
    const confirmation = {
      tools: [{ name: 'create_project', args: { name: 'New Project' } }],
      message: 'Create this project?',
    };

    it('surfaces pendingConfirmation immediately when the durable run already carries a waitTokenId', async () => {
      const { agentChatService } = await import('@/services/agentChatService');
      vi.mocked(agentChatService.streamMessage).mockRejectedValueOnce(
        new Error('sse down')
      );
      vi.mocked(agentChatService.getDurableRunStatus).mockResolvedValue({
        status: 'RUNNING',
        metadata: {
          status: 'awaiting_confirmation',
          confirmation,
          threadId: 'thread-durable',
          waitTokenId: 'wait-9',
        },
      } as never);

      useAgentChatStore.setState({ inputValue: 'make a project' });

      vi.useFakeTimers();
      try {
        const p = useAgentChatStore.getState().sendMessage();
        await vi.advanceTimersByTimeAsync(3000);
        await p;
      } finally {
        vi.useRealTimers();
      }

      expect(agentChatService.getDurableRunStatus).toHaveBeenCalledTimes(1);
      expect(
        useAgentChatStore.getState().pendingConfirmations['thread-durable']
      ).toEqual(
        expect.objectContaining({
          threadId: 'thread-durable',
          jobId: 'run-stub',
          ...confirmation,
          waitTokenId: 'wait-9',
        })
      );
    });

    it('keeps polling through a tokenless awaiting_confirmation snapshot until the wait token lands', async () => {
      const { agentChatService } = await import('@/services/agentChatService');
      vi.mocked(agentChatService.streamMessage).mockRejectedValueOnce(
        new Error('sse down')
      );
      vi.mocked(agentChatService.getDurableRunStatus)
        .mockResolvedValueOnce({
          status: 'RUNNING',
          metadata: {
            status: 'awaiting_confirmation',
            confirmation,
            threadId: 'thread-durable',
          },
        } as never)
        .mockResolvedValueOnce({
          status: 'RUNNING',
          metadata: {
            status: 'awaiting_confirmation',
            confirmation,
            threadId: 'thread-durable',
            waitTokenId: 'wait-later',
          },
        } as never);

      useAgentChatStore.setState({ inputValue: 'make a project' });

      vi.useFakeTimers();
      try {
        const p = useAgentChatStore.getState().sendMessage();
        await vi.advanceTimersByTimeAsync(3000); // tick 1: tokenless — not surfaced
        expect(useAgentChatStore.getState().pendingConfirmations).toEqual({});
        await vi.advanceTimersByTimeAsync(3000); // tick 2: token present — surfaced
        await p;
      } finally {
        vi.useRealTimers();
      }

      expect(agentChatService.getDurableRunStatus).toHaveBeenCalledTimes(2);
      const state = useAgentChatStore.getState();
      expect(state.pendingConfirmations['thread-durable']).toEqual(
        expect.objectContaining({
          threadId: 'thread-durable',
          jobId: 'run-stub',
          ...confirmation,
          waitTokenId: 'wait-later',
        })
      );
      const assistant = state.messages.find((m) => m.role === 'assistant');
      expect(assistant?.content).toBe('Waiting for your confirmation...');
      expect(state.isStreaming).toBe(false);
    });

    it('surfaces pendingConfirmation without a waitTokenId once the tokenless grace period is exceeded', async () => {
      const { agentChatService } = await import('@/services/agentChatService');
      vi.mocked(agentChatService.streamMessage).mockRejectedValueOnce(
        new Error('sse down')
      );
      // Never carries a token — the fail-safe path.
      vi.mocked(agentChatService.getDurableRunStatus).mockResolvedValue({
        status: 'RUNNING',
        metadata: {
          status: 'awaiting_confirmation',
          confirmation,
          threadId: 'thread-durable',
        },
      } as never);

      useAgentChatStore.setState({ inputValue: 'make a project' });

      vi.useFakeTimers();
      try {
        const p = useAgentChatStore.getState().sendMessage();
        // MAX_TOKENLESS_TICKS (2) tolerated ticks, surfaced on the 3rd.
        await vi.advanceTimersByTimeAsync(9000);
        await p;
      } finally {
        vi.useRealTimers();
      }

      expect(agentChatService.getDurableRunStatus).toHaveBeenCalledTimes(3);
      const state = useAgentChatStore.getState();
      expect(state.pendingConfirmations['thread-durable']).toEqual(
        expect.objectContaining({
          threadId: 'thread-durable',
          jobId: 'run-stub',
          ...confirmation,
        })
      );
      expect(
        state.pendingConfirmations['thread-durable']?.waitTokenId
      ).toBeUndefined();
      const assistant = state.messages.find((m) => m.role === 'assistant');
      expect(assistant?.content).toBe('Waiting for your confirmation...');
      expect(assistant?.isStreaming).toBe(false);
      expect(state.isStreaming).toBe(false);
    });
  });

  describe('reflection revise loop', () => {
    it('replaces first-answer tokens with second when revising=true fires between them', async () => {
      // Arrange: streamMessage calls onToken('A'), then onReflection(revising=true),
      // then onToken('B'), then onDone — the final message content must be 'B'.
      const { agentChatService } = await import('@/services/agentChatService');
      vi.mocked(agentChatService.streamMessage).mockImplementationOnce(
        async (_req, callbacks) => {
          callbacks.onToken?.('A');
          callbacks.onReflection?.(false, [], 1, true);
          callbacks.onToken?.('B');
          callbacks.onDone?.();
        }
      );

      useAgentChatStore.setState({ inputValue: 'test prompt' });
      await useAgentChatStore.getState().sendMessage();

      const messages = useAgentChatStore.getState().messages;
      const assistant = messages.find((m) => m.role === 'assistant');
      expect(assistant?.content).toBe('B');
    });

    it('does NOT reset content when revising=false', async () => {
      // Arrange: onReflection with revising=false (quality passed) should leave
      // accumulated content untouched.
      const { agentChatService } = await import('@/services/agentChatService');
      vi.mocked(agentChatService.streamMessage).mockImplementationOnce(
        async (_req, callbacks) => {
          callbacks.onToken?.('A');
          callbacks.onReflection?.(true, [], 0, false);
          callbacks.onDone?.();
        }
      );

      useAgentChatStore.setState({ inputValue: 'test prompt' });
      await useAgentChatStore.getState().sendMessage();

      const messages = useAgentChatStore.getState().messages;
      const assistant = messages.find((m) => m.role === 'assistant');
      expect(assistant?.content).toBe('A');
    });
  });

  describe('confirmAction resume stream', () => {
    // The confirm (post-HITL) stream emits the same plan / rag_context
    // events as the main stream; the store must not drop them.
    it('attaches plan and citations from the resumed stream to the assistant message', async () => {
      const { agentChatService } = await import('@/services/agentChatService');
      vi.mocked(agentChatService.streamConfirm).mockImplementationOnce(
        async (_req, callbacks) => {
          callbacks.onPlan?.(
            [{ step: 1, description: 'ingest paper', tool: 'ingest_arxiv' }],
            ''
          );
          callbacks.onRagContext?.([
            {
              document_id: 'doc-1',
              title: 'Attention Is All You Need',
              content: 'snippet',
              score: 0.9,
            },
          ]);
          callbacks.onToken?.('done!');
          callbacks.onDone?.();
        }
      );

      useAgentChatStore.setState({
        activeThreadId: 'thread-A',
        messages: [
          {
            id: 'a-1',
            role: 'assistant',
            content: 'Waiting for your confirmation...',
            timestamp: new Date(),
          },
        ],
        pendingConfirmations: {
          'thread-A': {
            threadId: 'thread-A',
            assistantMessageId: 'a-1',
            jobId: 'job-1',
            tools: [{ name: 'ingest_arxiv', args: {} }],
            message: 'Confirm?',
          },
        },
      });
      await useAgentChatStore.getState().confirmAction('thread-A', true);

      const assistant = useAgentChatStore
        .getState()
        .messages.find((m) => m.role === 'assistant');
      expect(assistant?.plan).toEqual([
        {
          step: 1,
          description: 'ingest paper',
          tool: 'ingest_arxiv',
          args_hint: {},
          depends_on: [],
        },
      ]);
      expect(assistant?.citations).toEqual([
        {
          documentId: 'doc-1',
          documentTitle: 'Attention Is All You Need',
          snippet: 'snippet',
          score: 0.9,
        },
      ]);
      expect(assistant?.content).toBe('done!');
    });

    // When SSE confirm fails, a durable (Trigger.dev) run must complete via its
    // wait token — not the legacy /confirm endpoint. The token was captured
    // before pendingConfirmation was nulled; re-reading it from the store here
    // used to yield undefined, silently dropping the approval.
    it('completes the durable wait token on SSE failure instead of the legacy confirm', async () => {
      const { agentChatService } = await import('@/services/agentChatService');
      vi.mocked(agentChatService.streamConfirm).mockRejectedValueOnce(
        new Error('sse down')
      );
      vi.mocked(agentChatService.getDurableRunStatus).mockResolvedValue({
        status: 'COMPLETED',
        output: { result: { message: 'ingested' } },
      } as never);

      useAgentChatStore.setState({
        activeThreadId: 'thread-A',
        messages: [
          {
            id: 'a-1',
            role: 'assistant',
            content: 'Waiting for your confirmation...',
            timestamp: new Date(),
          },
        ],
        pendingConfirmations: {
          'thread-A': {
            threadId: 'thread-A',
            assistantMessageId: 'a-1',
            jobId: 'run-42',
            waitTokenId: 'wait-7',
            tools: [{ name: 'ingest_arxiv', args: {} }],
            message: 'Confirm?',
          },
        },
      });

      vi.useFakeTimers();
      try {
        const p = useAgentChatStore.getState().confirmAction('thread-A', true);
        // The poll loop waits 3s before its first status check.
        await vi.advanceTimersByTimeAsync(3000);
        await p;
      } finally {
        vi.useRealTimers();
      }

      expect(agentChatService.completeDurableConfirmation).toHaveBeenCalledWith(
        'run-42',
        'wait-7',
        true
      );
      expect(agentChatService.confirmAction).not.toHaveBeenCalled();
    });

    it('keeps the confirmation available when the confirm stream reports an error', async () => {
      const { agentChatService } = await import('@/services/agentChatService');
      vi.mocked(agentChatService.streamConfirm).mockImplementationOnce(
        async (_req, callbacks) => {
          callbacks.onError?.('Confirmation is temporarily unavailable');
        }
      );
      const pending = {
        threadId: 'thread-A',
        assistantMessageId: 'a-1',
        jobId: 'job-1',
        tools: [{ name: 'create_project', args: {} }],
        message: 'Confirm?',
      };
      useAgentChatStore.setState({
        activeThreadId: 'thread-A',
        messages: [
          {
            id: 'a-1',
            role: 'assistant',
            content: 'Waiting for your confirmation...',
            timestamp: new Date(),
          },
        ],
        pendingConfirmations: { 'thread-A': pending },
      });

      await useAgentChatStore.getState().confirmAction('thread-A', true);

      expect(
        useAgentChatStore.getState().pendingConfirmations['thread-A']
      ).toEqual(pending);
      expect(useAgentChatStore.getState().isConfirming).toBe(false);
    });
  });

  describe('request identity and cancellation (RS-C1)', () => {
    it("a superseded generation's terminal events do not clobber the newer generation", async () => {
      const { agentChatService } = await import('@/services/agentChatService');
      const pending: Array<{
        callbacks: AgentStreamCallbacks;
        resolve: () => void;
      }> = [];
      const deferredImpl = (
        _req: unknown,
        callbacks: AgentStreamCallbacks
      ): Promise<void> =>
        new Promise<void>((resolve) => {
          pending.push({ callbacks, resolve });
        });
      vi.mocked(agentChatService.streamMessage)
        .mockImplementationOnce(deferredImpl)
        .mockImplementationOnce(deferredImpl);

      useAgentChatStore.setState({ inputValue: 'gen1 message' });
      const gen1 = useAgentChatStore.getState().sendMessage();
      await vi.waitFor(() => expect(pending.length).toBe(1));

      // Supersede gen1 the way the store's public API allows: stop it, then
      // start a new generation.
      useAgentChatStore.getState().stopGeneration();
      expect(useAgentChatStore.getState().isStreaming).toBe(false);

      useAgentChatStore.setState({ inputValue: 'gen2 message' });
      const gen2 = useAgentChatStore.getState().sendMessage();
      await vi.waitFor(() => expect(pending.length).toBe(2));

      const gen2Controller = getAbortController();
      expect(gen2Controller).not.toBeNull();
      expect(useAgentChatStore.getState().isStreaming).toBe(true);

      // Gen1's stream is superseded but its mock promise is still pending —
      // its terminal event arrives late, after gen2 has taken over.
      pending[0].callbacks.onDone?.();

      expect(useAgentChatStore.getState().isStreaming).toBe(true);
      expect(getAbortController()).toBe(gen2Controller);

      pending.forEach((p) => p.resolve());
      await Promise.all([gen1, gen2]);
    });

    it('stopGeneration aborts the underlying stream', async () => {
      const { agentChatService } = await import('@/services/agentChatService');
      let capturedSignal: AbortSignal | undefined;
      let release: (() => void) | undefined;
      vi.mocked(agentChatService.streamMessage).mockImplementationOnce(
        (_req, _callbacks, signal) =>
          new Promise<void>((resolve) => {
            capturedSignal = signal;
            release = resolve;
          })
      );

      useAgentChatStore.setState({ inputValue: 'hello' });
      const send = useAgentChatStore.getState().sendMessage();
      await vi.waitFor(() => expect(capturedSignal).toBeDefined());

      expect(capturedSignal?.aborted).toBe(false);
      useAgentChatStore.getState().stopGeneration();
      expect(capturedSignal?.aborted).toBe(true);

      release?.();
      await send;
    });

    it('keeps a parked confirmation owned by its thread across navigation', async () => {
      const { agentChatService } = await import('@/services/agentChatService');
      useAgentChatStore.setState({
        activeThreadId: 'thread-A',
        messages: [],
        pendingConfirmations: {
          'thread-A': {
            threadId: 'thread-A',
            assistantMessageId: 'a-assistant',
            jobId: 'job-a',
            tools: [{ name: 'create_project_note', args: {} }],
            message: 'Confirm?',
          },
        },
      });

      useAgentChatStore.getState().selectThread('thread-B');
      await useAgentChatStore.getState().confirmAction('thread-A', true);

      expect(agentChatService.streamConfirm).not.toHaveBeenCalled();
      expect(
        useAgentChatStore.getState().pendingConfirmations['thread-A']
      ).toBeDefined();

      vi.mocked(agentChatService.streamConfirm).mockImplementationOnce(
        async (_request, callbacks) => {
          callbacks.onToken?.('thread A result');
          callbacks.onDone?.();
        }
      );
      useAgentChatStore.getState().selectThread('thread-A');
      await useAgentChatStore.getState().confirmAction('thread-A', true);

      expect(agentChatService.streamConfirm).toHaveBeenCalledWith(
        { thread_id: 'job-a', confirmed: true },
        expect.any(Object),
        expect.any(AbortSignal)
      );
      expect(useAgentChatStore.getState().messages).toEqual([
        expect.objectContaining({
          id: 'a-assistant',
          content: 'thread A result',
        }),
      ]);
      expect(useAgentChatStore.getState().pendingConfirmations).toEqual({});
    });

    it("confirmAction events target the captured message, not the visible thread's last assistant message", async () => {
      const { agentChatService } = await import('@/services/agentChatService');

      useAgentChatStore.setState({
        activeThreadId: 'thread-A',
        messages: [
          {
            id: 'a-assistant',
            role: 'assistant',
            content: 'Waiting for your confirmation...',
            timestamp: new Date(),
          },
        ],
        pendingConfirmations: {
          'thread-A': {
            threadId: 'thread-A',
            assistantMessageId: 'a-assistant',
            jobId: 'job-a',
            tools: [{ name: 'ingest_arxiv', args: {} }],
            message: 'Confirm?',
          },
        },
      });

      let confirmCallbacks: AgentStreamCallbacks | undefined;
      let releaseConfirm: (() => void) | undefined;
      vi.mocked(agentChatService.streamConfirm).mockImplementationOnce(
        (_req, callbacks) =>
          new Promise<void>((resolve) => {
            confirmCallbacks = callbacks;
            releaseConfirm = resolve;
          })
      );

      const confirmPromise = useAgentChatStore
        .getState()
        .confirmAction('thread-A', true);
      await vi.waitFor(() => expect(confirmCallbacks).toBeDefined());

      // Switch to a different thread while thread A's confirm stream is
      // still pending.
      serviceMocks.getThreadMessages.mockResolvedValueOnce({
        messages: [
          {
            id: 'b1',
            role: 'assistant',
            content: 'thread B message',
            created_at: new Date().toISOString(),
          },
        ],
      });
      useAgentChatStore.getState().selectThread('thread-B');
      await useAgentChatStore.getState().loadThreadMessages('thread-B');

      const beforeLateEvents = useAgentChatStore.getState().messages;
      expect(beforeLateEvents.map((m) => m.content)).toEqual([
        'thread B message',
      ]);

      // Thread A's confirm stream fires its late events after the switch —
      // they must not touch thread B's now-visible last assistant message.
      confirmCallbacks?.onToken?.('late content for thread A');
      confirmCallbacks?.onDone?.();

      const afterLateEvents = useAgentChatStore.getState().messages;
      expect(afterLateEvents).toEqual(beforeLateEvents);

      releaseConfirm?.();
      await confirmPromise;
    });

    it("an aborted confirm cannot clear a newer thread's stream owner", async () => {
      const { agentChatService } = await import('@/services/agentChatService');
      let releaseConfirm: (() => void) | undefined;
      vi.mocked(agentChatService.streamConfirm).mockImplementationOnce(
        () =>
          new Promise<void>((resolve) => {
            releaseConfirm = resolve;
          })
      );
      useAgentChatStore.setState({
        activeThreadId: 'thread-A',
        messages: [
          {
            id: 'a-assistant',
            role: 'assistant',
            content: 'Waiting for your confirmation...',
            timestamp: new Date(),
          },
        ],
        pendingConfirmations: {
          'thread-A': {
            threadId: 'thread-A',
            assistantMessageId: 'a-assistant',
            jobId: 'job-a',
            tools: [],
            message: 'Confirm?',
          },
        },
      });

      const confirm = useAgentChatStore
        .getState()
        .confirmAction('thread-A', true);
      await vi.waitFor(() => expect(releaseConfirm).toBeDefined());
      useAgentChatStore.getState().selectThread('thread-B');

      let releaseSend: (() => void) | undefined;
      vi.mocked(agentChatService.streamMessage).mockImplementationOnce(
        (_request, callbacks) =>
          new Promise<void>((resolve) => {
            releaseSend = () => {
              callbacks.onDone?.();
              resolve();
            };
          })
      );
      useAgentChatStore.setState({ inputValue: 'thread B turn' });
      const send = useAgentChatStore.getState().sendMessage();
      await vi.waitFor(() => expect(releaseSend).toBeDefined());
      const threadBController = getAbortController();

      releaseConfirm?.();
      await confirm;

      expect(useAgentChatStore.getState().isStreaming).toBe(true);
      expect(getAbortController()).toBe(threadBController);
      expect(
        useAgentChatStore.getState().pendingConfirmations['thread-A']
      ).toBeDefined();

      releaseSend?.();
      await send;
    });

    it('drops a trailing token frame arriving after onDone', async () => {
      const { agentChatService } = await import('@/services/agentChatService');
      vi.mocked(agentChatService.streamMessage).mockImplementationOnce(
        async (_req, callbacks) => {
          callbacks.onToken?.('hello');
          callbacks.onDone?.();
          // A normally-completed generation never aborts its own signal —
          // onDone only releases ownership. A trailing token frame must be
          // dropped by the ownership check, not the (never-set) abort flag.
          callbacks.onToken?.(' trailing');
        }
      );

      useAgentChatStore.setState({ inputValue: 'hi' });
      await useAgentChatStore.getState().sendMessage();

      const assistant = useAgentChatStore
        .getState()
        .messages.find((m) => m.role === 'assistant');
      expect(assistant?.content).toBe('hello');
    });

    it('durable poll completion after a thread switch does not write into the new thread', async () => {
      const { agentChatService } = await import('@/services/agentChatService');
      vi.mocked(agentChatService.streamConfirm).mockRejectedValueOnce(
        new Error('sse down')
      );
      let resolvePoll!: (value: {
        status: string;
        output: Record<string, unknown>;
      }) => void;
      vi.mocked(agentChatService.getDurableRunStatus).mockImplementation(
        () =>
          new Promise((resolve) => {
            resolvePoll = resolve;
          }) as never
      );

      useAgentChatStore.setState({
        activeThreadId: 'thread-A',
        messages: [
          {
            id: 'a-assistant',
            role: 'assistant',
            content: 'Waiting for your confirmation...',
            timestamp: new Date(),
          },
        ],
        pendingConfirmations: {
          'thread-A': {
            threadId: 'thread-A',
            assistantMessageId: 'a-assistant',
            jobId: 'run-42',
            waitTokenId: 'wait-7',
            tools: [{ name: 'ingest_arxiv', args: {} }],
            message: 'Confirm?',
          },
        },
      });

      vi.useFakeTimers();
      try {
        const confirmPromise = useAgentChatStore
          .getState()
          .confirmAction('thread-A', true);
        // Past the 3s poll sleep — confirmAction is now suspended inside the
        // getDurableRunStatus network await.
        await vi.advanceTimersByTimeAsync(3000);
        expect(agentChatService.getDurableRunStatus).toHaveBeenCalledWith(
          'run-42'
        );

        // Switch threads while that await is in flight.
        serviceMocks.getThreadMessages.mockResolvedValueOnce({
          messages: [
            {
              id: 'b1',
              role: 'assistant',
              content: 'thread B message',
              created_at: new Date().toISOString(),
            },
          ],
        });
        useAgentChatStore.getState().selectThread('thread-B');
        await useAgentChatStore.getState().loadThreadMessages('thread-B');

        const before = useAgentChatStore.getState().messages;
        expect(before.map((m) => m.content)).toEqual(['thread B message']);

        // Thread A's poll result lands late — it must not touch thread B's
        // last assistant message.
        resolvePoll({
          status: 'COMPLETED',
          output: { result: { message: 'ingested' } },
        });
        await confirmPromise;

        expect(useAgentChatStore.getState().messages).toEqual(before);
      } finally {
        vi.useRealTimers();
      }
    });
  });

  describe('persisted execution hydration', () => {
    it('restores the latest assistant plan and keeps pending results incomplete', async () => {
      serviceMocks.getThreadMessages.mockResolvedValueOnce({
        messages: [
          {
            id: 'assistant-1',
            role: 'assistant',
            content: 'Draft generation is still running.',
            created_at: new Date().toISOString(),
            plan: [
              {
                step: 1,
                description: 'Create draft',
                tool: 'create_draft',
                args_hint: {},
                depends_on: [],
              },
            ],
            tool_executions: [
              {
                id: 'exec-1',
                tool_name: 'create_draft',
                tool_display_name: 'Create draft',
                args: {},
                status: 'completed',
                result: { status: 'pending', task_id: 'draft-task' },
              },
            ],
          },
        ],
      });
      useAgentChatStore.getState().selectThread('thread-1');

      await useAgentChatStore.getState().loadThreadMessages('thread-1');

      const state = useAgentChatStore.getState();
      expect(state.currentPlan).toHaveLength(1);
      expect(state.messages[0].plan).toHaveLength(1);
      expect(state.messages[0].toolExecutions?.[0].status).toBe('pending');
    });

    it('restores a persisted execution error as failed', async () => {
      serviceMocks.getThreadMessages.mockResolvedValueOnce({
        messages: [
          {
            id: 'assistant-error',
            role: 'assistant',
            content: 'Draft generation failed.',
            created_at: new Date().toISOString(),
            tool_executions: [
              {
                id: 'exec-error',
                tool_name: 'create_draft',
                tool_display_name: 'Create draft',
                args: {},
                status: 'completed',
                result: undefined,
                error: 'citation review failed',
              },
            ],
          },
        ],
      });
      useAgentChatStore.getState().selectThread('thread-1');

      await useAgentChatStore.getState().loadThreadMessages('thread-1');

      expect(
        useAgentChatStore.getState().messages[0].toolExecutions?.[0].status
      ).toBe('failed');
    });
  });

  describe('reset', () => {
    it('restores all state to initial values', () => {
      // Mutate state
      useAgentChatStore.setState({
        uiMode: 'panel',
        activeThreadId: 'thread-99',
        messages: [
          { id: '1', role: 'user', content: 'msg', timestamp: new Date() },
        ],
        isStreaming: true,
        inputValue: 'some text',
        hasUnread: true,
        pageContext: { type: 'project', label: 'Proj' },
        isLoadingThreads: true,
        isLoadingMessages: true,
      });

      useAgentChatStore.getState().reset();

      const state = useAgentChatStore.getState();
      expect(state.uiMode).toBe('closed');
      expect(state.activeThreadId).toBeNull();
      expect(state.messages).toEqual([]);
      expect(state.threads).toEqual([]);
      expect(state.isStreaming).toBe(false);
      expect(state.inputValue).toBe('');
      expect(state.hasUnread).toBe(false);
      expect(state.pageContext).toEqual({
        type: 'unknown',
        label: 'Dashboard',
      });
      expect(state.isLoadingThreads).toBe(false);
      expect(state.isLoadingMessages).toBe(false);
    });
  });
});
