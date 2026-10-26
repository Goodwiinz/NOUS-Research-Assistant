'use client';

import { useEffect, useRef, useState, type ReactElement } from 'react';
import type {
  PDFDocumentProxy,
  PDFPageProxy,
  RenderTask,
  TextLayer,
} from 'pdfjs-dist';
import { MAX_PIXELS } from '@/lib/pdf/safe-renderer';
import styles from './PdfDocumentPage.module.css';

type PdfRuntime = typeof import('pdfjs-dist');
type Viewport = ReturnType<PDFPageProxy['getViewport']>;

/** Keep visited text layers for continuous selection; release offscreen pixels. */
export function PdfDocumentPage({
  document: pdfDocument,
  runtime,
  page,
  width,
  scrollRoot,
  initialPage,
}: {
  document: PDFDocumentProxy;
  runtime: PdfRuntime;
  page: number;
  width: number;
  scrollRoot: HTMLDivElement | null;
  initialPage: number;
}): ReactElement {
  const frame = useRef<HTMLDivElement>(null);
  const textHost = useRef<HTMLDivElement>(null);
  const canvas = useRef<HTMLCanvasElement>(null);
  const [visible, setVisible] = useState(page === initialPage);
  const [visited, setVisited] = useState(page === initialPage);
  const [loaded, setLoaded] = useState<{
    width: number;
    source: PDFPageProxy;
    viewport: Viewport;
  } | null>(null);
  const [error, setError] = useState(false);
  const current = loaded?.width === width ? loaded : null;

  useEffect(() => {
    const target = frame.current;
    if (!target) return;
    const observer = new IntersectionObserver(
      (entries) => {
        for (const entry of entries) {
          setVisible(entry.isIntersecting);
          if (entry.isIntersecting) setVisited(true);
        }
      },
      { root: scrollRoot, rootMargin: '300px 0px' }
    );
    observer.observe(target);
    return () => observer.disconnect();
  }, [scrollRoot]);

  useEffect(() => {
    if (!visited) return;
    let active = true;
    let layer: TextLayer | undefined;
    const host = textHost.current;
    const timeout = setTimeout(() => {
      if (!active) return;
      active = false;
      layer?.cancel();
      setError(true);
    }, 30_000);
    const read = async (): Promise<void> => {
      const source = await pdfDocument.getPage(page);
      if (!active) return;
      const original = source.getViewport({ scale: 1 });
      if (
        ![original.width, original.height].every(
          (value) => Number.isFinite(value) && value > 0
        )
      )
        throw new Error('Invalid page dimensions');
      const scale = Math.min(
        1.5,
        width / original.width,
        1600 / original.height,
        Math.sqrt(MAX_PIXELS / original.width / original.height)
      );
      const viewport = source.getViewport({ scale });
      const content = await source.getTextContent();
      if (!active) return;
      if (
        content.items.length > 20_000 ||
        content.items.reduce(
          (count, item) => count + ('str' in item ? item.str.length : 0),
          0
        ) > 200_000
      )
        throw new Error('Page text limit');
      const container = window.document.createElement('div');
      container.className = styles.textLayer;
      container.dataset.pdfTextPage = String(page - 1);
      container.style.setProperty('--total-scale-factor', String(scale));
      container.style.setProperty('--scale-round-x', '1px');
      container.style.setProperty('--scale-round-y', '1px');
      layer = new runtime.TextLayer({
        textContentSource: content,
        container,
        viewport,
      });
      await layer.render();
      if (!active) return;
      host?.replaceChildren(container);
      setLoaded({ width, source, viewport });
      clearTimeout(timeout);
    };
    void read().catch(() => {
      if (active) setError(true);
      clearTimeout(timeout);
    });
    return () => {
      active = false;
      clearTimeout(timeout);
      layer?.cancel();
      host?.replaceChildren();
    };
  }, [pdfDocument, runtime, page, width, visited]);

  useEffect(() => {
    const target = canvas.current;
    if (!visible || !current || !target) return;
    let active = true;
    let rendering: RenderTask | undefined;
    const timeout = setTimeout(() => {
      if (!active) return;
      rendering?.cancel();
      setError(true);
    }, 30_000);
    target.width = Math.max(1, Math.floor(current.viewport.width));
    target.height = Math.max(1, Math.floor(current.viewport.height));
    const draw = async (): Promise<void> => {
      rendering = current.source.render({
        canvas: target,
        viewport: current.viewport,
        annotationMode: runtime.AnnotationMode.DISABLE,
        background: '#ffffff',
      });
      await rendering.promise;
    };
    void draw()
      .catch(() => {
        if (active) setError(true);
      })
      .finally(() => clearTimeout(timeout));
    return () => {
      active = false;
      clearTimeout(timeout);
      rendering?.cancel();
      target.width = target.height = 0;
    };
  }, [current, runtime, visible]);

  return (
    <section aria-label={`Page ${page}`} data-pdf-page={page} className="mb-4">
      <p className="mb-1 text-center text-xs text-muted-foreground select-none">
        Page {page}
      </p>
      <div
        ref={frame}
        className={styles.page}
        style={{
          width: current?.viewport.width ?? width,
          height: current?.viewport.height ?? width * 1.333,
        }}
      >
        <canvas
          ref={canvas}
          aria-hidden="true"
          className="absolute inset-0 block"
        />
        <div ref={textHost} />
        {error && (
          <p role="status" className="p-4 text-sm">
            Page preview unavailable. Use the page controls or download the PDF.
          </p>
        )}
      </div>
    </section>
  );
}
