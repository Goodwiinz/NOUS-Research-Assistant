import { beforeEach, describe, expect, it, vi } from 'vitest';
import React from 'react';
import { render, screen, fireEvent } from '@testing-library/react';
import { AgentFAB } from '../AgentFAB';
import { useAgentChatStore } from '@/store/agentChatStore';

const navigation = vi.hoisted(() => ({ pathname: '/documents' }));

vi.mock('next/navigation', () => ({
  usePathname: () => navigation.pathname,
}));

describe('AgentFAB', () => {
  beforeEach(() => {
    useAgentChatStore.getState().reset();
    navigation.pathname = '/documents';
  });

  it('renders open button when closed', () => {
    render(<AgentFAB />);
    expect(screen.getByLabelText('Open agent chat')).toBeInTheDocument();
  });

  it('renders close button when open', () => {
    useAgentChatStore.getState().openPanel();
    render(<AgentFAB />);
    expect(screen.getByLabelText('Close agent chat')).toBeInTheDocument();
  });

  it('toggles on click', () => {
    render(<AgentFAB />);
    fireEvent.click(screen.getByLabelText('Open agent chat'));
    expect(useAgentChatStore.getState().uiMode).toBe('panel');
  });

  it('shows unread badge when hasUnread and closed', () => {
    useAgentChatStore.setState({ hasUnread: true });
    const { container } = render(<AgentFAB />);
    expect(container.querySelector('.bg-destructive')).toBeInTheDocument();
  });

  it.each(['/chat', '/chat/'])(
    'hides the redundant launcher on %s',
    (pathname) => {
      navigation.pathname = pathname;
      render(<AgentFAB />);

      expect(
        screen.queryByLabelText('Open agent chat')
      ).not.toBeInTheDocument();
    }
  );

  it('keeps the launcher functional away from chat', () => {
    navigation.pathname = '/documents';
    render(<AgentFAB />);

    fireEvent.click(screen.getByLabelText('Open agent chat'));

    expect(useAgentChatStore.getState().uiMode).toBe('panel');
    expect(screen.getByLabelText('Close agent chat')).toBeInTheDocument();
  });

  it('updates launcher visibility across client-side navigation', () => {
    const { rerender } = render(<AgentFAB />);
    expect(screen.getByLabelText('Open agent chat')).toBeInTheDocument();

    navigation.pathname = '/chat';
    rerender(<AgentFAB />);
    expect(screen.queryByLabelText('Open agent chat')).not.toBeInTheDocument();

    navigation.pathname = '/documents';
    rerender(<AgentFAB />);
    expect(screen.getByLabelText('Open agent chat')).toBeInTheDocument();
  });

  it('preserves the close affordance when an open panel reaches chat', () => {
    useAgentChatStore.getState().openPanel();
    navigation.pathname = '/chat';
    render(<AgentFAB />);

    fireEvent.click(screen.getByLabelText('Close agent chat'));

    expect(useAgentChatStore.getState().uiMode).toBe('closed');
    expect(screen.queryByLabelText('Open agent chat')).not.toBeInTheDocument();
  });
});
