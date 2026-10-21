import { beforeEach, describe, expect, it, vi } from 'vitest';
import { consumePdfQuoteIntent, stagePdfQuoteIntent } from '../chat-intent';

const state = vi.hoisted(() => ({
  auth: {
    isAuthenticated: true,
    user: { id: 'user-A', organization_id: 'org-A' },
  },
  workspace: 'workspace-A',
  reset: () => {},
  workspaceChanged: () => {},
  authChanged: () => {},
}));
vi.mock('@/stores/authStore', () => ({
  useAuthStore: {
    getState: () => state.auth,
    subscribe: (changed: () => void) => {
      state.authChanged = changed;
    },
  },
}));
vi.mock('@/store/chat-store', () => ({
  useChatStore: {
    getState: () => ({ currentWorkspaceId: state.workspace }),
    subscribe: (changed: () => void) => {
      state.workspaceChanged = changed;
    },
  },
  onChatSessionReset: (reset: () => void) => {
    state.reset = reset;
  },
}));

describe('transient PDF navigation quote', () => {
  // Mutation-verified 2026-10-14: removing chat-intent.ts:60 token matching,
  // :61 one-time consume, :63 owner matching, or :36/:37 transition listeners
  // fails the corresponding regression below; restored source passes.
  // Exact focused command (one -t pattern per named guard):
  // pnpm --dir frontend exec vitest run src/lib/pdf/__tests__/chat-intent.test.ts -t 'consumes the matching owner|does not let an old route|discards a quote after a mismatched|cannot resurrect'
  beforeEach(() => {
    state.reset();
    state.auth = {
      isAuthenticated: true,
      user: { id: 'user-A', organization_id: 'org-A' },
    };
    state.workspace = 'workspace-A';
  });

  it('consumes the matching owner, document, and range only once', () => {
    const token = stagePdfQuoteIntent('doc-A', [1, 2], '> private passage')!;
    expect(token).not.toContain('private passage');
    expect(consumePdfQuoteIntent(token, 'doc-A', [1, 2])).toBe(
      '> private passage'
    );
    expect(consumePdfQuoteIntent(token, 'doc-A', [1, 2])).toBeUndefined();
  });

  it('does not let an old route consume a newer selection', () => {
    const old = stagePdfQuoteIntent('doc-A', [1, 2], '> old')!;
    const current = stagePdfQuoteIntent('doc-A', [1, 2], '> current')!;
    expect(consumePdfQuoteIntent(old, 'doc-A', [1, 2])).toBeUndefined();
    expect(consumePdfQuoteIntent(current, 'doc-A', [1, 2])).toBe('> current');
  });

  it.each(['workspace', 'account'])(
    'cannot resurrect a quote after leaving and returning to its %s without consuming',
    (kind) => {
      const token = stagePdfQuoteIntent(
        'doc-A',
        [1, 2],
        '> old private passage'
      )!;
      if (kind === 'workspace') {
        state.workspace = 'workspace-B';
        state.workspaceChanged();
        state.workspace = 'workspace-A';
        state.workspaceChanged();
      } else {
        state.auth.user.id = 'user-B';
        state.authChanged();
        state.auth.user.id = 'user-A';
        state.authChanged();
      }
      expect(consumePdfQuoteIntent(token, 'doc-A', [1, 2])).toBeUndefined();
    }
  );

  it.each([
    'user',
    'organization',
    'workspace',
    'document',
    'range',
    'logout',
    'session reset',
  ])(
    'discards a quote after a mismatched %s and cannot resurrect it',
    (change) => {
      const token = stagePdfQuoteIntent('doc-A', [1, 2], '> private')!;
      if (change === 'user') state.auth.user.id = 'user-B';
      if (change === 'organization') state.auth.user.organization_id = 'org-B';
      if (change === 'workspace') state.workspace = 'workspace-B';
      if (change === 'logout') state.auth.isAuthenticated = false;
      if (change === 'session reset') state.reset();
      expect(
        consumePdfQuoteIntent(
          token,
          change === 'document' ? 'doc-B' : 'doc-A',
          change === 'range' ? [0, 0] : [1, 2]
        )
      ).toBeUndefined();
      state.auth = {
        isAuthenticated: true,
        user: { id: 'user-A', organization_id: 'org-A' },
      };
      state.workspace = 'workspace-A';
      expect(consumePdfQuoteIntent(token, 'doc-A', [1, 2])).toBeUndefined();
    }
  );
});
