/**
 * Storage can refuse a write (quota: loggingService keeps hundreds of log
 * entries in localStorage) or any access (SecurityError under restricted
 * policies). The zustand persist middleware forwards a synchronous setItem
 * failure out of setState, so a store reset can throw in the middle of the
 * account-change sequence. Nothing security-critical may be skipped because
 * of it: the account signal, the bearer token and the Query cache go first,
 * every remaining step is isolated, sign-out still reaches Supabase, and a
 * verified profile still publishes.
 *
 * Mutation checks (docs/engineering/testing.md), from the repository root:
 *
 *   pnpm --dir frontend exec vitest run src/store/__tests__/auth-store-storage-failures.test.ts
 *
 * - Guard `frontend/src/stores/authStore.ts` clearUserScopedClientState:
 *   make `attempt` rethrow (or call `useChatStore.getState().reset()` bare).
 *   Red: "signOut still revokes the session when the persisted chat
 *   selection cannot be written" (signOut rejects before Supabase signOut).
 * - Guard `frontend/src/stores/authStore.ts` observeIdentity: add
 *   `writeClientOwner(userId)` back after `sessionUserId = userId`. Red:
 *   "does not stamp an identity the profile has not verified".
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { CLIENT_OWNER_STORAGE_KEY } from '@/lib/client-owner';

type AuthChangeHandler = (event: string, session: unknown) => void;

interface Harness {
  useAuthStore: typeof import('@/stores/authStore').useAuthStore;
  useChatStore: typeof import('@/store/chat-store').useChatStore;
  useProjectStore: typeof import('@/store/projectStore').useProjectStore;
  apiGet: ReturnType<typeof vi.fn>;
  clearAuth: ReturnType<typeof vi.fn>;
  queryClear: ReturnType<typeof vi.fn>;
  supabaseSignOut: ReturnType<typeof vi.fn>;
  getUser: ReturnType<typeof vi.fn>;
  getSession: ReturnType<typeof vi.fn>;
  emit: (event: string, session: unknown) => void;
}

const session = (
  id: string
): { user: { id: string }; access_token: string } => ({
  user: { id },
  access_token: `synthetic-${id}`,
});
const profile = (
  id: string
): { user: { id: string }; organization: { id: string } } => ({
  user: { id },
  organization: { id: `org-${id}` },
});

function deferred<T>(): {
  promise: Promise<T>;
  resolve: (value: T) => void;
} {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((yes) => {
    resolve = yes;
  });
  return { promise, resolve };
}

const flushTimers = (): Promise<void> =>
  new Promise((resolve) => setTimeout(resolve, 0));

async function loadFreshTab(id: string): Promise<Harness> {
  vi.resetModules();
  const handlers: AuthChangeHandler[] = [];
  const emit = (event: string, value: unknown): void =>
    handlers.forEach((handler) => handler(event, value));

  const apiGet = vi.fn().mockResolvedValue(profile(id));
  const clearAuth = vi.fn();
  const queryClear = vi.fn();
  const supabaseSignOut = vi.fn().mockResolvedValue({ error: null });
  const getUser = vi
    .fn()
    .mockResolvedValue({ data: { user: { id } }, error: null });
  const getSession = vi
    .fn()
    .mockResolvedValue({ data: { session: session(id) } });

  vi.doMock('@/lib/supabase/client', () => ({
    createClient: () => ({
      auth: {
        onAuthStateChange: (handler: AuthChangeHandler) => {
          handlers.push(handler);
          return { data: { subscription: { unsubscribe: vi.fn() } } };
        },
        signInWithPassword: vi.fn(),
        signOut: supabaseSignOut,
        getUser,
        getSession,
      },
    }),
  }));
  vi.doMock('@/lib/supabase/clearAuthCookies', () => ({
    clearSupabaseAuthCookies: vi.fn(),
  }));
  vi.doMock('@/services/api-client', () => ({
    api: { get: apiGet, post: vi.fn(), clearAuth },
  }));
  vi.doMock('@/lib/query-client', () => ({
    getAppQueryClient: () => ({
      clear: queryClear,
      invalidateQueries: vi.fn(),
    }),
    setAppQueryClient: vi.fn(),
  }));

  const { useAuthStore } = await import('@/stores/authStore');
  const { useChatStore } = await import('@/store/chat-store');
  const { useProjectStore } = await import('@/store/projectStore');
  return {
    useAuthStore,
    useChatStore,
    useProjectStore,
    apiGet,
    clearAuth,
    queryClear,
    supabaseSignOut,
    getUser,
    getSession,
    emit,
  };
}

async function bootFromCookie(tab: Harness, id: string): Promise<void> {
  const verification = deferred<unknown>();
  tab.getUser.mockReturnValueOnce(verification.promise);
  const initialized = tab.useAuthStore.getState().initialize();
  await vi.waitFor(() => expect(tab.getUser).toHaveBeenCalledOnce());
  tab.emit('SIGNED_IN', session(id));
  verification.resolve({ data: { user: { id } }, error: null });
  await initialized;
  await flushTimers();
  expect(tab.useAuthStore.getState().user?.id).toBe(id);
}

function seedPrivateState(tab: Harness): void {
  tab.useChatStore.setState({
    currentWorkspaceId: 'A-only-workspace',
    currentThreadId: 'A-only-thread',
  });
  tab.useProjectStore.setState({
    currentProject: { id: 'A-only-project' } as never,
  });
}

/** Storage that refuses to write the persisted chat selection. */
function denyChatStorageWrites(): void {
  const setItem = Storage.prototype.setItem;
  vi.spyOn(Storage.prototype, 'setItem').mockImplementation(function (
    this: Storage,
    key: string,
    value: string
  ) {
    if (key === 'chat-storage') {
      throw new DOMException('Quota exceeded', 'QuotaExceededError');
    }
    return setItem.call(this, key, value);
  });
}

