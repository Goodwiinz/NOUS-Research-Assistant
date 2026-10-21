import type { PDFWorker } from 'pdfjs-dist';
import type { DocumentInitParameters } from 'pdfjs-dist/types/src/display/api';

export const MAX_PIXELS = 2_000_000;

/** No PDF-selected font, CMap or decoder URL may reach the network. */
export class NoExternalPdfData {
  async fetch(): Promise<never> {
    throw new Error('External PDF resources are disabled');
  }
}

type CanvasEntry = {
  canvas: HTMLCanvasElement;
  context: CanvasRenderingContext2D;
};

/** The library's temporary image/pattern canvases share one allocation budget. */
export class BoundedPdfCanvasFactory {
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

/** Data-only rendering: no PDF-controlled network, actions, forms or XFA. */
export function safePdfOptions(
  data: Uint8Array,
  worker: PDFWorker
): DocumentInitParameters & { isEvalSupported: boolean } {
  return {
    data,
    worker,
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
}
