/**
 * Q-P1: a same-user page load must keep the persisted client caches; any
 * other load, and any load whose owner cannot be established, must clear
 * them (the GOO-350 guarantee: account B never sees account A's cached client
 * data).
 *
 * Every test boots a fresh module graph with pre-seeded localStorage, the way
 * a browser tab does: in-memory identity is empty, only storage survives.
 * The real `workspaceService` and `chat-store` are used so the assertions
 * observe actual localStorage removal, not a mocked call.
 *
 * Two layers protect the caches. The origin-level owner stamp
 * (`nous:client-owner:v1`) decides whether a load clears everything. Each
 * payload (`chat-storage`, `default-workspace-object`) also carries the
 * account it was written for, so a payload a lagging tab wrote for A after
 * this origin moved to B is discarded even though the stamp says B.
 *
 * Mutation checks (procedure in docs/engineering/testing.md), run from the
 * repository root:
 *
 *   pnpm --dir frontend exec vitest run src/store/__tests__/auth-client-owner.test.ts
 *
 * - Guard `frontend/src/stores/authStore.ts` observeIdentity: drop the
 *   `?? readClientOwner()` fallback from `previousId`. Red: "keeps the caches
 *   when the same user reloads" (chat-storage no longer contains
 *   A-only-thread; a second /api/v2/workspaces bootstrap would follow).
 * - Guard `frontend/src/stores/authStore.ts` clearUserScopedClientState:
 *   drop `clearClientOwner()`. Red: "forgets the owner on sign-out so the
 *   next sign-in clears again" (the stamp survives sign-out, so the same
 *   user's next sign-in trusts caches nobody vouches for).
 * - Guard `frontend/src/services/workspaceService.ts` readCachedWorkspace:
 *   replace the `record.ownerUserId === ownerUserId` comparison with `true`.
 *   Red: "a lagging tab cannot hand A's payloads to B" (B requests
 *   /api/v2/workspaces/A-only-workspace).
 * - Guard `frontend/src/store/chat/slices/selectionSlice.ts`
 *   adoptPersistedSelection: replace the owner comparison with `true`. Red:
 *   same test (B adopts A-only-thread).
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { CLIENT_OWNER_STORAGE_KEY } from '@/lib/client-owner';

type AuthChangeHandler = (event: string, session: unknown) => void;

interface Harness {
  useAuthStore: typeof import('@/stores/authStore').useAuthStore;
  useChatStore: typeof import('@/store/chat-store').useChatStore;
  apiGet: ReturnType<typeof vi.fn>;
  getUser: ReturnType<typeof vi.fn>;
  getSession: ReturnType<typeof vi.fn>;
  emit: (event: string, session: unknown) => void;
}

interface WorkspaceStub {
  id: string;
  name: string;
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

const A_WORKSPACE: WorkspaceStub = {
  id: 'A-only-workspace',
  name: 'A-only workspace',
};
const B_WORKSPACE: WorkspaceStub = { id: 'B-workspace', name: 'B workspace' };

/**
 * What a previous visit by account A leaves behind in localStorage. With an
 * owner, the payloads are stamped the way the current code writes them;
 * without one they are the legacy shapes today's deployed code writes.
 */
function seedPersistedCaches(options: { owner?: string } = {}): void {
  localStorage.setItem(
    'chat-storage',
    JSON.stringify({
      state: {
        currentWorkspaceId: 'A-only-workspace',
        currentConversationId: 'A-only-conversation',
        currentThreadId: 'A-only-thread',
        sidebarCollapsed: false,
        ...(options.owner ? { ownerUserId: options.owner } : {}),
      },
      version: 0,
    })
  );
  localStorage.setItem(
    'default-workspace-object',
    options.owner
      ? JSON.stringify({
          version: 1,
          ownerUserId: options.owner,
          cachedAt: Date.now(),
          workspace: A_WORKSPACE,
        })
      : JSON.stringify(A_WORKSPACE)
  );
  // Loose keys today's code writes next to the record.
  localStorage.setItem('default-workspace-cached-at', String(Date.now()));
  localStorage.setItem('default-workspace-id', 'A-only-workspace');
  localStorage.setItem('default-conversation-id', 'A-only-conversation');
}

const PERSISTED_CACHE_KEYS = [
  'chat-storage',
  'default-workspace-object',
  'default-workspace-cached-at',
  'default-workspace-id',
  'default-conversation-id',
];

function expectCachesCleared(): void {
  for (const key of PERSISTED_CACHE_KEYS) {
    expect(localStorage.getItem(key) ?? '', key).not.toContain('A-only');
  }
}

function expectCachesKept(): void {
  expect(localStorage.getItem('chat-storage') ?? '').toContain(
    'A-only-thread'
  );
  expect(localStorage.getItem('default-workspace-object') ?? '').toContain(
    'A-only-workspace'
  );
}

