import { describe, expect, it, vi } from 'vitest';
import React from 'react';
import { render, screen, type RenderResult } from '@testing-library/react';

import { makeChatPageMessage } from '@/test/chatMessageFactory';
import { ChatRuntimeProvider } from '../ChatRuntimeProvider';
import { AuiMessageByIndex } from '../AuiMessage';

/**
 * The server-authored `error.category` used to be write-only — recorded on the
 * bubble and never read. These tests pin the minimal read: a short helper line
 * per category, and Retry hidden for the two categories where an identical
 * retry provably cannot succeed.
 */

const noop = vi.fn();

function renderErrorMessage(
  category: string | undefined,
  content = ''
): RenderResult {
  const messages = [
    makeChatPageMessage({
      id: 'u1',
      role: 'user',
      content: 'summarize this',
      timestamp: 1,
    }),
    makeChatPageMessage({
      id: 'a1',
      role: 'assistant',
      content,
      timestamp: 2,
      error: {
        message: 'This response failed to generate. Please try again.',
        ...(category ? { category } : {}),
      },
    }),
  ];
  return render(
    <ChatRuntimeProvider
      messages={messages}
      isRunning={false}
      onSend={noop}
      onCancel={noop}
    >
      <AuiMessageByIndex index={1} message={messages[1]} onRetry={noop} />
    </ChatRuntimeProvider>
  );
}

describe('AuiMessage error category', () => {
  it('renders the rate_limited helper line', () => {
    renderErrorMessage('rate_limited');
    expect(
      screen.getByText('The service is busy. Try again in a moment.')
    ).toBeInTheDocument();
  });

  it('renders the upstream_timeout helper line', () => {
    renderErrorMessage('upstream_timeout');
    expect(
      screen.getByText('The model took too long. Retry usually works.')
    ).toBeInTheDocument();
  });

  it('renders the invalid_request helper line', () => {
    renderErrorMessage('invalid_request');
    expect(
      screen.getByText("This request can't be retried as-is.")
    ).toBeInTheDocument();
  });

  it('renders the conflict helper line', () => {
    renderErrorMessage('conflict');
    expect(
      screen.getByText('A confirmation is already in progress.')
    ).toBeInTheDocument();
  });

  it('uses stable safe copy for an unmapped category', () => {
    renderErrorMessage('stream-error', 'Stream error: /srv/private/example.py');
    expect(
      screen.getByText('The response could not be completed. Please try again.')
    ).toBeInTheDocument();
    expect(screen.queryByText(/private\/example\.py/)).not.toBeInTheDocument();
  });

  it('uses stable safe copy when no category is present', () => {
    renderErrorMessage(undefined, 'Stream error: /srv/private/example.py');
    expect(
      screen.getByText('The response could not be completed. Please try again.')
    ).toBeInTheDocument();
    expect(screen.queryByText(/private\/example\.py/)).not.toBeInTheDocument();
  });

  it('degrades safely for an unknown category from a newer backend', () => {
    renderErrorMessage(
      'some_future_category',
      'Stream error: /srv/private/example.py'
    );
    expect(
      screen.getByText('The response could not be completed. Please try again.')
    ).toBeInTheDocument();
    expect(screen.queryByText(/private\/example\.py/)).not.toBeInTheDocument();
    expect(screen.getByRole('button', { name: /retry/i })).toBeInTheDocument();
  });

  it('keeps Retry for retryable categories', () => {
    renderErrorMessage('rate_limited');
    expect(screen.getByRole('button', { name: /retry/i })).toBeInTheDocument();
  });

  it('hides Retry for invalid_request — an identical retry cannot succeed', () => {
    renderErrorMessage('invalid_request');
    expect(
      screen.queryByRole('button', { name: /retry/i })
    ).not.toBeInTheDocument();
  });

  it('hides Retry for conflict — another confirmation holds the claim', () => {
    renderErrorMessage('conflict');
    expect(
      screen.queryByRole('button', { name: /retry/i })
    ).not.toBeInTheDocument();
  });

  it('renders permission guidance without offering a doomed Retry', () => {
    renderErrorMessage('permission_denied');
    expect(
      screen.getByText("You don't have permission to complete this action.")
    ).toBeInTheDocument();
    expect(
      screen.queryByRole('button', { name: /retry/i })
    ).not.toBeInTheDocument();
  });

  it('always renders the primary error message itself', () => {
    renderErrorMessage('conflict');
    expect(
      screen.getByText('This response failed to generate. Please try again.')
    ).toBeInTheDocument();
  });
});
