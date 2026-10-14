'use client';

import {
  useEffect,
  useLayoutEffect,
  useRef,
  useState,
  type ReactElement,
} from 'react';
import type {
  PDFDocumentLoadingTask,
  PDFDocumentProxy,
  PDFWorker,
} from 'pdfjs-dist';
import { safePdfOptions } from '@/lib/pdf/safe-renderer';
import { selectedPdfText } from '@/lib/pdf/selection';
import type { PdfAskHandler, PdfTextSelection } from '@/types/pdf-chat';
import { PdfChatControls } from './PdfChatControls';
import { PdfDocumentPage } from './PdfDocumentPage';

type PdfRuntime = typeof import('pdfjs-dist');
type Ready = { blob: Blob; document: PDFDocumentProxy; runtime: PdfRuntime };
const MAX_BYTES = 32 * 1024 * 1024;
const MAX_PAGES = 100;

/** A data-only document surface; no scripting/annotation actions are installed. */
export function PdfDocumentViewer({
  blob,
  title,
  targetPage,
  onAsk,
}: {
  blob: Blob;
  title: string;
  targetPage?: number;
  onAsk?: PdfAskHandler;
}): ReactElement {
  const [ready, setReady] = useState<Ready | null>(null);
  const [failure, setFailure] = useState<{
    blob: Blob;
    stopped: boolean;
  } | null>(null);
  const [selection, setSelection] = useState<{
    blob: Blob;
    text: PdfTextSelection;
  }>();
  const [identity, setIdentity] = useState({ blob, version: 0 });
  const selectionRef = useRef(selection);
  const askOwner = useRef<object | null>(null);
  const [asking, setAsking] = useState(false);
  const [askFailed, setAskFailed] = useState(false);
  useLayoutEffect(() => {
    selectionRef.current = selection;
  }, [selection]);
  useLayoutEffect(() => {
    askOwner.current = null;
    return () => {
      askOwner.current = null;
    };
  }, [blob]);
  if (identity.blob !== blob) {
    setIdentity({ blob, version: identity.version + 1 });
    setAsking(false);
    setAskFailed(false);
  }
  const [width, setWidth] = useState(600);
  const [root, setRoot] = useState<HTMLDivElement | null>(null);
  const stop = useRef<() => void>(() => {});
  const current = ready?.blob === blob ? ready : null;
  const error = failure?.blob === blob ? failure : null;
  const page =
    Number.isSafeInteger(targetPage) && (targetPage ?? 0) > 0 ? targetPage! : 1;

  useEffect(() => {
    let active = true;
    let worker: Worker | undefined;
    let pdfWorker: PDFWorker | undefined;
    let loading: PDFDocumentLoadingTask | undefined;
    let reader: FileReader | undefined;
    const dispose = (): void => {
      reader?.abort();
      void loading?.destroy().catch(() => {});
      pdfWorker?.destroy();
      worker?.terminate();
    };
    const fail = (stopped = false): void => {
      if (!active) return;
      active = false;
      clearTimeout(timeout);
      dispose();
      setFailure({ blob, stopped });
    };
    stop.current = () => fail(true);
    const timeout = setTimeout(() => fail(), 30_000);
    const load = async (): Promise<void> => {
      if (!blob.size || blob.size > MAX_BYTES)
        throw new Error('PDF byte limit');
      const bytes = await new Promise<ArrayBuffer>((resolve, reject) => {
        reader = new FileReader();
        reader.onload = () =>
          reader?.result instanceof ArrayBuffer
            ? resolve(reader.result)
            : reject(new Error('Invalid bytes'));
        reader.onerror = reader.onabort = () =>
          reject(new Error('Read stopped'));
        reader.readAsArrayBuffer(blob);
      });
      if (!active) return;
      const runtime = await import('pdfjs-dist');
      if (!active) return;
      worker = new Worker(
        new URL('pdfjs-dist/build/pdf.worker.min.mjs', import.meta.url),
        { type: 'module' }
      );
      worker.onerror = () => fail();
      pdfWorker = runtime.PDFWorker.create({ port: worker, verbosity: 0 });
      loading = runtime.getDocument(
        safePdfOptions(new Uint8Array(bytes), pdfWorker)
      );
      const document = await loading.promise;
      if (!active) return;
      if (
        !Number.isSafeInteger(document.numPages) ||
        document.numPages < 1 ||
        document.numPages > MAX_PAGES
      )
        throw new Error('PDF page limit');
      clearTimeout(timeout);
      setReady({ blob, document, runtime });
    };
    void load().catch(() => fail());
    return () => {
      active = false;
      clearTimeout(timeout);
      dispose();
    };
  }, [blob]);

  useEffect(() => {
    const container = root;
    if (!container) return;
    const observer = new ResizeObserver((entries) => {
      const value = entries[0]?.contentRect.width;
      if (value > 48) setWidth(Math.min(1200, Math.floor(value - 32)));
    });
    observer.observe(container);
    return () => observer.disconnect();
  }, [root]);

  useEffect(() => {
    if (!current || error || !root) return;
    const target = root.querySelector<HTMLElement>(`[data-pdf-page="${page}"]`);
    if (target)
      root.scrollTo({
        top:
          root.scrollTop +
          target.getBoundingClientRect().top -
          root.getBoundingClientRect().top,
      });
  }, [current, page, error, root]);

  const captureSelection = (): void => {
    if (!root) return;
    const text = selectedPdfText(root, window.getSelection());
    if (text) setSelection({ blob, text });
  };

  return (
    <div>
      {onAsk && (
        <PdfChatControls
          key={identity.version}
          pending={asking}
          onAsk={async (range, quote, quoteRange) => {
            if (askOwner.current) return;
            const owner = {};
            const captured = selectionRef.current;
            askOwner.current = owner;
            setAsking(true);
            setAskFailed(false);
            try {
              const accepted = quote
                ? await onAsk(range, quote, quoteRange)
                : await onAsk(range);
              if (askOwner.current !== owner || accepted === false) return;
              // Only this captured selection transferred. A new selection made
              // during workspace setup remains pending in this viewer.
              if (quote && selectionRef.current === captured) {
                selectionRef.current = undefined;
                setSelection(undefined);
                if (root && selectedPdfText(root, window.getSelection()))
                  window.getSelection()?.removeAllRanges();
              }
            } catch {
              if (askOwner.current === owner) setAskFailed(true);
            } finally {
              if (askOwner.current === owner) {
                askOwner.current = null;
                setAsking(false);
              }
            }
          }}
          textSelection={selection?.blob === blob ? selection.text : undefined}
          pageCount={current?.document.numPages}
        />
      )}
      {askFailed && (
        <p role="alert" className="px-4 py-2 text-sm">
          Could not open PDF chat. Try again.
        </p>
      )}
      <div className="flex items-center justify-between gap-2 border-b border-border px-4 py-2 text-xs">
        <span>
          {current && !error
            ? `${current.document.numPages} pages · Select text to quote`
            : 'PDF preview'}
        </span>
        {!error && (
          <button
            type="button"
            className="min-h-11 rounded px-2 focus-visible:ring-2 focus-visible:ring-ring"
            onClick={() => stop.current()}
          >
            Stop preview
          </button>
        )}
      </div>
      {error && (
        <p role="alert" className="p-4 text-sm">
          {error.stopped
            ? 'PDF preview stopped.'
            : 'PDF preview is unavailable.'}{' '}
          Use the page controls, open in a new tab, or download the PDF.
        </p>
      )}
      {!current && !error && (
        <p role="status" className="p-4 text-sm">
          Preparing PDF preview…
        </p>
      )}
      {current && page > current.document.numPages && (
        <p role="status" className="p-4 text-sm">
          The cited page is outside this PDF.
        </p>
      )}
      <div
        ref={setRoot}
        role="region"
        aria-label={`PDF preview: ${title}`}
        tabIndex={0}
        onPointerUp={captureSelection}
        onKeyUp={captureSelection}
        className="h-[70vh] min-h-[400px] overflow-auto bg-muted/20 p-4 focus-visible:outline-hidden focus-visible:ring-2 focus-visible:ring-ring"
      >
        {current &&
          !error &&
          Array.from({ length: current.document.numPages }, (_, index) => (
            <PdfDocumentPage
              key={index}
              document={current.document}
              runtime={current.runtime}
              page={index + 1}
              width={width}
              scrollRoot={root}
              initialPage={page}
            />
          ))}
      </div>
    </div>
  );
}
