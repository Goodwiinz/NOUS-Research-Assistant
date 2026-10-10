import React from 'react';
import { beforeEach, describe, expect, it } from 'vitest';
import { render, screen } from '@testing-library/react';
import { ProgressPanel } from '../ProgressPanel';
import { useAgentActivityStore } from '@/stores/agentActivityStore';
import { useAgentChatStore } from '@/store/agentChatStore';

describe('ProgressPanel persisted fallback', () => {
  beforeEach(() => {
    useAgentActivityStore.setState({ runs: {}, currentThreadId: null });
    useAgentChatStore.getState().reset();
  });

  it('renders a reloaded pending draft plan as in progress', () => {
    useAgentChatStore.setState({
      activeThreadId: 'thread-1',
      messages: [
        {
          id: 'assistant-1',
          role: 'assistant',
          content: 'Still running',
          timestamp: new Date(),
          plan: [
            {
              step: 1,
              description: 'Create draft',
              tool: 'create_draft',
              args_hint: {},
              depends_on: [],
            },
          ],
          toolExecutions: [
            {
              id: 'exec-1',
              toolName: 'create_draft',
              toolDisplayName: 'Create draft',
              args: {},
              status: 'pending',
            },
          ],
        },
      ],
    });

    render(<ProgressPanel threadId="thread-1" />);

    expect(screen.getByText('Create draft')).toBeInTheDocument();
    expect(screen.getByText('In progress')).toBeInTheDocument();
    expect(screen.getByText('0 of 1')).toBeInTheDocument();
  });

  it('correlates repeated persisted tools positionally', () => {
    useAgentChatStore.setState({
      activeThreadId: 'thread-1',
      messages: [
        {
          id: 'assistant-1',
          role: 'assistant',
          content: 'One search completed',
          timestamp: new Date(),
          plan: [
            {
              step: 1,
              description: 'Search first source',
              tool: 'search_documents',
              args_hint: {},
              depends_on: [],
            },
            {
              step: 2,
              description: 'Search second source',
              tool: 'search_documents',
              args_hint: {},
              depends_on: [],
            },
          ],
          toolExecutions: [
            {
              id: 'exec-1',
              toolName: 'search_documents',
              toolDisplayName: 'Search documents',
              args: {},
              status: 'completed',
            },
          ],
        },
      ],
    });

    render(<ProgressPanel threadId="thread-1" />);

    expect(screen.getByText('1 of 2')).toBeInTheDocument();
    expect(screen.getByText('Done')).toBeInTheDocument();
    expect(screen.getByText('Pending')).toBeInTheDocument();
  });

  it('renders a persisted cancelled execution as terminal', () => {
    useAgentChatStore.setState({
      activeThreadId: 'thread-1',
      messages: [
        {
          id: 'assistant-1',
          role: 'assistant',
          content: 'Stopped',
          timestamp: new Date(),
          plan: [
            {
              step: 1,
              description: 'Create draft',
              tool: 'create_draft',
              args_hint: {},
              depends_on: [],
            },
          ],
          toolExecutions: [
            {
              id: 'exec-1',
              toolName: 'create_draft',
              toolDisplayName: 'Create draft',
              args: {},
              status: 'cancelled',
            },
          ],
        },
      ],
    });

    render(<ProgressPanel threadId="thread-1" />);

    expect(screen.getByText('Cancelled')).toBeInTheDocument();
    expect(screen.queryByText('In progress')).not.toBeInTheDocument();
    expect(screen.getByText('0 of 1')).toBeInTheDocument();
  });

  it('marks tool-less persisted steps done after a successful turn', () => {
    useAgentChatStore.setState({
      activeThreadId: 'thread-1',
      messages: [
        {
          id: 'assistant-1',
          role: 'assistant',
          content: 'Completed response',
          timestamp: new Date(),
          plan: [
            {
              step: 1,
              description: 'Respond with findings',
              tool: '',
              args_hint: {},
              depends_on: [],
            },
          ],
          toolExecutions: [],
        },
      ],
    });

    render(<ProgressPanel threadId="thread-1" />);

    expect(screen.getByText('Done')).toBeInTheDocument();
    expect(screen.getByText('1 of 1')).toBeInTheDocument();
  });
});