function requestedUrls(tab: Harness): string[] {
  return tab.apiGet.mock.calls.map(([url]) => String(url));
}

/**
 * A fresh tab: new module graph, empty in-memory identity, Supabase answering
 * for `id` from its cookie session.
 */
async function loadFreshTab(id: string): Promise<Harness> {
  vi.resetModules();
  const handlers: AuthChangeHandler[] = [];
  const emit = (event: string, value: unknown): void =>
    handlers.forEach((handler) => handler(event, value));

  const apiGet = vi.fn().mockResolvedValue(profile(id));
  const getUser = vi
    .fn()
    .mockResolvedValue({ data: { user: { id } }, error: null });
  const getSession = vi
    .fn()
    .mockResolvedValue({ data: { session: session(id) } });
  const signInWithPassword = vi.fn().mockImplementation(async () => {
    // Mirrors supabase-js: SIGNED_IN is broadcast as part of the sign-in call.
    emit('SIGNED_IN', session(id));
    return { data: { session: session(id) }, error: null };
  });

  vi.doMock('@/lib/supabase/client', () => ({
    createClient: () => ({
      auth: {
        onAuthStateChange: (handler: AuthChangeHandler) => {
          handlers.push(handler);
          return { data: { subscription: { unsubscribe: vi.fn() } } };
        },
        signInWithPassword,
        signOut: vi.fn().mockResolvedValue({ error: null }),
        getUser,
        getSession,
      },
    }),
  }));
  vi.doMock('@/lib/supabase/clearAuthCookies', () => ({
    clearSupabaseAuthCookies: vi.fn(),
  }));
  vi.doMock('@/services/api-client', () => ({
    api: { get: apiGet, post: vi.fn(), clearAuth: vi.fn() },
  }));

  const { useAuthStore } = await import('@/stores/authStore');
  const { useChatStore } = await import('@/store/chat-store');
  return { useAuthStore, useChatStore, apiGet, getUser, getSession, emit };
}

/** Serve the backend this tab's user would see. */
function serveBackend(
  tab: Harness,
  id: string,
  options: { list: WorkspaceStub[]; byId?: Record<string, WorkspaceStub> }
): void {
  tab.apiGet.mockImplementation(async (url: string) => {
    if (url === '/auth/me') return profile(id);
    if (url === '/api/v2/workspaces') return options.list;
    const single = /^\/api\/v2\/workspaces\/([^/?]+)$/.exec(url);
    if (single) {
      const workspace = options.byId?.[single[1]];
      if (workspace) return workspace;
      throw { error: { status_code: 404 } };
    }
    if (/^\/api\/v2\/workspaces\/[^/]+\/conversations/.test(url)) {
      return { conversations: [], total: 0 };
    }
    throw new Error(`Unexpected GET ${url}`);
  });
}

/**
 * The deployed boot sequence: AuthProvider calls initialize(), auth-js emits
 * SIGNED_IN from the cookie session while getUser() is still verifying it,
 * then the verified profile lands.
 */
async function bootFromCookie(harness: Harness, id: string): Promise<void> {
  const verification = deferred<unknown>();
  harness.getUser.mockReturnValueOnce(verification.promise);
  const initialized = harness.useAuthStore.getState().initialize();
  await vi.waitFor(() => expect(harness.getUser).toHaveBeenCalledOnce());
  harness.emit('SIGNED_IN', session(id));
  verification.resolve({ data: { user: { id } }, error: null });
  await initialized;
  // Lets the listener-scheduled profile fetch run (and dedupe).
  await flushTimers();
  expect(harness.useAuthStore.getState().user?.id).toBe(id);
}

