import { render } from '@/test/test-utils';
import { beforeEach, describe, expect, it, vi } from 'vitest';

import ChatLayoutClient from '../../../../app/(dashboard)/chat/chat-layout-client';

const mockPush = vi.fn();

vi.mock('@/components/context-rail', () => ({
  ContextRail: () => <aside data-testid="context-rail" />,
}));

vi.mock('@/hooks', () => ({
  useChatPersistence: () => ({
    currentThreadId: 'thread-1',
    currentWorkspaceId: 'workspace-1',
  }),
}));

vi.mock('@/store/chat-store', () => ({
  useChatStore: (selector: (state: unknown) => unknown) =>
    selector({
      workspaces: [{ id: 'workspace-1', name: 'Default workspace' }],
      // Fields read by selectCurrentThreadProjectId — no thread selected.
      currentThreadId: null,
      threadToConversation: {},
      threads: {},
    }),
  selectCurrentThreadProjectId: () => undefined,
  resolveBoundProjectId: () => undefined,
}));

vi.mock('@/stores/authStore', () => ({
  useAuthStore: (selector: (state: unknown) => unknown) =>
    selector({ isAuthenticated: false }),
}));

vi.mock('next/navigation', () => ({
  useRouter: () => ({ push: mockPush }),
  useSearchParams: () => new URLSearchParams(),
}));

describe('ChatLayoutClient', () => {
  beforeEach(() => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response(null)));
  });

  it('does not emit local debug ingest requests from the browser', async () => {
    render(
      <ChatLayoutClient>
        <div>Chat content</div>
      </ChatLayoutClient>
    );

    await Promise.resolve();

    expect(fetch).not.toHaveBeenCalledWith(
      expect.stringContaining('127.0.0.1:7528'),
      expect.anything()
    );
  });
});
