/**
 * Unit tests for ChatInput streaming/loading behavior
 *
 * Tests assistant-ui send/cancel controls while a run is active.
 */

import { beforeEach, describe, expect, it, vi } from 'vitest';
import React from 'react';
import { screen, fireEvent, waitFor } from '@testing-library/react';

// Mock framer-motion to avoid animation issues in tests
vi.mock('framer-motion', () => ({
  motion: {
    div: ({ children, ...props }: any) => <div {...props}>{children}</div>,
    span: ({ children, ...props }: any) => <span {...props}>{children}</span>,
    button: ({ children, ...props }: any) => (
      <button {...props}>{children}</button>
    ),
  },
  AnimatePresence: ({ children }: any) => <>{children}</>,
  useMotionValue: () => ({ set: vi.fn(), get: () => 0 }),
  useSpring: (v: any) => v,
  useTransform: () => ({ set: vi.fn(), get: () => 0 }),
  useReducedMotion: () => false,
}));

// Mock @assistant-ui/react primitives at the DOM boundary so ChatInput
// can render under test without a real runtime provider.
vi.mock('@assistant-ui/react', async () => {
  const React = await import('react');
  return {
    AssistantRuntimeProvider: ({ children }: any) => <>{children}</>,
    useExternalStoreRuntime: () => ({}),
    ComposerPrimitive: {
      Root: React.forwardRef<HTMLFormElement, any>(function MockComposerRoot(
        { children, asChild: _asChild, ...props },
        ref
      ) {
        return (
          <form ref={ref} {...props}>
            {children}
          </form>
        );
      }),
      Input: ({ children, asChild: _asChild, ...props }: any) =>
        React.cloneElement(React.Children.only(children), props),
      Queue: () => null,
      Send: ({ children, ...props }: React.ComponentProps<'button'>) => (
        <button type="button" {...props}>
          {children}
        </button>
      ),
      Cancel: ({ children, ...props }: React.ComponentProps<'button'>) => (
        <button type="button" {...props}>
          {children}
        </button>
      ),
    },
    QueueItemPrimitive: {
      Text: () => null,
      Steer: ({ children }: React.ComponentProps<'button'>) => (
        <button>{children}</button>
      ),
      Remove: ({ children }: React.ComponentProps<'button'>) => (
        <button>{children}</button>
      ),
    },
    useAui: () => ({
      composer: () => ({
        getState: () => ({ text: '' }),
        setText: vi.fn(),
        setRunConfig: vi.fn(),
      }),
    }),
  };
});

import { ChatInput } from '../ChatInput';
import { renderWithChatRuntime } from './renderWithChatRuntime';

// Default props for all tests
const defaultProps = {
  value: '',
  onChange: vi.fn(),
  onSubmit: vi.fn(),
  onStop: vi.fn(),
  isLoading: false,
  enableRAG: true,
  onRAGToggle: vi.fn(),
};