describe('client caches across page loads', () => {
  beforeEach(() => {
    localStorage.clear();
    vi.clearAllMocks();
  });

  afterEach(() => {
    localStorage.clear();
  });

  it('keeps the caches when the same user reloads', async () => {
    localStorage.setItem(CLIENT_OWNER_STORAGE_KEY, 'A');
    seedPersistedCaches({ owner: 'A' });
    const tab = await loadFreshTab('A');

    await bootFromCookie(tab, 'A');

    expectCachesKept();
    expect(tab.useChatStore.getState()).toMatchObject({
      currentWorkspaceId: 'A-only-workspace',
      currentConversationId: 'A-only-conversation',
      currentThreadId: 'A-only-thread',
    });
    expect(localStorage.getItem(CLIENT_OWNER_STORAGE_KEY)).toBe('A');
    expect(tab.apiGet).toHaveBeenCalledTimes(1);
  });

  it('clears the caches when a different user loads and re-stamps them', async () => {
    localStorage.setItem(CLIENT_OWNER_STORAGE_KEY, 'A');
    seedPersistedCaches({ owner: 'A' });
    const tab = await loadFreshTab('B');

    await bootFromCookie(tab, 'B');

    expectCachesCleared();
    expect(tab.useChatStore.getState()).toMatchObject({
      currentWorkspaceId: null,
      currentConversationId: null,
      currentThreadId: null,
    });
    expect(localStorage.getItem(CLIENT_OWNER_STORAGE_KEY)).toBe('B');
    expect(tab.useAuthStore.getState().user?.id).toBe('B');
  });

  it('forgets the owner on sign-out so the next sign-in clears again', async () => {
    const tab = await loadFreshTab('A');
    await bootFromCookie(tab, 'A');
    expect(localStorage.getItem(CLIENT_OWNER_STORAGE_KEY)).toBe('A');

    await tab.useAuthStore.getState().signOut();
    expect(localStorage.getItem(CLIENT_OWNER_STORAGE_KEY)).toBeNull();

    // Residue nobody vouches for (a late persist write, another tab).
    seedPersistedCaches({ owner: 'A' });
    await tab.useAuthStore
      .getState()
      .signIn('a@example.invalid', 'synthetic-password');
    expectCachesCleared();
    expect(localStorage.getItem(CLIENT_OWNER_STORAGE_KEY)).toBe('A');

    // And a different account after sign-out is cleared as before.
    await tab.useAuthStore.getState().signOut();
    seedPersistedCaches({ owner: 'A' });
    const other = await loadFreshTab('B');
    await other.useAuthStore
      .getState()
      .signIn('b@example.invalid', 'synthetic-password');
    expectCachesCleared();
    expect(localStorage.getItem(CLIENT_OWNER_STORAGE_KEY)).toBe('B');
  });

  it('forgets the owner when the session is rejected', async () => {
    const tab = await loadFreshTab('A');
    await bootFromCookie(tab, 'A');

    tab.useAuthStore.getState().invalidateRejectedSession('A');

    expect(localStorage.getItem(CLIENT_OWNER_STORAGE_KEY)).toBeNull();
    expectCachesCleared();
  });

  it('clears the caches when no owner stamp exists (first deploy) and writes one', async () => {
    seedPersistedCaches({ owner: 'A' });
    const tab = await loadFreshTab('A');

    await bootFromCookie(tab, 'A');

    expectCachesCleared();
    expect(localStorage.getItem(CLIENT_OWNER_STORAGE_KEY)).toBe('A');
  });

  it('clears the caches when the owner stamp cannot be read', async () => {
    localStorage.setItem(CLIENT_OWNER_STORAGE_KEY, 'A');
    seedPersistedCaches({ owner: 'A' });
    const getItem = Storage.prototype.getItem;
    const denied = vi
      .spyOn(Storage.prototype, 'getItem')
      .mockImplementation(function (this: Storage, key: string) {
        if (key === CLIENT_OWNER_STORAGE_KEY) {
          throw new DOMException('Storage access denied', 'SecurityError');
        }
        return getItem.call(this, key);
      });
    const tab = await loadFreshTab('A');

    await bootFromCookie(tab, 'A');

    expectCachesCleared();
    denied.mockRestore();
    expect(localStorage.getItem(CLIENT_OWNER_STORAGE_KEY)).toBe('A');
  });

  it('still clears when a tab holding A in memory receives SIGNED_IN for B', async () => {
    const tab = await loadFreshTab('A');
    await bootFromCookie(tab, 'A');
    // Another tab switched to B and already re-stamped the shared storage;
    // auth-js hands this tab B's session with the event.
    localStorage.setItem(CLIENT_OWNER_STORAGE_KEY, 'B');
    seedPersistedCaches({ owner: 'A' });
    tab.getUser.mockResolvedValue({ data: { user: { id: 'B' } }, error: null });
    tab.getSession.mockResolvedValue({ data: { session: session('B') } });
    tab.apiGet.mockResolvedValue(profile('B'));

    tab.emit('SIGNED_IN', session('B'));

    expectCachesCleared();
    expect(tab.useAuthStore.getState().user).toBeNull();
    // The switch drops the stamp; only B's verified profile re-stamps it.
    expect(localStorage.getItem(CLIENT_OWNER_STORAGE_KEY)).toBeNull();
    // Drain the profile fetch the event scheduled, so this tab's module
    // graph cannot keep clearing storage underneath the next test.
    await vi.waitFor(() =>
      expect(tab.useAuthStore.getState().user?.id).toBe('B')
    );
    expect(localStorage.getItem(CLIENT_OWNER_STORAGE_KEY)).toBe('B');
  });
});

