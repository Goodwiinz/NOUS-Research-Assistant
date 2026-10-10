/**
 * Unit tests for ChatInput file attach behavior.
 *
 * Verifies the Paperclip button is wired to a hidden file input that invokes
 * `onAttach` with the selected FileList.
 */

import { beforeEach, describe, expect, it, vi } from 'vitest';
import { act, fireEvent, screen, waitFor } from '@testing-library/react';
import { renderWithChatRuntime } from './renderWithChatRuntime';

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

vi.mock('@assistant-ui/react', async () => {
  const React = await import('react');
  const MockComposerRoot = React.forwardRef<HTMLFormElement, any>(
    ({ children, asChild: _asChild, ...props }, ref) => (
      <form ref={ref} {...props}>
        {children}
      </form>
    )
  );
  MockComposerRoot.displayName = 'MockComposerRoot';
  return {
    AssistantRuntimeProvider: ({ children }: any) => <>{children}</>,
    useExternalStoreRuntime: () => ({}),
    ComposerPrimitive: {
      Root: MockComposerRoot,
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

const baseProps = {
  value: '',
  onChange: vi.fn(),
  onSubmit: vi.fn(),
  onStop: vi.fn(),
  isLoading: false,
  enableRAG: true,
  onRAGToggle: vi.fn(),
};

describe('ChatInput file attach', () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it('renders a hidden file input wired into the Paperclip control', () => {
    const { container } = renderWithChatRuntime(<ChatInput {...baseProps} />);
    const input = container.querySelector(
      'input[type="file"]'
    ) as HTMLInputElement | null;
    expect(input).not.toBeNull();
    expect(input?.multiple).toBe(true);
  });

  it('calls onAttach with selected files', () => {
    const onAttach = vi.fn();
    const { container } = renderWithChatRuntime(
      <ChatInput {...baseProps} onAttach={onAttach} />
    );
    const input = container.querySelector(
      'input[type="file"]'
    ) as HTMLInputElement;
    const file = new File(['hello'], 'hello.pdf', {
      type: 'application/pdf',
    });
    Object.defineProperty(input, 'files', {
      value: [file] as unknown as FileList,
      configurable: true,
    });
    fireEvent.change(input);
    expect(onAttach).toHaveBeenCalledTimes(1);
    const calledWith = onAttach.mock.calls[0][0] as FileList;
    expect(calledWith[0].name).toBe('hello.pdf');
  });

  it('does not call onAttach when no files selected', () => {
    const onAttach = vi.fn();
    const { container } = renderWithChatRuntime(
      <ChatInput {...baseProps} onAttach={onAttach} />
    );
    const input = container.querySelector(
      'input[type="file"]'
    ) as HTMLInputElement;
    Object.defineProperty(input, 'files', {
      value: [] as unknown as FileList,
      configurable: true,
    });
    fireEvent.change(input);
    expect(onAttach).not.toHaveBeenCalled();
  });

  it('resets the input value so selecting the same file twice re-fires onAttach', () => {
    const onAttach = vi.fn();
    const { container } = renderWithChatRuntime(
      <ChatInput {...baseProps} onAttach={onAttach} />
    );
    const input = container.querySelector(
      'input[type="file"]'
    ) as HTMLInputElement;
    const file = new File(['hello'], 'hello.pdf', {
      type: 'application/pdf',
    });
    Object.defineProperty(input, 'files', {
      value: [file] as unknown as FileList,
      configurable: true,
    });
    fireEvent.change(input);
    expect(input.value).toBe('');
  });

  it('clears attachment chips and revokes blob URLs after a sent turn settles', async () => {
    const revokeObjectURL = vi.fn();
    vi.stubGlobal('URL', {
      ...URL,
      createObjectURL: vi.fn(() => 'blob:mock'),
      revokeObjectURL,
    });

    // A non-empty value is required for the submit guard to fire onSubmit.
    const onSubmit = vi.fn();
    const { container, rerender } = renderWithChatRuntime(
      <ChatInput {...baseProps} value="hi" onSubmit={onSubmit} />
    );
    const imageInput = container.querySelector(
      'input[accept="image/*"]'
    ) as HTMLInputElement;
    const image = new File(['x'], 'pic.png', { type: 'image/png' });
    Object.defineProperty(imageInput, 'files', {
      value: [image] as unknown as FileList,
      configurable: true,
    });
    fireEvent.change(imageInput);

    // Chip is present before send.
    expect(
      container.querySelector('ul[aria-label="Attached files"]')
    ).not.toBeNull();

    const form = container.querySelector('form') as HTMLFormElement;
    fireEvent.submit(form);

    expect(onSubmit).toHaveBeenCalledTimes(1);
    expect(revokeObjectURL).not.toHaveBeenCalled();
    rerender(
      <ChatInput {...baseProps} value="" onSubmit={onSubmit} isLoading />
    );
    rerender(
      <ChatInput
        {...baseProps}
        value=""
        onSubmit={onSubmit}
        isLoading={false}
      />
    );
    await waitFor(() =>
      expect(revokeObjectURL).toHaveBeenCalledWith('blob:mock')
    );
    // Chips list is gone (attachments emptied).
    expect(
      container.querySelector('ul[aria-label="Attached files"]')
    ).toBeNull();

    vi.unstubAllGlobals();
  });

  it('restores attachment chips when a canceled turn restores its draft', async () => {
    const onSubmit = vi.fn();
    const onAttach = vi.fn(async () => [{ ok: true, documentId: 'doc-1' }]);
    const { container, rerender } = renderWithChatRuntime(
      <ChatInput
        {...baseProps}
        value="cancel this turn"
        onSubmit={onSubmit}
        onAttach={onAttach}
      />
    );
    const input = container.querySelector(
      'input[type="file"]'
    ) as HTMLInputElement;
    Object.defineProperty(input, 'files', {
      value: [
        new File(['x'], 'paper.pdf', { type: 'application/pdf' }),
      ] as unknown as FileList,
      configurable: true,
    });
    await act(async () => fireEvent.change(input));

    fireEvent.submit(container.querySelector('form') as HTMLFormElement);
    rerender(
      <ChatInput
        {...baseProps}
        value=""
        onSubmit={onSubmit}
        onAttach={onAttach}
        isLoading
      />
    );
    rerender(
      <ChatInput
        {...baseProps}
        value="cancel this turn"
        onSubmit={onSubmit}
        onAttach={onAttach}
        isLoading={false}
      />
    );

    await waitFor(() =>
      expect(
        container.querySelector('ul[aria-label="Attached files"]')
      ).not.toBeNull()
    );
    fireEvent.submit(container.querySelector('form') as HTMLFormElement);
    expect(onSubmit).toHaveBeenLastCalledWith(['doc-1']);
  });

  describe('attachment ids handed to the send', () => {
    function attachFiles(container: HTMLElement, names: string[]): void {
      const input = container.querySelector(
        'input[type="file"]'
      ) as HTMLInputElement;
      Object.defineProperty(input, 'files', {
        value: names.map(
          (n) => new File(['x'], n, { type: 'application/pdf' })
        ) as unknown as FileList,
        configurable: true,
      });
      fireEvent.change(input);
    }

    it('sends the document ids of the uploaded chips', async () => {
      const onSubmit = vi.fn();
      const onAttach = vi.fn(async () => [
        { ok: true, documentId: 'doc-1' },
        { ok: true, documentId: 'doc-2' },
      ]);
      const { container } = renderWithChatRuntime(
        <ChatInput
          {...baseProps}
          value="hi"
          onSubmit={onSubmit}
          onAttach={onAttach}
        />
      );
      await act(async () => {
        attachFiles(container, ['a.pdf', 'b.pdf']);
      });

      fireEvent.submit(container.querySelector('form') as HTMLFormElement);

      expect(onSubmit).toHaveBeenCalledWith(['doc-1', 'doc-2']);
    });

    it('un-attaches a document when its chip is removed', async () => {
      // The chips are the visible contract: a file the user took back off the
      // composer must not ride along with the turn anyway.
      const onSubmit = vi.fn();
      const onAttach = vi.fn(async () => [
        { ok: true, documentId: 'doc-1' },
        { ok: true, documentId: 'doc-2' },
      ]);
      const { container, getByLabelText } = renderWithChatRuntime(
        <ChatInput
          {...baseProps}
          value="hi"
          onSubmit={onSubmit}
          onAttach={onAttach}
        />
      );
      await act(async () => {
        attachFiles(container, ['a.pdf', 'b.pdf']);
      });

      fireEvent.click(getByLabelText('Remove a.pdf'));
      fireEvent.submit(container.querySelector('form') as HTMLFormElement);

      expect(onSubmit).toHaveBeenCalledWith(['doc-2']);
    });

    it('blocks until a failed retained chip is removed', async () => {
      const onSubmit = vi.fn();
      const onAttach = vi.fn(async () => [
        { ok: true, documentId: 'doc-1' },
        { ok: false },
      ]);
      const { container } = renderWithChatRuntime(
        <ChatInput
          {...baseProps}
          value="hi"
          onSubmit={onSubmit}
          onAttach={onAttach}
        />
      );
      await act(async () => {
        attachFiles(container, ['a.pdf', 'b.pdf']);
      });

      fireEvent.submit(container.querySelector('form') as HTMLFormElement);

      expect(onSubmit).not.toHaveBeenCalled();
      fireEvent.click(screen.getByLabelText('Remove b.pdf'));
      fireEvent.submit(container.querySelector('form') as HTMLFormElement);
      expect(onSubmit).toHaveBeenCalledWith(['doc-1']);
    });

    it('sends an empty list when a host reports no outcomes', () => {
      // Hosts that return nothing leave the chips optimistic; they are a
      // visual record only and must not fabricate ids.
      const onSubmit = vi.fn();
      const { container } = renderWithChatRuntime(
        <ChatInput {...baseProps} value="hi" onSubmit={onSubmit} />
      );
      attachFiles(container, ['a.pdf']);

      fireEvent.submit(container.querySelector('form') as HTMLFormElement);

      expect(onSubmit).toHaveBeenCalledWith([]);
    });
  });

  describe('upload state on the chips', () => {
    function attachOne(container: HTMLElement, name = 'paper.pdf'): void {
      const input = container.querySelector(
        'input[type="file"]'
      ) as HTMLInputElement;
      Object.defineProperty(input, 'files', {
        value: [
          new File(['x'], name, { type: 'application/pdf' }),
        ] as unknown as FileList,
        configurable: true,
      });
      fireEvent.change(input);
    }

    it('shows an uploading chip until the host reports the outcome', async () => {
      let settle: (r: { ok: boolean }[]) => void = () => {};
      const onAttach = vi.fn(
        () =>
          new Promise<{ ok: boolean }[]>((resolve) => {
            settle = resolve;
          })
      );
      const { container } = renderWithChatRuntime(
        <ChatInput {...baseProps} onAttach={onAttach} />
      );
      attachOne(container);

      expect(
        container.querySelector('[aria-label="Uploading paper.pdf"]')
      ).not.toBeNull();

      await act(async () => {
        settle([{ ok: true }]);
      });

      expect(
        container.querySelector('[aria-label="Uploading paper.pdf"]')
      ).toBeNull();
      expect(
        container.querySelector('[aria-label="Upload failed for paper.pdf"]')
      ).toBeNull();
    });

    it('blocks click and Enter while an upload is pending and keeps the draft and chip', async () => {
      let settle: (
        r: { ok: boolean; documentId?: string }[]
      ) => void = () => {};
      const onSubmit = vi.fn();
      const onChange = vi.fn();
      const onAttach = vi.fn(
        () =>
          new Promise<{ ok: boolean; documentId?: string }[]>((resolve) => {
            settle = resolve;
          })
      );
      const { container } = renderWithChatRuntime(
        <ChatInput
          {...baseProps}
          value="pending draft"
          onChange={onChange}
          onSubmit={onSubmit}
          onAttach={onAttach}
        />
      );
      attachOne(container);

      const form = container.querySelector('form') as HTMLFormElement;
      fireEvent.click(screen.getByRole('button', { name: 'Send' }));
      fireEvent.keyDown(
        container.querySelector('textarea') as HTMLTextAreaElement,
        {
          key: 'Enter',
          code: 'Enter',
        }
      );
      fireEvent.submit(form);

      expect(onSubmit).not.toHaveBeenCalled();
      expect(onChange).not.toHaveBeenCalledWith('');
      expect(
        container.querySelector('[aria-label="Uploading paper.pdf"]')
      ).not.toBeNull();

      await act(async () => {
        settle([{ ok: true, documentId: 'doc-pending' }]);
      });
      fireEvent.click(screen.getByRole('button', { name: 'Send' }));

      expect(onSubmit).toHaveBeenCalledWith(['doc-pending']);
    });

    it('blocks Queue for a mixed batch until every retained upload settles', async () => {
      let settle: (
        r: { ok: boolean; documentId?: string }[]
      ) => void = () => {};
      const onSubmit = vi.fn();
      const onAttach = vi
        .fn()
        .mockResolvedValueOnce([{ ok: true, documentId: 'doc-done' }])
        .mockImplementationOnce(
          () =>
            new Promise<{ ok: boolean; documentId?: string }[]>((resolve) => {
              settle = resolve;
            })
        );
      const { container } = renderWithChatRuntime(
        <ChatInput
          {...baseProps}
          value="queued draft"
          isLoading
          onSubmit={onSubmit}
          onAttach={onAttach}
        />
      );
      await act(async () => {
        attachOne(container, 'done.pdf');
      });
      attachOne(container, 'pending.pdf');

      fireEvent.click(screen.getByRole('button', { name: 'Queue' }));
      fireEvent.keyDown(
        container.querySelector('textarea') as HTMLTextAreaElement,
        {
          key: 'Enter',
          code: 'Enter',
        }
      );
      fireEvent.submit(container.querySelector('form') as HTMLFormElement);
      expect(onSubmit).not.toHaveBeenCalled();
      expect(
        container.querySelector('[aria-label="Uploading pending.pdf"]')
      ).not.toBeNull();

      await act(async () => {
        settle([{ ok: true, documentId: 'doc-pending' }]);
      });
      fireEvent.submit(container.querySelector('form') as HTMLFormElement);
      expect(onSubmit).toHaveBeenCalledWith(['doc-done', 'doc-pending']);
    });

    it('marks the chip failed when the host reports a failed upload', async () => {
      const onAttach = vi.fn(async () => [{ ok: false }]);
      const { container } = renderWithChatRuntime(
        <ChatInput {...baseProps} onAttach={onAttach} />
      );

      await act(async () => {
        attachOne(container);
      });

      expect(
        container.querySelector('[aria-label="Upload failed for paper.pdf"]')
      ).not.toBeNull();
    });

    it('marks the chip failed when the host rejects', async () => {
      const onAttach = vi.fn(() => Promise.reject(new Error('network down')));
      const { container } = renderWithChatRuntime(
        <ChatInput {...baseProps} onAttach={onAttach} />
      );

      await act(async () => {
        attachOne(container);
      });

      expect(
        container.querySelector('[aria-label="Upload failed for paper.pdf"]')
      ).not.toBeNull();
      expect(
        container.querySelector('[aria-label="Uploading paper.pdf"]')
      ).toBeNull();
    });

    it('settles each pick of the same file independently', async () => {
      // The file input is reset after every selection, so the identical file
      // can be attached twice; the two chips must not share an identity.
      let settleSecond: (r: { ok: boolean }[]) => void = () => {};
      const onAttach = vi
        .fn()
        .mockResolvedValueOnce([{ ok: false }])
        .mockImplementationOnce(
          () =>
            new Promise<{ ok: boolean }[]>((resolve) => {
              settleSecond = resolve;
            })
        );

      const { container } = renderWithChatRuntime(
        <ChatInput {...baseProps} onAttach={onAttach} />
      );

      await act(async () => {
        attachOne(container);
      });
      await act(async () => {
        attachOne(container);
      });

      // First pick failed; second is still in flight — one of each, not two
      // chips sharing whichever outcome landed last.
      expect(
        container.querySelectorAll('[aria-label="Upload failed for paper.pdf"]')
      ).toHaveLength(1);
      expect(
        container.querySelectorAll('[aria-label="Uploading paper.pdf"]')
      ).toHaveLength(1);

      await act(async () => {
        settleSecond([{ ok: true }]);
      });

      // The successful second pick must not have cleared the first failure.
      expect(
        container.querySelectorAll('[aria-label="Upload failed for paper.pdf"]')
      ).toHaveLength(1);
      expect(
        container.querySelectorAll('[aria-label="Uploading paper.pdf"]')
      ).toHaveLength(0);
    });

    it('leaves the chip settled when the host reports nothing back', async () => {
      const onAttach = vi.fn();
      const { container } = renderWithChatRuntime(
        <ChatInput {...baseProps} onAttach={onAttach} />
      );

      await act(async () => {
        attachOne(container);
      });

      // No promise to await, so the chip must not sit in the uploading state.
      expect(
        container.querySelector('[aria-label="Uploading paper.pdf"]')
      ).toBeNull();
      expect(
        container.querySelector('ul[aria-label="Attached files"]')
      ).not.toBeNull();
    });
  });
});

describe('ChatInput attach controls are keyboard reachable', () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it.each([
    ['Attach file', /attach file/i],
    ['Attach image', /attach image/i],
  ])('%s input is focusable and named', (_label, matcher) => {
    renderWithChatRuntime(<ChatInput {...baseProps} />);
    const input = screen.getByLabelText(matcher) as HTMLInputElement;
    expect(input.tagName).toBe('INPUT');
    expect(input).not.toHaveClass('hidden');
    input.focus();
    expect(document.activeElement).toBe(input);
  });
});
