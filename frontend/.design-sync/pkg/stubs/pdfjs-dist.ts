// design-sync stand-in for pdfjs-dist (routed via tsconfig.bundle.json).
// The real runtime is ~1.1 MB and pushes _ds_bundle.js past the 12 MB upload
// cap. Every app import of it is `import type` or a lazy `await import(...)`
// inside the PDF viewers' load effect, so this stub only changes what happens
// when a viewer actually tries to open a PDF: it lands in the viewer's own
// error state - the same thing a design preview with no real PDF bytes shows.
const unavailable = (): never => {
  throw new Error('PDF rendering is not available in design previews');
};

export const GlobalWorkerOptions: { workerSrc?: string } = {};
export const version = 'design-sync-stub';
export const PDFWorker = { create: unavailable };
export const getDocument = unavailable;
