'use client';

import { useEffect, useRef, useState, type ReactElement } from 'react';
import type { PDFDocumentLoadingTask, PDFWorker, RenderTask } from 'pdfjs-dist';

export const MAX_PDF_PREVIEW_BYTES = 2 * 1024 * 1024;
const MAX_PAGES = 100;
const MAX_PIXELS = 2_000_000;
const TIMEOUT_MS = 30_000;

/** No PDF-selected font, CMap or decoder URL may reach the network. */
class NoExternalPdfData {
  async fetch(): Promise<never> {
    throw new Error('External PDF resources are disabled');
  }
}

type CanvasEntry = {
  canvas: HTMLCanvasElement;
  context: CanvasRenderingContext2D;
};

/** The library's temporary image/pattern canvases share one allocation budget. */
class BoundedPdfCanvasFactory {
  private entries = new Set<HTMLCanvasElement>();

  private check(
    width: number,
    height: number,
    replacing?: HTMLCanvasElement
  ): void {
    const pixels = width * height;
    let total = pixels;
    for (const canvas of this.entries) {
      if (canvas !== replacing) total += canvas.width * canvas.height;
    }
    if (
      !Number.isInteger(width) ||
      !Number.isInteger(height) ||
      width < 1 ||
      height < 1 ||
      pixels > MAX_PIXELS ||
      total > MAX_PIXELS * 4 ||
      (!replacing && this.entries.size >= 16)
    )
      throw new Error('PDF canvas limit');
  }

  create(width: number, height: number): CanvasEntry {
    this.check(width, height);
    const canvas = document.createElement('canvas');
    canvas.width = width;
    canvas.height = height;
    const context = canvas.getContext('2d');
    if (!context) throw new Error('Canvas is unavailable');
    this.entries.add(canvas);
    return { canvas, context };
  }

  reset(entry: CanvasEntry, width: number, height: number): void {
    this.check(width, height, entry.canvas);
    entry.canvas.width = width;
    entry.canvas.height = height;
  }

  destroy(entry: CanvasEntry): void {
    this.entries.delete(entry.canvas);
    entry.canvas.width = entry.canvas.height = 0;
  }
}

type Result = {
  blob: Blob;
  page: number;
  status: 'ready' | 'error' | 'stopped';
  pages: number;
};

/**
 * Authenticated bytes only. Canvas drawing deliberately omits PDF viewers,
 * scripting/action managers, text/annotation layers, links, forms and XFA.
 * Each page owns a worker/task; teardown, timeout and navigation destroy it.
 */