describe('ChatInput streaming behavior', () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  describe('when isLoading is true', () => {
    it('shows a Stop button', () => {
      renderWithChatRuntime(<ChatInput {...defaultProps} isLoading={true} />);

      const stopButton = screen.getByText('Stop');
      expect(stopButton).toBeInTheDocument();
    });

    it('keeps the textarea enabled for a queued follow-up', () => {
      renderWithChatRuntime(<ChatInput {...defaultProps} isLoading />);
      expect(screen.getByRole('textbox')).not.toBeDisabled();
    });

    it('shows Queue instead of Send for a non-empty follow-up', () => {
      renderWithChatRuntime(
        <ChatInput {...defaultProps} isLoading value="follow up" />
      );

      expect(screen.getByText('Queue')).toBeInTheDocument();
      const sendButton = screen.queryByText('Send');
      expect(sendButton).not.toBeInTheDocument();
    });
  });

  describe('when isLoading is false', () => {
    it('renders the styled composer as a form', () => {
      const { container } = renderWithChatRuntime(
        <ChatInput {...defaultProps} isLoading={false} value="Hello" />
      );
      const composer = container.querySelector('form');
      expect(composer).toBeInTheDocument();
      expect(composer).toContainElement(screen.getByRole('textbox'));
      expect(composer).toContainElement(screen.getByText('Send'));
    });

    it('submits once when the Send button is clicked', () => {
      const onSubmit = vi.fn();
      renderWithChatRuntime(
        <ChatInput
          {...defaultProps}
          isLoading={false}
          value="Hello"
          onSubmit={onSubmit}
        />
      );
      fireEvent.click(screen.getByText('Send'));
      expect(onSubmit).toHaveBeenCalledTimes(1);
    });

    it('shows the Send button (not Stop)', () => {
      renderWithChatRuntime(
        <ChatInput {...defaultProps} isLoading={false} value="Hello" />
      );

      const sendButton = screen.getByText('Send');
      expect(sendButton).toBeInTheDocument();

      const stopButton = screen.queryByText('Stop');
      expect(stopButton).not.toBeInTheDocument();
    });

    it('does not disable the textarea', () => {
      renderWithChatRuntime(<ChatInput {...defaultProps} isLoading={false} />);

      const textarea = screen.getByRole('textbox');
      expect(textarea).not.toBeDisabled();
    });
  });

  describe('composer status pill (removed — message-area pill owns status)', () => {
    it('never renders a composer status pill, idle or loading', () => {
      const { rerender } = renderWithChatRuntime(
        <ChatInput {...defaultProps} />
      );
      expect(screen.queryByText(/Nous is/)).not.toBeInTheDocument();
      expect(screen.queryByText('nous-agent')).not.toBeInTheDocument();

      rerender(<ChatInput {...defaultProps} isLoading />);
      expect(screen.queryByText(/Nous is/)).not.toBeInTheDocument();
      expect(screen.queryByRole('status')).not.toBeInTheDocument();
    });
  });

  describe('Use my sources switch', () => {
    it('renders a switch reflecting the RAG state and toggles it', () => {
      const onRAGToggle = vi.fn();
      const { rerender } = renderWithChatRuntime(
        <ChatInput
          {...defaultProps}
          enableRAG={false}
          onRAGToggle={onRAGToggle}
        />
      );
      const toggle = screen.getByRole('switch', { name: 'Use my sources' });
      expect(toggle).toHaveAttribute('aria-checked', 'false');
      expect(toggle).toHaveAttribute('title', 'Answers without your sources.');
      fireEvent.click(toggle);
      expect(onRAGToggle).toHaveBeenCalledWith(true);

      rerender(
        <ChatInput {...defaultProps} enableRAG onRAGToggle={onRAGToggle} />
      );
      expect(
        screen.getByRole('switch', { name: 'Use my sources' })
      ).toHaveAttribute('title', 'Grounds answers in your sources.');
    });

    it('drops the Ultra Thinking wording', () => {
      renderWithChatRuntime(<ChatInput {...defaultProps} />);
      expect(screen.queryByText(/Ultra Thinking/i)).not.toBeInTheDocument();
    });
  });

  describe('keyboard hint row', () => {
    it('is gone, the Send button carries the shortcut', () => {
      renderWithChatRuntime(<ChatInput {...defaultProps} value="hi" />);
      expect(screen.queryByText(/to send/)).not.toBeInTheDocument();
      expect(screen.queryByText(/for newline/)).not.toBeInTheDocument();
      expect(screen.getByText('Send').closest('button')).toHaveAttribute(
        'title',
        'Send (Enter)'
      );
    });
  });

  describe('slash command menu', () => {
    it('opens a listbox of commands when the value is "/"', () => {
      renderWithChatRuntime(<ChatInput {...defaultProps} value="/" />);
      expect(screen.getByRole('listbox')).toBeInTheDocument();
      expect(screen.getByText('/new')).toBeInTheDocument();
      expect(screen.getByText('/projects')).toBeInTheDocument();
    });

    it('uses valid textarea autocomplete semantics while the menu is open', () => {
      renderWithChatRuntime(<ChatInput {...defaultProps} value="/" />);
      const textarea = screen.getByRole('textbox');

      expect(textarea).not.toHaveAttribute('aria-expanded');
      expect(textarea).toHaveAttribute('aria-haspopup', 'listbox');
      expect(textarea).toHaveAttribute('aria-autocomplete', 'list');
      expect(textarea).toHaveAttribute(
        'aria-controls',
        'slash-command-listbox'
      );
    });

    it('does not show the menu for normal text', () => {
      renderWithChatRuntime(<ChatInput {...defaultProps} value="hello" />);
      expect(screen.queryByRole('listbox')).not.toBeInTheDocument();
    });

    it('runs the highlighted command on Enter (and does not submit)', () => {
      const onCommand = vi.fn();
      const onSubmit = vi.fn();
      renderWithChatRuntime(
        <ChatInput
          {...defaultProps}
          value="/new"
          onCommand={onCommand}
          onSubmit={onSubmit}
        />
      );
      fireEvent.keyDown(screen.getByRole('textbox'), { key: 'Enter' });
      expect(onCommand).toHaveBeenCalledWith('new');
      expect(onSubmit).not.toHaveBeenCalled();
    });

    it('runs a command when its row is clicked', () => {
      const onCommand = vi.fn();
      renderWithChatRuntime(
        <ChatInput {...defaultProps} value="/" onCommand={onCommand} />
      );
      fireEvent.click(screen.getByText('/clear'));
      expect(onCommand).toHaveBeenCalledWith('clear');
    });

    it('Escape dismisses the menu without clearing the input', () => {
      const onChange = vi.fn();
      renderWithChatRuntime(
        <ChatInput {...defaultProps} value="/new" onChange={onChange} />
      );
      expect(screen.getByRole('listbox')).toBeInTheDocument();
      fireEvent.keyDown(screen.getByRole('textbox'), { key: 'Escape' });
      expect(onChange).not.toHaveBeenCalled();
      expect(screen.queryByRole('listbox')).not.toBeInTheDocument();
    });

    it('opens Commands over an existing draft and restores its selection on Escape', () => {
      const onChange = vi.fn();
      renderWithChatRuntime(
        <ChatInput
          {...defaultProps}
          value="Draft to preserve"
          onChange={onChange}
        />
      );
      const textarea = screen.getByRole('textbox') as HTMLTextAreaElement;
      textarea.focus();
      textarea.setSelectionRange(2, 7);

      fireEvent.click(screen.getByRole('button', { name: 'Open commands' }));
      expect(screen.getByRole('listbox')).toBeInTheDocument();
      expect(onChange).not.toHaveBeenCalled();
      expect(textarea).toHaveValue('Draft to preserve');

      fireEvent.keyDown(textarea, { key: 'Escape' });
      expect(screen.queryByRole('listbox')).not.toBeInTheDocument();
      expect(onChange).not.toHaveBeenCalled();
      expect(document.activeElement).toBe(textarea);
      expect(textarea.selectionStart).toBe(2);
      expect(textarea.selectionEnd).toBe(7);
    });

    it('runs a safe command without discarding an existing draft', () => {
      const onChange = vi.fn();
      const onCommand = vi.fn();
      renderWithChatRuntime(
        <ChatInput
          {...defaultProps}
          value="Draft to preserve"
          onChange={onChange}
          onCommand={onCommand}
        />
      );

      fireEvent.click(screen.getByRole('button', { name: 'Open commands' }));
      fireEvent.click(screen.getByText('/help'));

      expect(onCommand).toHaveBeenCalledWith('help');
      expect(onChange).not.toHaveBeenCalledWith('');
    });

    it('also confirms retry before it can replace an existing draft', () => {
      const onChange = vi.fn();
      const onCommand = vi.fn();
      renderWithChatRuntime(
        <ChatInput
          {...defaultProps}
          value="Draft to preserve"
          onChange={onChange}
          onCommand={onCommand}
        />
      );

      fireEvent.click(screen.getByRole('button', { name: 'Open commands' }));
      fireEvent.click(screen.getByText('/retry'));

      expect(screen.getByRole('alertdialog')).toBeInTheDocument();
      expect(onChange).not.toHaveBeenCalled();
      expect(onCommand).not.toHaveBeenCalled();
    });

    it('requires an explicit discard before a draft-affecting command runs', async () => {
      const onChange = vi.fn();
      const onCommand = vi.fn();
      renderWithChatRuntime(
        <ChatInput
          {...defaultProps}
          value="Draft to preserve"
          onChange={onChange}
          onCommand={onCommand}
        />
      );
      const textarea = screen.getByRole('textbox') as HTMLTextAreaElement;
      textarea.focus();
      textarea.setSelectionRange(2, 7);

      fireEvent.click(screen.getByRole('button', { name: 'Open commands' }));
      fireEvent.click(screen.getByText('/clear'));

      expect(screen.getByRole('alertdialog')).toBeInTheDocument();
      expect(onCommand).not.toHaveBeenCalled();
      expect(onChange).not.toHaveBeenCalled();

      fireEvent.click(
        screen.getByRole('button', { name: 'Keep draft', exact: true })
      );
      expect(screen.queryByRole('alertdialog')).not.toBeInTheDocument();
      expect(onCommand).not.toHaveBeenCalled();
      await waitFor(() => expect(document.activeElement).toBe(textarea));
      expect(textarea.selectionStart).toBe(2);
      expect(textarea.selectionEnd).toBe(7);

      fireEvent.click(screen.getByRole('button', { name: 'Open commands' }));
      fireEvent.click(screen.getByText('/clear'));
      fireEvent.click(
        screen.getByRole('button', {
          name: 'Run and discard draft',
          exact: true,
        })
      );
      expect(onChange).toHaveBeenCalledWith('');
      expect(onCommand).toHaveBeenCalledWith('clear');
    });
  });
});
