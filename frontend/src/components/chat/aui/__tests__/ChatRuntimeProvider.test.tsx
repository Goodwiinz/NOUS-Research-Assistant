import { describe, expect, it, vi } from 'vitest';
import React from 'react';
import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import {
  ComposerPrimitive,
  useAui,
  useThread,
  useThreadRuntime,
} from '@assistant-ui/react';

import type { ChatPageMessage } from '@/components/chat/shared/cloudMessageView';
import { makeChatPageMessage } from '@/test/chatMessageFactory';
import { ChatRuntimeProvider } from '../ChatRuntimeProvider';

const messages: ChatPageMessage[] = [
  makeChatPageMessage({ id: 'u1', role: 'user', content: 'hi', timestamp: 1 }),
  makeChatPageMessage({
    id: 'a1',
    role: 'assistant',
    content: 'hello',
    timestamp: 2,
  }),
];

function Probe() {
  const msgCount = useThread((t) => t.messages.length);
  const running = useThread((t) => t.isRunning);
  return (
    <div data-testid="probe">
      {msgCount}:{String(running)}
    </div>
  );
}

function IdentityProbe() {
  const ids = useThread((thread) =>
    thread.messages.map((message) => message.id).join(',')
  );
  return <div data-testid="identity-probe">{ids}</div>;
}

function CancelProbe() {
  const runtime = useThreadRuntime();
  return (
    <button type="button" onClick={() => runtime.cancelRun()}>
      cancel
    </button>
  );
}

function QueueProbe(): React.JSX.Element {
  const aui = useAui();
  return (
    <ComposerPrimitive.Root
      onSubmit={() =>
        aui.composer().setRunConfig({
          custom: { attachmentIds: ['doc-1'] },
        })
      }
    >
      <ComposerPrimitive.Input aria-label="Follow-up" />
      <ComposerPrimitive.Send>Queue</ComposerPrimitive.Send>
      <ComposerPrimitive.Queue>
        {({ queueItem }) => <span>{queueItem.prompt}</span>}
      </ComposerPrimitive.Queue>
    </ComposerPrimitive.Root>
  );
}

describe('ChatRuntimeProvider', () => {
  it('projects messages and isRunning into the assistant-ui thread', () => {
    render(
      <ChatRuntimeProvider
        messages={messages}
        isRunning
        onSend={vi.fn()}
        onCancel={vi.fn()}
      >
        <Probe />
      </ChatRuntimeProvider>
    );

    expect(screen.getByTestId('probe')).toHaveTextContent('2:true');
  });

  it('invokes onCancel when the runtime cancels a run', async () => {
    const onCancel = vi.fn();
    const user = userEvent.setup();
    render(
      <ChatRuntimeProvider
        messages={messages}
        isRunning
        onSend={vi.fn()}
        onCancel={onCancel}
      >
        <CancelProbe />
      </ChatRuntimeProvider>
    );

    await user.click(screen.getByRole('button', { name: 'cancel' }));

    await waitFor(() => expect(onCancel).toHaveBeenCalledTimes(1));
  });

  it('queues a follow-up during a run and sends it after the run settles', async () => {
    const onSend = vi.fn();
    const user = userEvent.setup();
    const { rerender } = render(
      <ChatRuntimeProvider
        messages={messages}
        isRunning
        onSend={onSend}
        onCancel={vi.fn()}
      >
        <QueueProbe />
      </ChatRuntimeProvider>
    );

    await user.type(
      screen.getByRole('textbox', { name: 'Follow-up' }),
      'next{Enter}'
    );

    expect(onSend).not.toHaveBeenCalled();
    expect(screen.getByText('next')).toBeInTheDocument();

    // Mutation check: removing isSendDisabled from the busy expression in
    // packages/chat-runtime/runtime.ts must dispatch early and fail here.
    rerender(
      <ChatRuntimeProvider
        messages={messages}
        isRunning={false}
        isSendDisabled
        onSend={onSend}
        onCancel={vi.fn()}
      >
        <QueueProbe />
      </ChatRuntimeProvider>
    );
    expect(onSend).not.toHaveBeenCalled();

    rerender(
      <ChatRuntimeProvider
        messages={messages}
        isRunning={false}
        onSend={onSend}
        onCancel={vi.fn()}
      >
        <QueueProbe />
      </ChatRuntimeProvider>
    );

    await waitFor(() => expect(onSend).toHaveBeenCalledWith('next', ['doc-1']));
  });

  it('does not dispatch queued prompts when a run is cancelled', async () => {
    const onSend = vi.fn();
    const onCancel = vi.fn();
    const user = userEvent.setup();
    const view = (isRunning: boolean): React.JSX.Element => (
      <ChatRuntimeProvider
        messages={messages}
        isRunning={isRunning}
        onSend={onSend}
        onCancel={onCancel}
      >
        <QueueProbe />
        <CancelProbe />
      </ChatRuntimeProvider>
    );
    const { rerender } = render(view(true));
    await user.type(
      screen.getByRole('textbox', { name: 'Follow-up' }),
      'parked{Enter}'
    );
    expect(screen.getByText('parked')).toBeInTheDocument();
    await user.click(screen.getByRole('button', { name: 'cancel' }));
    expect(onCancel).toHaveBeenCalledTimes(1);
    rerender(view(false));
    expect(onSend).not.toHaveBeenCalled();
    await user.type(
      screen.getByRole('textbox', { name: 'Follow-up' }),
      'resume{Enter}'
    );
    await waitFor(() =>
      expect(onSend).toHaveBeenCalledWith('resume', ['doc-1'])
    );
  });

  it('keeps the runtime identity stable when an optimistic row becomes canonical', async () => {
    const optimistic = makeChatPageMessage({
      runtimeId: 'runtime-turn-1',
      source: 'optimistic',
      role: 'assistant',
      content: 'Persisted answer',
      timestamp: 2,
    });
    const canonical = makeChatPageMessage({
      id: 'db-message-1',
      runtimeId: 'runtime-turn-1',
      source: 'canonical',
      role: 'assistant',
      content: 'Persisted answer',
      timestamp: 2,
    });

    const { rerender } = render(
      <ChatRuntimeProvider
        messages={[optimistic]}
        isRunning={false}
        onSend={vi.fn()}
        onCancel={vi.fn()}
      >
        <IdentityProbe />
      </ChatRuntimeProvider>
    );
    expect(screen.getByTestId('identity-probe')).toHaveTextContent(
      'runtime-turn-1'
    );

    rerender(
      <ChatRuntimeProvider
        messages={[canonical]}
        isRunning={false}
        onSend={vi.fn()}
        onCancel={vi.fn()}
      >
        <IdentityProbe />
      </ChatRuntimeProvider>
    );

    await waitFor(() =>
      expect(screen.getByTestId('identity-probe')).toHaveTextContent(
        'runtime-turn-1'
      )
    );
    expect(screen.getByTestId('identity-probe')).not.toHaveTextContent(
      'db-message-1'
    );
  });
});