export function PdfArtifactPreview({
  blob,
  title,
}: {
  blob: Blob;
  title: string;
}): ReactElement {
  const canvas = useRef<HTMLCanvasElement>(null);
  const cancel = useRef<() => void>(() => {});
  const [selection, setSelection] = useState({ blob, page: 1 });
  const page = selection.blob === blob ? selection.page : 1;
  const [result, setResult] = useState<Result | null>(null);
  const current = result?.blob === blob && result.page === page ? result : null;
  const ready = current?.status === 'ready';

  useEffect(() => {
    let active = true;
    let worker: Worker | undefined;
    let pdfWorker: PDFWorker | undefined;
    let loading: PDFDocumentLoadingTask | undefined;
    let rendering: RenderTask | undefined;
    let reader: FileReader | undefined;
    const target = canvas.current;
    const dispose = (): void => {
      reader?.abort();
      rendering?.cancel();
      void loading?.destroy().catch(() => {});
      pdfWorker?.destroy();
      worker?.terminate();
    };
    const finish = (status: Result['status'], pages = 0): void => {
      if (!active) return;
      active = false;
      clearTimeout(timeout);
      dispose();
      setResult({ blob, page, status, pages });
    };
    const timeout = setTimeout(() => finish('error'), TIMEOUT_MS);
    cancel.current = () => finish('stopped');

    const draw = async (): Promise<void> => {
      if (!target || blob.size === 0 || blob.size > MAX_PDF_PREVIEW_BYTES) {
        finish('error');
        return;
      }
      const bytes = await new Promise<ArrayBuffer>((resolve, reject) => {
        reader = new FileReader();
        reader.onload = () => {
          if (reader?.result instanceof ArrayBuffer) resolve(reader.result);
          else reject(new Error('Invalid PDF bytes'));
        };
        reader.onerror = reader.onabort = () =>
          reject(new Error('Read stopped'));
        reader.readAsArrayBuffer(blob);
      });
      if (!active) return;
      const pdf = await import('pdfjs-dist');
      if (!active) return;
      // A dedicated module Worker prevents pdf.js's main-thread fake-worker
      // fallback. Next bundles this exact pinned worker as a same-origin asset.
      worker = new Worker(
        new URL('pdfjs-dist/build/pdf.worker.min.mjs', import.meta.url),
        { type: 'module' }
      );
      worker.onerror = () => finish('error');
      pdfWorker = pdf.PDFWorker.create({ port: worker, verbosity: 0 });
      const options = {
        data: new Uint8Array(bytes),
        worker: pdfWorker,
        // Kept explicit for older API compatibility; pdf.js 6 removed eval.
        isEvalSupported: false,
        enableXfa: false,
        disableFontFace: true,
        useSystemFonts: true,
        useWorkerFetch: false,
        useWasm: false,
        BinaryDataFactory: NoExternalPdfData,
        CanvasFactory: BoundedPdfCanvasFactory,
        disableAutoFetch: true,
        disableStream: true,
        disableRange: true,
        isOffscreenCanvasSupported: false,
        isImageDecoderSupported: false,
        maxImageSize: MAX_PIXELS,
        canvasMaxAreaInBytes: MAX_PIXELS * 4,
        stopAtErrors: true,
        verbosity: 0,
      };
      loading = pdf.getDocument(options);
      const document = await loading.promise;
      if (!active) return;
      if (
        !Number.isInteger(document.numPages) ||
        document.numPages < 1 ||
        document.numPages > MAX_PAGES ||
        page > document.numPages
      ) {
        finish('error');
        return;
      }
      const source = await document.getPage(page);
      if (!active) return;
      const original = source.getViewport({ scale: 1 });
      if (
        !Number.isFinite(original.width) ||
        !Number.isFinite(original.height) ||
        original.width <= 0 ||
        original.height <= 0
      ) {
        finish('error');
        return;
      }
      const scale = Math.min(
        1.5,
        1200 / original.width,
        1600 / original.height,
        Math.sqrt(MAX_PIXELS / original.width / original.height)
      );
      const viewport = source.getViewport({ scale });
      target.width = Math.max(1, Math.floor(viewport.width));
      target.height = Math.max(1, Math.floor(viewport.height));
      rendering = source.render({
        canvas: target,
        viewport,
        annotationMode: pdf.AnnotationMode.DISABLE,
        background: '#ffffff',
      });
      await rendering.promise;
      finish('ready', document.numPages);
    };
    void draw().catch(() => finish('error'));
    return () => {
      active = false;
      clearTimeout(timeout);
      dispose();
      // Drop old account/page pixels as well as the worker's document bytes.
      if (target) target.width = target.height = 0;
    };
  }, [blob, page]);

  return (
    <section aria-label={`PDF preview: ${title}`} className="p-4">
      {!current && (
        <div role="status">
          Preparing PDF preview…{' '}
          <button type="button" onClick={() => cancel.current()}>
            Stop preview
          </button>
        </div>
      )}
      {current && !ready && (
        <p role="alert">
          {current.status === 'stopped'
            ? 'PDF preview stopped.'
            : 'PDF preview is unavailable.'}{' '}
          Download this file to view it.
        </p>
      )}
      <canvas
        ref={canvas}
        title="PDF preview"
        role="img"
        aria-label={`${title}, page ${page}`}
        hidden={!ready}
        className="h-auto max-w-full"
      />
      {ready && (
        <nav aria-label="PDF pages" className="mt-3 flex items-center gap-3">
          <button
            type="button"
            disabled={page === 1}
            onClick={() => setSelection({ blob, page: page - 1 })}
          >
            Previous page
          </button>
          <span aria-live="polite">
            Page {page} of {current.pages}
          </span>
          <button
            type="button"
            disabled={page === current.pages}
            onClick={() => setSelection({ blob, page: page + 1 })}
          >
            Next page
          </button>
        </nav>
      )}
    </section>
  );
}
