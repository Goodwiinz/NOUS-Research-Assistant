import { fireEvent, render, screen } from '@testing-library/react';
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

function openPalette(): void {
  fireEvent.keyDown(window, { key: 'k', metaKey: true });
}

describe('ChatLayoutClient command palette', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response(null)));
  });

  it('exposes dialog, combobox and listbox semantics', () => {
    render(
      <ChatLayoutClient>
        <div>Chat content</div>
      </ChatLayoutClient>
    );
    openPalette();

    const dialog = screen.getByRole('dialog', { name: /command palette/i });
    expect(dialog).toBeInTheDocument();

    const input = screen.getByRole('combobox', { name: /search commands/i });
    expect(input).toHaveAttribute('aria-controls', 'chat-command-list');
    expect(input).toHaveAttribute('aria-expanded', 'true');

    const listbox = screen.getByRole('listbox');
    expect(listbox).toHaveAttribute('id', 'chat-command-list');

    const options = screen.getAllByRole('option');
    expect(options).toHaveLength(7);
    expect(input).toHaveAttribute('aria-activedescendant', options[0].id);
    expect(options[0]).toHaveAttribute('aria-selected', 'true');
    expect(options[1]).toHaveAttribute('aria-selected', 'false');
  });

  it('routes the previously dead Upload Document entry', () => {
    render(
      <ChatLayoutClient>
        <div>Chat content</div>
      </ChatLayoutClient>
    );
    openPalette();

    fireEvent.click(screen.getByRole('option', { name: /upload document/i }));
    expect(mockPush).toHaveBeenCalledWith('/documents/upload');
  });

  it('has no Create Collection entry, since no collections route exists', () => {
    render(
      <ChatLayoutClient>
        <div>Chat content</div>
      </ChatLayoutClient>
    );
    openPalette();

    expect(screen.queryByText(/create collection/i)).toBeNull();
  });
});
