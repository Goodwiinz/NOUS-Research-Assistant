import {
  act,
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
  within,
} from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { PdfDocumentViewer } from '../PdfDocumentViewer';

const driver = vi.hoisted(() => ({
  pages: 3,
  loading: null as Promise<unknown> | null,
  textRendering: null as Promise<void> | null,
  renderOptions: [] as Record<string, unknown>[],
  loads: [] as Record<string, unknown>[],
  cancel: vi.fn(),
  textCancel: vi.fn(),
  destroy: vi.fn(async () => {}),
  terminate: vi.fn(),
}));
vi.mock('pdfjs-dist', () => ({
  AnnotationMode: { DISABLE: 0 },
  PDFWorker: { create: () => ({ destroy: vi.fn() }) },
  getDocument: (options: Record<string, unknown>) => {
    driver.loads.push(options);
    return {
      destroy: driver.destroy,
      promise:
        driver.loading ??
        Promise.resolve({
          numPages: driver.pages,
          getPage: async (page: number) => ({
            getViewport: ({ scale }: { scale: number }) => ({
              width: 600 * scale,
              height: 800 * scale,
            }),
            getTextContent: async () => ({
              items: [{ str: `Selectable page ${page} text` }],
              styles: {},
            }),
            render: (options: Record<string, unknown>) => {
              driver.renderOptions.push(options);
              return { promise: Promise.resolve(), cancel: driver.cancel };
            },
          }),
        }),
    };
  },
  TextLayer: class {
    constructor(
      private options: {
        container: HTMLElement;
        textContentSource: { items: { str: string }[] };
      }
    ) {}
    async render(): Promise<void> {
      const span = document.createElement('span');
      span.textContent = this.options.textContentSource.items[0].str;
      this.options.container.append(span);
      await driver.textRendering;
    }
    cancel = driver.textCancel;
  },
}));

const file = (): Blob => new Blob(['%PDF-1.7'], { type: 'application/pdf' });
function selectText(first: number, last: number): void {
  const anchor = screen.getByText(`Selectable page ${first} text`).firstChild!;
  const focus = screen.getByText(`Selectable page ${last} text`).firstChild!;
  window.getSelection()?.setBaseAndExtent(anchor, 2, focus, 8);
  fireEvent.pointerUp(screen.getByRole('region', { name: /PDF preview/ }));
}

beforeEach(() => {
  driver.pages = 3;
  driver.loading = null;
  driver.textRendering = null;
  driver.loads.length = 0;
  driver.renderOptions.length = 0;
  vi.stubGlobal(
    'Worker',
    class {
      terminate = driver.terminate;
    }
  );
  vi.stubGlobal(
    'IntersectionObserver',
    class {
      constructor(
        private callback: (entries: { isIntersecting: boolean }[]) => void
      ) {}
      observe(): void {
        this.callback([{ isIntersecting: true }]);
      }
      disconnect(): void {}
    }
  );
  vi.stubGlobal(
    'ResizeObserver',
    class {
      observe(): void {}
      disconnect(): void {}
    }
  );
  HTMLElement.prototype.scrollTo = vi.fn();
});
afterEach(() => {
  cleanup();
  window.getSelection()?.removeAllRanges();
  vi.useRealTimers();
  vi.unstubAllGlobals();
});

