import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { getAccountSignal, resetAccountSession } from '@/lib/account-session';
const auth = vi.hoisted(() => ({
  getSession: vi.fn(),
  refreshSession: vi.fn(),
}));
vi.mock('@/lib/supabase/client', () => ({ createClient: () => ({ auth }) }));
vi.mock('@/services/api-client', () => ({
  api: { get: vi.fn(), post: vi.fn() },
}));
import { agentChatService } from '@/services/agentChatService';
import { api } from '@/services/api-client';
function deferred<T>(): { promise: Promise<T>; resolve: (value: T) => void } {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((yes) => {
    resolve = yes;
  });
  return { promise, resolve };
}
const fetchMock = vi.fn();
beforeEach(() => {
  resetAccountSession();
  vi.stubGlobal('fetch', fetchMock);
  auth.getSession.mockResolvedValue({ data: { session: null } });
});
afterEach(() => {
  vi.unstubAllGlobals();
  vi.useRealTimers();
});

it.each(['start', 'status', 'confirm', 'cancel'] as const)(
  'cancels raw agent %s and discards its deferred result',
  async (operation) => {
    const response = deferred<Response>();
    fetchMock.mockReturnValueOnce(response.promise);
    const actions = {
      start: () =>
        agentChatService.startDurableRun({
          messages: [{ role: 'user', content: 'A private' }],
          page_context: { type: 'chat' },
        }),
      status: () => agentChatService.getDurableRunStatus('A-run'),
      confirm: () =>
        agentChatService.completeDurableConfirmation('A-run', 'A-token', true),
      cancel: () => agentChatService.cancelPendingConfirmation('A-thread'),
    };
    const pending = actions[operation]();
    const outcome = pending.catch((error) => error);
    await vi.waitFor(() => expect(fetchMock).toHaveBeenCalled());
    const signal = fetchMock.mock.calls[0][1]?.signal;
    resetAccountSession();
    response.resolve(
      new Response(JSON.stringify({ runId: 'A-run' }), { status: 200 })
    );
    expect(await outcome).toMatchObject({ name: 'AbortError' });
    expect(signal?.aborted).toBe(true);
  }
);

it('does not acquire B credentials for an old cancellation after deferred auth', async () => {
  const authResult = deferred<never>();
  auth.getSession.mockReturnValueOnce(authResult.promise);
  const pending = agentChatService
    .cancelActiveRun('A-thread')
    .catch((error) => error);
  await vi.waitFor(() => expect(auth.getSession).toHaveBeenCalled());
  resetAccountSession();
  authResult.resolve({
    data: { session: { access_token: 'synthetic-B' } },
  } as never);
  const outcome = await pending;
  expect(fetchMock).not.toHaveBeenCalled();
  expect(outcome).toMatchObject({ name: 'AbortError' });
});

it('does not continue cancellation polling after the account ends', async () => {
  vi.useFakeTimers();
  fetchMock.mockResolvedValueOnce(new Response('{}'));
  const projection = deferred<never>();
  vi.mocked(api.get).mockReturnValueOnce(projection.promise);
  const pending = agentChatService
    .cancelActiveRun('A-thread', 'A-run')
    .catch((error) => error);
  await vi.waitFor(() => expect(api.get).toHaveBeenCalledOnce());
  resetAccountSession();
  projection.resolve({ status: 'running' } as never);
  await vi.runAllTimersAsync();
  const outcome = await pending;
  expect(api.get).toHaveBeenCalledOnce();
  expect(outcome).toMatchObject({ name: 'AbortError' });
});

it('does not refresh B session for A stream late unauthorized response', async () => {
  const response = deferred<Response>();
  fetchMock.mockReturnValueOnce(response.promise);
  const refreshed = vi.fn();
  const pending = agentChatService
    .streamMessage(
      {
        messages: [{ role: 'user', content: 'A' }],
        page_context: { type: 'chat' },
      },
      { onAuthRefreshAttempt: refreshed },
      getAccountSignal()
    )
    .catch(() => undefined);
  await vi.waitFor(() => expect(fetchMock).toHaveBeenCalledOnce());
  resetAccountSession();
  response.resolve(new Response('{}', { status: 401 }));
  await pending;
  expect(auth.refreshSession).not.toHaveBeenCalled();
  expect(refreshed).not.toHaveBeenCalled();
});
