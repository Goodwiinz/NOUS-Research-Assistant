/**
 * The persisted chat selection (`chat-storage`) carries the account it was
 * written for. Hydration holds it back; it is adopted only when the auth
 * store publishes a verified user with the same id, and discarded (once,
 * for good) otherwise. A payload without an owner, which is what today's
 * deployed code writes, is never adopted.
 *
 * Mutation check (docs/engineering/testing.md), from the repository root:
 *
 *   pnpm --dir frontend exec vitest run src/store/__tests__/chat-store.persistedSelection.test.ts
 *
 * - Guard `frontend/src/store/chat/slices/selectionSlice.ts`
 *   adoptPersistedSelection: replace the `pending.ownerUserId === ownerUserId`
 *   comparison with `true`. Red: "discards a selection owned by another
 *   account" and "never adopts a legacy selection without an owner" (A's
 *   thread id is adopted for B / for a payload nobody owns).
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

vi.mock('@/services/api-client', () => ({
  api: { get: vi.fn(), post: vi.fn(), clearAuth: vi.fn() },
}));

const A_SELECTION = {
  currentWorkspaceId: 'A-only-workspace',
  currentConversationId: 'A-only-conversation',
  currentThreadId: 'A-only-thread',
};

function seedChatStorage(state: Record<string, unknown>): void {
  localStorage.setItem(
    'chat-storage',
    JSON.stringify({ state: { sidebarCollapsed: true, ...state }, version: 0 })
  );
}

function persistedState(): Record<string, unknown> {
  const raw = localStorage.getItem('chat-storage');
  return raw
    ? (JSON.parse(raw) as { state: Record<string, unknown> }).state
    : {};
}

async function loadFreshStore(): Promise<
  typeof import('@/store/chat-store').useChatStore
> {
  vi.resetModules();
  const { useChatStore } = await import('@/store/chat-store');
  return useChatStore;
}

describe('persisted chat selection ownership', () => {
  beforeEach(() => {
    localStorage.clear();
  });

  afterEach(() => {
    localStorage.clear();
  });

  it('holds the selection back at hydration but keeps the sidebar preference', async () => {
    seedChatStorage({ ...A_SELECTION, ownerUserId: 'A' });

    const useChatStore = await loadFreshStore();

    expect(useChatStore.getState()).toMatchObject({
      currentWorkspaceId: null,
      currentConversationId: null,
      currentThreadId: null,
      ownerUserId: null,
      sidebarCollapsed: true,
    });
  });

  it('adopts a selection owned by the published user and stamps later writes', async () => {
    seedChatStorage({ ...A_SELECTION, ownerUserId: 'A' });
    const useChatStore = await loadFreshStore();

    useChatStore.getState().adoptPersistedSelection('A');

    expect(useChatStore.getState()).toMatchObject({
      ...A_SELECTION,
      ownerUserId: 'A',
    });
    expect(persistedState()).toMatchObject({
      ...A_SELECTION,
      ownerUserId: 'A',
    });
  });

  it('discards a selection owned by another account', async () => {
    seedChatStorage({ ...A_SELECTION, ownerUserId: 'A' });
    const useChatStore = await loadFreshStore();

    useChatStore.getState().adoptPersistedSelection('B');

    expect(useChatStore.getState()).toMatchObject({
      currentWorkspaceId: null,
      currentConversationId: null,
      currentThreadId: null,
      ownerUserId: 'B',
    });
    expect(JSON.stringify(persistedState())).not.toContain('A-only');
    expect(persistedState()).toMatchObject({ ownerUserId: 'B' });
  });

  it('never adopts a legacy selection without an owner', async () => {
    seedChatStorage(A_SELECTION);
    const useChatStore = await loadFreshStore();

    useChatStore.getState().adoptPersistedSelection('A');

    expect(useChatStore.getState()).toMatchObject({
      currentWorkspaceId: null,
      currentThreadId: null,
      ownerUserId: 'A',
    });
    expect(JSON.stringify(persistedState())).not.toContain('A-only');
  });

  it('consumes the hydrated selection once, so a later account cannot resurrect it', async () => {
    seedChatStorage({ ...A_SELECTION, ownerUserId: 'A' });
    const useChatStore = await loadFreshStore();

    useChatStore.getState().adoptPersistedSelection('B');
    useChatStore.getState().reset();
    useChatStore.getState().adoptPersistedSelection('A');

    expect(useChatStore.getState()).toMatchObject({
      currentThreadId: null,
      ownerUserId: 'A',
    });
  });

  it('does not override a selection made before the profile published', async () => {
    seedChatStorage({ ...A_SELECTION, ownerUserId: 'A' });
    const useChatStore = await loadFreshStore();
    useChatStore.setState({ currentThreadId: 'deep-linked-thread' });

    useChatStore.getState().adoptPersistedSelection('A');

    expect(useChatStore.getState().currentThreadId).toBe('deep-linked-thread');
  });

  it('forgets the owner on reset', async () => {
    const useChatStore = await loadFreshStore();
    useChatStore.getState().adoptPersistedSelection('A');

    useChatStore.getState().reset();

    expect(useChatStore.getState().ownerUserId).toBeNull();
  });

  it('reset also throws away a parked selection the same account could claim', async () => {
    // The auth store resets when it cannot vouch for the origin's caches
    // (no owner stamp, unreadable storage). The account it publishes right
    // after must not inherit the hydrated selection just because the payload
    // names it.
    seedChatStorage({ ...A_SELECTION, ownerUserId: 'A' });
    const useChatStore = await loadFreshStore();

    useChatStore.getState().reset();
    useChatStore.getState().adoptPersistedSelection('A');

    expect(useChatStore.getState()).toMatchObject({
      currentWorkspaceId: null,
      currentThreadId: null,
      ownerUserId: 'A',
    });
    expect(JSON.stringify(persistedState())).not.toContain('A-only');
  });
});