/** Storage that refuses every removal. */
function denyRemovals(): void {
  vi.spyOn(Storage.prototype, 'removeItem').mockImplementation(() => {
    throw new DOMException('Storage access denied', 'SecurityError');
  });
}

function expectSessionRevoked(tab: Harness): void {
  expect(tab.useAuthStore.getState()).toMatchObject({
    user: null,
    organization: null,
    isAuthenticated: false,
  });
  expect(tab.clearAuth).toHaveBeenCalled();
  expect(tab.queryClear).toHaveBeenCalled();
  expect(tab.supabaseSignOut).toHaveBeenCalledTimes(1);
}

describe('account changes under failing storage', () => {
  beforeEach(() => {
    localStorage.clear();
    vi.clearAllMocks();
  });

  afterEach(() => {
    vi.restoreAllMocks();
    localStorage.clear();
  });

  it('signOut still revokes the session when the persisted chat selection cannot be written', async () => {
    const tab = await loadFreshTab('A');
    await bootFromCookie(tab, 'A');
    seedPrivateState(tab);
    denyChatStorageWrites();

    await expect(tab.useAuthStore.getState().signOut()).resolves.toBeUndefined();

    expectSessionRevoked(tab);
    expect(tab.useChatStore.getState().currentThreadId).toBeNull();
    expect(tab.useProjectStore.getState().currentProject).toBeNull();
    expect(localStorage.getItem(CLIENT_OWNER_STORAGE_KEY)).toBeNull();
  });

  it('signOut still revokes the session when storage refuses removals', async () => {
    const tab = await loadFreshTab('A');
    await bootFromCookie(tab, 'A');
    seedPrivateState(tab);
    denyRemovals();

    await expect(tab.useAuthStore.getState().signOut()).resolves.toBeUndefined();

    expectSessionRevoked(tab);
    expect(tab.useChatStore.getState().currentThreadId).toBeNull();
    expect(tab.useProjectStore.getState().currentProject).toBeNull();
  });

  it('an account change still clears the rest when the chat selection cannot be written', async () => {
    const tab = await loadFreshTab('A');
    await bootFromCookie(tab, 'A');
    seedPrivateState(tab);
    tab.getUser.mockResolvedValue({ data: { user: { id: 'B' } }, error: null });
    tab.getSession.mockResolvedValue({ data: { session: session('B') } });
    tab.apiGet.mockResolvedValue(profile('B'));
    denyChatStorageWrites();

    expect(() => tab.emit('SIGNED_IN', session('B'))).not.toThrow();

    expect(tab.useAuthStore.getState().user).toBeNull();
    expect(tab.clearAuth).toHaveBeenCalled();
    expect(tab.queryClear).toHaveBeenCalled();
    expect(tab.useChatStore.getState().currentThreadId).toBeNull();
    expect(tab.useProjectStore.getState().currentProject).toBeNull();
    expect(localStorage.getItem(CLIENT_OWNER_STORAGE_KEY)).toBeNull();
    // B's verification still completes on this tab.
    await vi.waitFor(() =>
      expect(tab.useAuthStore.getState().user?.id).toBe('B')
    );
  });

  it('publishes the profile even when the persisted chat selection cannot be written', async () => {
    localStorage.setItem(CLIENT_OWNER_STORAGE_KEY, 'A');
    denyChatStorageWrites();
    const tab = await loadFreshTab('A');

    await bootFromCookie(tab, 'A');

    expect(tab.useAuthStore.getState()).toMatchObject({
      user: { id: 'A' },
      isAuthenticated: true,
      isLoading: false,
    });
    expect(tab.useChatStore.getState().ownerUserId).toBe('A');
  });

  it('does not stamp an identity the profile has not verified', async () => {
    const tab = await loadFreshTab('A');
    const verification = deferred<unknown>();
    tab.getUser.mockReturnValueOnce(verification.promise);
    const initialized = tab.useAuthStore.getState().initialize();
    await vi.waitFor(() => expect(tab.getUser).toHaveBeenCalledOnce());

    // The cookie session says A, but nothing has verified it yet.
    tab.emit('SIGNED_IN', session('A'));
    expect(localStorage.getItem(CLIENT_OWNER_STORAGE_KEY)).toBeNull();

    verification.resolve({ data: { user: { id: 'A' } }, error: null });
    await initialized;
    await flushTimers();
    expect(tab.useAuthStore.getState().user?.id).toBe('A');
    expect(localStorage.getItem(CLIENT_OWNER_STORAGE_KEY)).toBe('A');
  });
});