describe('controlled PDF text selection', () => {
  it('sends quote provenance without restricting retrieval for backward multi-page and one-page selections', async () => {
    const ask = vi.fn();
    render(<PdfDocumentViewer blob={file()} title="paper.pdf" onAsk={ask} />);
    await screen.findByText('Selectable page 3 text');
    selectText(3, 1);
    expect(screen.getByLabelText('First page')).toHaveValue(null);
    expect(screen.getByLabelText('Last page')).toHaveValue(null);
    await act(async () => {
      fireEvent.click(screen.getByRole('button', { name: 'Ask about PDF' }));
    });
    expect(ask).toHaveBeenLastCalledWith(
      undefined,
      '> le page 1 text\n> Selectable page 2 text\n> Se',
      [0, 2]
    );
    expect(screen.queryByLabelText('Selected PDF passage')).toBeNull();
    expect(screen.getByLabelText('First page')).toHaveValue(null);
    expect(window.getSelection()?.isCollapsed).toBe(true);
    selectText(2, 2);
    fireEvent.keyUp(screen.getByRole('region', { name: /PDF preview/ }), {
      key: 'ArrowRight',
      shiftKey: true,
    });
    await act(async () => {
      fireEvent.click(screen.getByRole('button', { name: 'Ask about PDF' }));
    });
    expect(ask).toHaveBeenLastCalledWith(undefined, '> lectab', [1, 1]);
    selectText(2, 2);
    fireEvent.click(screen.getByRole('button', { name: 'Clear quote' }));
    await act(async () => {
      fireEvent.click(screen.getByRole('button', { name: 'Ask about PDF' }));
    });
    expect(ask).toHaveBeenLastCalledWith(undefined);
    expect(document.querySelector('iframe,object,embed')).toBeNull();
    expect(
      driver.renderOptions.every((options) => options.annotationMode === 0)
    ).toBe(true);
    expect(driver.loads[0]).toMatchObject({
      enableXfa: false,
      useWorkerFetch: false,
      useWasm: false,
      isEvalSupported: false,
    });
  });

  it('retains the exact selection after setup rejection and clears it only when retry succeeds', async () => {
    const ask = vi
      .fn()
      .mockRejectedValueOnce(new Error('offline'))
      .mockResolvedValue(true);
    render(<PdfDocumentViewer blob={file()} title="paper.pdf" onAsk={ask} />);
    await screen.findByText('Selectable page 2 text');
    selectText(2, 2);
    await act(async () => {
      fireEvent.click(screen.getByRole('button', { name: 'Ask about PDF' }));
    });
    expect(screen.getByRole('alert')).toHaveTextContent(
      'Could not open PDF chat. Try again.'
    );
    expect(screen.getByLabelText('Selected PDF passage')).toHaveTextContent(
      '> lectab'
    );
    expect(screen.getByLabelText('Selected PDF pages')).toHaveTextContent(
      '2–2'
    );
    await act(async () => {
      fireEvent.click(screen.getByRole('button', { name: 'Ask about PDF' }));
    });
    expect(ask).toHaveBeenCalledTimes(2);
    expect(screen.queryByLabelText('Selected PDF passage')).toBeNull();
    expect(screen.queryByRole('alert')).toBeNull();
  });

  // Mutation verified: remove PdfDocumentViewer.tsx:188's captured identity check.
  // pnpm --dir frontend test src/components/documents/__tests__/PdfDocumentViewer.test.tsx -t 'preserves a newer text selection'
  it('preserves a newer text selection when an older asynchronous handoff succeeds', async () => {
    let resolve!: (value: boolean) => void;
    const ask = vi.fn(
      () =>
        new Promise<boolean>((done) => {
          resolve = done;
        })
    );
    render(<PdfDocumentViewer blob={file()} title="paper.pdf" onAsk={ask} />);
    await screen.findByText('Selectable page 3 text');
    selectText(2, 2);
    fireEvent.click(screen.getByRole('button', { name: 'Ask about PDF' }));
    expect(
      screen.getByRole('button', { name: 'Opening PDF chat…' })
    ).toBeDisabled();
    selectText(3, 3);
    await act(async () => {
      resolve(true);
    });
    expect(screen.getByLabelText('Selected PDF passage')).toHaveTextContent(
      '> lectab'
    );
    expect(screen.getByLabelText('Selected PDF pages')).toHaveTextContent(
      '3–3'
    );
    expect(window.getSelection()?.isCollapsed).toBe(false);
  });

  // Mutation verified: remove PdfDocumentViewer.tsx:175's pending guard.
  // pnpm --dir frontend test src/components/documents/__tests__/PdfDocumentViewer.test.tsx -t 'fences duplicate Ask'
  it('fences duplicate Ask events before the pending button rerenders', async () => {
    let resolve!: (value: boolean) => void;
    const ask = vi.fn(
      () =>
        new Promise<boolean>((done) => {
          resolve = done;
        })
    );
    render(<PdfDocumentViewer blob={file()} title="paper.pdf" onAsk={ask} />);
    await screen.findByText('Selectable page 2 text');
    selectText(2, 2);
    const button = screen.getByRole('button', { name: 'Ask about PDF' });
    act(() => {
      fireEvent.click(button);
      fireEvent.click(button);
    });
    expect(ask).toHaveBeenCalledOnce();
    await act(async () => {
      resolve(true);
    });
  });

  it('does not let a pending old-document handoff clear the replacement viewer selection', async () => {
    let resolve!: (value: boolean) => void;
    const ask = vi.fn(
      () =>
        new Promise<boolean>((done) => {
          resolve = done;
        })
    );
    const { rerender } = render(
      <PdfDocumentViewer blob={file()} title="old.pdf" onAsk={ask} />
    );
    await screen.findByText('Selectable page 2 text');
    selectText(2, 2);
    fireEvent.click(screen.getByRole('button', { name: 'Ask about PDF' }));
    rerender(<PdfDocumentViewer blob={file()} title="new.pdf" onAsk={ask} />);
    await screen.findByText('Selectable page 3 text');
    selectText(3, 3);
    await act(async () => {
      resolve(true);
    });
    expect(screen.getByLabelText('Selected PDF pages')).toHaveTextContent(
      '3–3'
    );
    expect(screen.getByLabelText('Selected PDF passage')).toHaveTextContent(
      '> lectab'
    );
  });

  // Mutation verified: remove PdfDocumentViewer.tsx:56/58's owner resets,
  // :195's catch ownership check, or :197's finally ownership check.
  // pnpm --dir frontend test src/components/documents/__tests__/PdfDocumentViewer.test.tsx -t 'new document pending'
  it.each(['resolve', 'reject'])(
    'keeps the new document pending when an old handoff settles by %s',
    async (outcome) => {
      let oldResolve!: (accepted: boolean) => void;
      let oldReject!: (reason: Error) => void;
      let newResolve!: (accepted: boolean) => void;
      const ask = vi
        .fn()
        .mockImplementationOnce(
          () =>
            new Promise<boolean>((resolve, reject) => {
              oldResolve = resolve;
              oldReject = reject;
            })
        )
        .mockImplementationOnce(
          () =>
            new Promise<boolean>((resolve) => {
              newResolve = resolve;
            })
        );
      const { rerender } = render(
        <PdfDocumentViewer blob={file()} title="old.pdf" onAsk={ask} />
      );
      await screen.findByText('Selectable page 2 text');
      selectText(2, 2);
      fireEvent.click(screen.getByRole('button', { name: 'Ask about PDF' }));
      rerender(<PdfDocumentViewer blob={file()} title="new.pdf" onAsk={ask} />);
      await screen.findByText('Selectable page 3 text');
      selectText(3, 3);
      fireEvent.click(screen.getByRole('button', { name: 'Ask about PDF' }));
      expect(ask).toHaveBeenCalledTimes(2);
      await act(async () => {
        if (outcome === 'resolve') oldResolve(true);
        else oldReject(new Error('Old setup failed'));
      });
      expect(
        screen.getByRole('button', { name: 'Opening PDF chat…' })
      ).toBeDisabled();
      expect(screen.queryByRole('alert')).toBeNull();
      expect(screen.getByLabelText('Selected PDF pages')).toHaveTextContent(
        '3–3'
      );
      await act(async () => {
        newResolve(true);
      });
      expect(
        screen.getByRole('button', { name: 'Ask about PDF' })
      ).toBeEnabled();
      expect(screen.queryByLabelText('Selected PDF passage')).toBeNull();
    }
  );

  it('never derives a page range from quoted text or a selection crossing another viewer', async () => {
    const ask = vi.fn();
    render(
      <>
        <p>Quoted page 2</p>
        <PdfDocumentViewer blob={file()} title="paper.pdf" onAsk={ask} />
      </>
    );
    await screen.findByText('Selectable page 1 text');
    const quote = screen.getByText('Quoted page 2').firstChild!;
    window.getSelection()?.setBaseAndExtent(quote, 0, quote, 6);
    fireEvent.pointerUp(screen.getByRole('region', { name: /PDF preview/ }));
    await act(async () => {
      fireEvent.click(screen.getByRole('button', { name: 'Ask about PDF' }));
    });
    expect(ask).toHaveBeenLastCalledWith(undefined);
    window
      .getSelection()
      ?.setBaseAndExtent(
        quote,
        0,
        screen.getByText('Selectable page 2 text').firstChild!,
        8
      );
    fireEvent.pointerUp(screen.getByRole('region', { name: /PDF preview/ }));
    expect(screen.getByLabelText('First page')).toHaveValue(null);
  });

  it('scrolls to cited physical pages and does not turn navigation into selection', async () => {
    vi.spyOn(HTMLElement.prototype, 'getBoundingClientRect').mockImplementation(
      function (this: HTMLElement) {
        return new DOMRect(
          0,
          (Number(this.dataset.pdfPage || 1) - 1) * 800,
          600,
          800
        );
      }
    );
    const blob = file();
    const props = { blob, title: 'paper.pdf', onAsk: vi.fn() };
    const { rerender } = render(
      <PdfDocumentViewer {...props} targetPage={3} />
    );
    await screen.findByText('Selectable page 3 text');
    expect(HTMLElement.prototype.scrollTo).toHaveBeenLastCalledWith({
      top: 1600,
    });
    expect(
      vi.mocked(HTMLElement.prototype.scrollTo).mock.instances.at(-1)
    ).toBe(screen.getByRole('region', { name: 'PDF preview: paper.pdf' }));
    expect(screen.getByLabelText('First page')).toHaveValue(null);
    rerender(<PdfDocumentViewer {...props} targetPage={1} />);
    expect(HTMLElement.prototype.scrollTo).toHaveBeenLastCalledWith({ top: 0 });
  });

  it('resets captured/manual intent and destroys prior resources when document bytes change', async () => {
    const { rerender, unmount } = render(
      <PdfDocumentViewer blob={file()} title="old.pdf" onAsk={vi.fn()} />
    );
    await screen.findByText('Selectable page 2 text');
    selectText(1, 2);
    rerender(
      <PdfDocumentViewer blob={file()} title="new.pdf" onAsk={vi.fn()} />
    );
    expect(screen.getByLabelText('First page')).toHaveValue(null);
    await screen.findByText('Selectable page 2 text');
    unmount();
    expect(driver.cancel).toHaveBeenCalled();
    expect(driver.textCancel).toHaveBeenCalled();
    expect(driver.destroy).toHaveBeenCalled();
    expect(driver.terminate).toHaveBeenCalled();
  });

  it('stop prevents a pending load from installing pages after cancellation', async () => {
    let complete!: (value: unknown) => void;
    driver.loading = new Promise((resolve) => {
      complete = resolve;
    });
    render(
      <PdfDocumentViewer blob={file()} title="stopped.pdf" onAsk={vi.fn()} />
    );
    await waitFor(() => expect(driver.loads).toHaveLength(1));
    fireEvent.click(screen.getByRole('button', { name: 'Stop preview' }));
    await act(async () => complete({ numPages: 3, getPage: vi.fn() }));
    expect(screen.getByRole('alert')).toHaveTextContent('PDF preview stopped');
    expect(
      within(screen.getByRole('region')).queryByLabelText('Page 1')
    ).toBeNull();
    expect(driver.terminate).toHaveBeenCalled();
  });

  // Mutation verified: remove PdfDocumentViewer.tsx:99's active check after
  // loading.promise. The old result replaces the new ready document.
  // pnpm --dir frontend test src/components/documents/__tests__/PdfDocumentViewer.test.tsx -t 'late old PDF'
  it('a late old PDF load cannot replace the ready document after a byte switch', async () => {
    let complete!: (value: unknown) => void;
    driver.loading = new Promise((resolve) => {
      complete = resolve;
    });
    const { rerender } = render(
      <PdfDocumentViewer blob={file()} title="old.pdf" onAsk={vi.fn()} />
    );
    await waitFor(() => expect(driver.loads).toHaveLength(1));
    driver.loading = null;
    rerender(
      <PdfDocumentViewer blob={file()} title="new.pdf" onAsk={vi.fn()} />
    );
    await screen.findByText('Selectable page 3 text');
    await act(async () => complete({ numPages: 2, getPage: vi.fn() }));
    expect(screen.getByText('Selectable page 3 text')).toBeInTheDocument();
  });

  it('times out an unfinished worker load and keeps manual fallback usable', async () => {
    driver.loading = new Promise(() => {});
    const timers = vi.spyOn(globalThis, 'setTimeout');
    render(
      <PdfDocumentViewer blob={file()} title="slow.pdf" onAsk={vi.fn()} />
    );
    await waitFor(() => expect(driver.loads).toHaveLength(1));
    const timeout = timers.mock.calls.find((call) => call[1] === 30_000)?.[0];
    expect(typeof timeout).toBe('function');
    act(() => {
      if (typeof timeout === 'function') timeout();
    });
    expect(screen.getByRole('alert')).toHaveTextContent(
      'PDF preview is unavailable'
    );
    expect(driver.destroy).toHaveBeenCalled();
    expect(screen.getByRole('button', { name: 'Ask about PDF' })).toBeEnabled();
  });

  it('keeps manual page controls available when bounded rendering is unavailable', async () => {
    driver.pages = 101;
    const ask = vi.fn();
    render(<PdfDocumentViewer blob={file()} title="large.pdf" onAsk={ask} />);
    await screen.findByRole('alert');
    fireEvent.change(screen.getByLabelText('First page'), {
      target: { value: '100' },
    });
    fireEvent.change(screen.getByLabelText('Last page'), {
      target: { value: '101' },
    });
    await act(async () => {
      fireEvent.click(screen.getByRole('button', { name: 'Ask about PDF' }));
    });
    expect(ask).toHaveBeenCalledWith([99, 100]);
  });
});
