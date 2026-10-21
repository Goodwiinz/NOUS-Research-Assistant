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

/** Why the interactive (pdf.js) preview did not open. */
type FailureReason =
  'stopped' | 'too-large' | 'too-many-pages' | 'timeout' | 'load-failed';
type Failure = { blob: Blob; reason: FailureReason; pageCount?: number };

class PdfPreviewLimit extends Error {
  constructor(
    readonly reason: 'too-large' | 'too-many-pages',
    readonly pageCount?: number
  ) {
    super(`PDF preview limit: ${reason}`);
  }
}

const FAILURE_COPY: Record<Exclude<FailureReason, 'stopped'>, string> = {
  'too-large': 'This PDF is larger than 32 MB.',
  'too-many-pages': `This PDF has more than ${MAX_PAGES} pages.`,
  timeout: 'The PDF took too long to prepare.',
  'load-failed': 'The PDF could not be prepared for interactive viewing.',
};

/** A data-only document surface; no scripting/annotation actions are installed. */
export function PdfDocumentViewer({
  blob,
  title,
  targetPage,
  onAsk,
  documentId,
  fallbackUrl,
}: {
  blob: Blob;
  title: string;
  targetPage?: number;
  onAsk?: PdfAskHandler;
  /** Identifies the document in failure reports. */
  documentId?: string;
  /** Object URL of `blob`. When the interactive preview cannot open, the
   * PDF falls back to the browser's read-only viewer at this URL. */
  fallbackUrl?: string;
}): ReactElement {
  const [ready, setReady] = useState<Ready | null>(null);
  const [failure, setFailure] = useState<Failure | null>(null);
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
    const fail = (reason: FailureReason, cause?: unknown): void => {
      if (!active) return;
      active = false;
      clearTimeout(timeout);
      dispose();
      const pageCount =
        cause instanceof PdfPreviewLimit ? cause.pageCount : undefined;
      if (reason !== 'stopped')
        console.warn('[PdfDocumentViewer] interactive PDF preview failed', {
          documentId,
          reason,
          ...(pageCount !== undefined ? { pageCount } : {}),
          error: cause instanceof Error ? cause.message : cause,
        });
      setFailure({ blob, reason, pageCount });
    };
    stop.current = () => fail('stopped');
    const timeout = setTimeout(() => fail('timeout'), 30_000);
    const load = async (): Promise<void> => {
      if (blob.size > MAX_BYTES) throw new PdfPreviewLimit('too-large');
      if (!blob.size) throw new Error('Empty PDF');
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
      worker.onerror = (event) => fail('load-failed', event.message);
      pdfWorker = runtime.PDFWorker.create({ port: worker, verbosity: 0 });
      loading = runtime.getDocument(
        safePdfOptions(new Uint8Array(bytes), pdfWorker)
      );
      const document = await loading.promise;
      if (!active) return;
      if (!Number.isSafeInteger(document.numPages) || document.numPages < 1)
        throw new Error('Invalid PDF page count');
      if (document.numPages > MAX_PAGES) {
        // fail() disposes the loading task, which destroys this document.
        throw new PdfPreviewLimit('too-many-pages', document.numPages);
      }
      clearTimeout(timeout);
      setReady({ blob, document, runtime });
    };
    void load().catch((cause: unknown) =>
      fail(
        cause instanceof PdfPreviewLimit ? cause.reason : 'load-failed',
        cause
      )
    );
    return () => {
      active = false;
      clearTimeout(timeout);
      dispose();
    };
  }, [blob, documentId]);

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

  // Over the limits (or when pdf.js fails) the PDF still previews, read-only,
  // in the browser's own viewer: the pre-pdf.js behavior, page anchor included.
  const fallback =
    error && error.reason !== 'stopped' && fallbackUrl
      ? { reason: error.reason, src: `${fallbackUrl}#page=${page}` }
      : null;

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
          pageCount={current?.document.numPages ?? error?.pageCount}
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
            : fallback
              ? 'Basic PDF preview'
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
      {fallback && (
        <p role="status" className="px-4 py-2 text-xs text-muted-foreground">
          {FAILURE_COPY[fallback.reason]} It is shown in your browser&apos;s PDF
          viewer instead; text selected there can&apos;t be quoted into chat.
          {onAsk && ' You can still ask about a page range.'}
        </p>
      )}
      {error && !fallback && (
        <p role="alert" className="p-4 text-sm">
          {error.reason === 'stopped'
            ? 'PDF preview stopped.'
            : `PDF preview is unavailable. ${FAILURE_COPY[error.reason]}`}{' '}
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
      {fallback ? (
        <iframe
          key={fallback.src}
          src={fallback.src}
          title={`Preview of ${title}`}
          className="h-[70vh] min-h-[400px] w-full bg-muted/20"
        />
      ) : (
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
      )}
    </div>
  );
}