describe('cached payloads carry their owner', () => {
  beforeEach(() => {
    localStorage.clear();
    vi.clearAllMocks();
  });

  afterEach(() => {
    localStorage.clear();
  });

  it("a lagging tab cannot hand A's payloads to B through a B-stamped origin", async () => {
    // Tab 2 switched this origin to B (stamp = B). Tab 1, still signed in as
    // A, then persisted A's selection and workspace before its own SIGNED_IN(B)
    // arrived. A fresh B tab keeps the caches at the stamp level, so the
    // payloads themselves must refuse A.
    localStorage.setItem(CLIENT_OWNER_STORAGE_KEY, 'B');
    seedPersistedCaches({ owner: 'A' });
    const tab = await loadFreshTab('B');
    serveBackend(tab, 'B', { list: [B_WORKSPACE] });

    await bootFromCookie(tab, 'B');
    expect(tab.useChatStore.getState()).toMatchObject({
      currentWorkspaceId: null,
      currentConversationId: null,
      currentThreadId: null,
      ownerUserId: 'B',
    });
    expect(localStorage.getItem('chat-storage') ?? '').not.toContain('A-only');

    await tab.useChatStore.getState().initializeDefaultWorkspace();

    const urls = requestedUrls(tab);
    expect(urls).not.toContain('/api/v2/workspaces/A-only-workspace');
    expect(urls).toContain('/api/v2/workspaces');
    expect(tab.useChatStore.getState().currentWorkspaceId).toBe('B-workspace');
    expect(tab.useChatStore.getState().workspaces).not.toContainEqual(
      expect.objectContaining({ name: 'A-only workspace' })
    );
    const record = localStorage.getItem('default-workspace-object') ?? '';
    expect(record).not.toContain('A-only');
    expect(JSON.parse(record)).toMatchObject({
      ownerUserId: 'B',
      workspace: { id: 'B-workspace' },
    });
  });

  it('same-user payloads are used without a workspace list request', async () => {
    localStorage.setItem(CLIENT_OWNER_STORAGE_KEY, 'A');
    seedPersistedCaches({ owner: 'A' });
    const tab = await loadFreshTab('A');
    serveBackend(tab, 'A', {
      list: [A_WORKSPACE],
      byId: { 'A-only-workspace': A_WORKSPACE },
    });

    await bootFromCookie(tab, 'A');
    expect(tab.useChatStore.getState()).toMatchObject({
      currentWorkspaceId: 'A-only-workspace',
      currentConversationId: 'A-only-conversation',
      currentThreadId: 'A-only-thread',
      ownerUserId: 'A',
    });

    await tab.useChatStore.getState().initializeDefaultWorkspace();

    const urls = requestedUrls(tab);
    expect(urls).toContain('/api/v2/workspaces/A-only-workspace');
    expect(urls).not.toContain('/api/v2/workspaces');
    expect(tab.useChatStore.getState().currentWorkspaceId).toBe(
      'A-only-workspace'
    );
  });

  it('discards legacy unstamped payloads once and stamps their replacements', async () => {
    localStorage.setItem(CLIENT_OWNER_STORAGE_KEY, 'A');
    seedPersistedCaches();
    const tab = await loadFreshTab('A');
    serveBackend(tab, 'A', {
      list: [A_WORKSPACE],
      byId: { 'A-only-workspace': A_WORKSPACE },
    });

    await bootFromCookie(tab, 'A');
    expect(tab.useChatStore.getState()).toMatchObject({
      currentWorkspaceId: null,
      currentThreadId: null,
      ownerUserId: 'A',
    });
    await tab.useChatStore.getState().initializeDefaultWorkspace();

    const urls = requestedUrls(tab);
    expect(urls).not.toContain('/api/v2/workspaces/A-only-workspace');
    expect(urls).toContain('/api/v2/workspaces');
    for (const key of [
      'default-workspace-cached-at',
      'default-workspace-id',
      'default-conversation-id',
    ]) {
      expect(localStorage.getItem(key), key).toBeNull();
    }
    expect(
      JSON.parse(localStorage.getItem('default-workspace-object') ?? 'null')
    ).toMatchObject({ ownerUserId: 'A', workspace: { id: 'A-only-workspace' } });
    expect(localStorage.getItem('chat-storage') ?? '').toContain(
      '"ownerUserId":"A"'
    );

    // The stamped replacements serve the next same-user load.
    const again = await loadFreshTab('A');
    serveBackend(again, 'A', {
      list: [A_WORKSPACE],
      byId: { 'A-only-workspace': A_WORKSPACE },
    });
    await bootFromCookie(again, 'A');
    expect(again.useChatStore.getState().currentWorkspaceId).toBe(
      'A-only-workspace'
    );
    await again.useChatStore.getState().initializeDefaultWorkspace();
    expect(requestedUrls(again)).toContain('/api/v2/workspaces/A-only-workspace');
    expect(requestedUrls(again)).not.toContain('/api/v2/workspaces');
  });
});
