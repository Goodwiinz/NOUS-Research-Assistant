import { act, cleanup, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, expect, it, vi } from 'vitest';

import { render } from '@/test/test-utils';
import { useAuthStore } from '@/stores/authStore';
import type { User } from '@/types/auth';

vi.mock('@/services/artifactService', () => ({
  artifactService: {
    fetchVersionBlob: vi.fn(),
    downloadVersion: vi.fn(),
    capabilities: vi.fn(),
  },
}));

import { artifactService } from '@/services/artifactService';
import { ArtifactPreview } from '../ArtifactPreview';
import { PdfArtifactPreview } from '../PdfArtifactPreview';

const driver = vi.hoisted(() => ({
  pages: 2,
  width: 600,
  height: 800,
  loads: [] as Array<Record<string, unknown>>,
  rendering: Promise.resolve(),
  loading: null as Promise<unknown> | null,
  pageResult: null as Promise<unknown> | null,
  destroy: vi.fn(async () => {}),
  renderCancel: vi.fn(),
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
          getPage: async () =>
            driver.pageResult ?? {
              getViewport: ({ scale }: { scale: number }) => ({
                width: driver.width * scale,
                height: driver.height * scale,
              }),
              render: () => ({
                promise: driver.rendering,
                cancel: driver.renderCancel,
              }),
            },
        }),
    };
  },
}));

const file = (): Blob => new Blob(['%PDF-1.7'], { type: 'application/pdf' });
beforeEach(() => {
  driver.pages = 2;
  driver.width = 600;
  driver.height = 800;
  driver.loads.length = 0;
  driver.loading = null;
  driver.pageResult = null;
  driver.rendering = Promise.resolve();
  vi.stubGlobal(
    'Worker',
    class {
      terminate = driver.terminate;
    }
  );
});

// Regression: the sandboxed native PDF plugin is blocked by Chromium. The
// application must draw authenticated bytes itself instead of embedding them.
it('renders the authenticated PDF as a canvas instead of a plugin frame', async () => {
  vi.stubGlobal(
    'URL',
    class extends URL {
      static createObjectURL = (): string => 'blob:pdf-regression';
      static revokeObjectURL = vi.fn();
    }
  );
  useAuthStore.setState({
    user: { id: 'pdf-owner', organization_id: 'org' } as User,
    isAuthenticated: true,
  });
  vi.mocked(artifactService.capabilities).mockResolvedValue({
    editingEnabled: false,
    previewEnabled: false,
  });
  vi.mocked(artifactService.fetchVersionBlob).mockResolvedValue(
    new Blob(['%PDF-1.7'], { type: 'application/pdf' })
  );
  render(
    <ArtifactPreview
      version={{
        artifactId: 'pdf-artifact',
        versionId: 'pdf-version',
        parentVersionId: null,
        title: 'report.pdf',
        mimeType: 'application/pdf',
        byteSize: 8,
        sha256: 'test',
        createdAt: '2026-10-09T00:00:00Z',
        producer: 'harness',
        sourceIds: [],
      }}
    />
  );
  await screen.findByTitle('PDF preview');
  expect(document.querySelector('iframe,object,embed')).toBeNull();
  expect(document.querySelector('canvas')).not.toBeNull();
});

afterEach(() => {
  cleanup();
  vi.useRealTimers();
  vi.unstubAllGlobals();
});

it('draws one bounded page and provides accessible page navigation', async () => {
  const { user } = render(
    <PdfArtifactPreview blob={file()} title="report.pdf" />
  );
  const image = await screen.findByRole('img', { name: 'report.pdf, page 1' });
  expect(image).toHaveAttribute('width', '900');
  expect(image).toHaveAttribute('height', '1200');
  expect(screen.getByRole('button', { name: 'Previous page' })).toBeDisabled();
  await user.click(screen.getByRole('button', { name: 'Next page' }));
  await screen.findByRole('img', { name: 'report.pdf, page 2' });
  expect(screen.getByText('Page 2 of 2')).toBeInTheDocument();
  expect(screen.getByRole('button', { name: 'Next page' })).toBeDisabled();
  expect(document.querySelectorAll('canvas')).toHaveLength(1);
});

it('rejects actual oversized bytes before parsing or worker creation', async () => {
  render(
    <PdfArtifactPreview
      blob={new Blob([new Uint8Array(2 * 1024 * 1024 + 1)])}
      title="oversized.pdf"
    />
  );
  expect(await screen.findByRole('alert')).toHaveTextContent('unavailable');
  expect(driver.loads).toHaveLength(0);
  expect(screen.queryByRole('img')).toBeNull();
});

it('rejects document page counts above the cap without painting', async () => {
  driver.pages = 101;
  render(<PdfArtifactPreview blob={file()} title="long.pdf" />);
  expect(await screen.findByRole('alert')).toHaveTextContent('unavailable');
  expect(screen.queryByRole('img')).toBeNull();
  expect(driver.terminate).toHaveBeenCalled();
});

it('scales huge pages before allocating canvas pixels', async () => {
  driver.width = 100_000;
  driver.height = 100_000;
  render(<PdfArtifactPreview blob={file()} title="huge.pdf" />);
  const image = await screen.findByRole('img');
  expect(Number(image.getAttribute('width'))).toBeLessThanOrEqual(1200);
  expect(Number(image.getAttribute('height'))).toBeLessThanOrEqual(1600);
  expect(
    Number(image.getAttribute('width')) * Number(image.getAttribute('height'))
  ).toBeLessThanOrEqual(2_000_000);
});

it('denies external resource reads and bounds scratch canvas allocation', async () => {
  render(<PdfArtifactPreview blob={file()} title="bounded.pdf" />);
  await screen.findByRole('img');
  const options = driver.loads[0];
  expect(options).toMatchObject({
    isEvalSupported: false,
    enableXfa: false,
    useWorkerFetch: false,
    useWasm: false,
    disableFontFace: true,
  });
  const Data = options.BinaryDataFactory as new () => {
    fetch: () => Promise<never>;
  };
  await expect(new Data().fetch()).rejects.toThrow('disabled');
  const Factory = options.CanvasFactory as new () => {
    create: (width: number, height: number) => unknown;
    destroy: (entry: unknown) => void;
  };
  const factory = new Factory();
  expect(() => factory.create(20_000, 20_000)).toThrow();
  vi.spyOn(HTMLCanvasElement.prototype, 'getContext').mockReturnValue(
    {} as CanvasRenderingContext2D
  );
  const entries = Array.from({ length: 8 }, () => factory.create(1000, 1000));
  expect(() => factory.create(1000, 1000)).toThrow();
  factory.destroy(entries[0]);
  expect(() => factory.create(1000, 1000)).not.toThrow();
  const countBounded = new Factory();
  Array.from({ length: 16 }, () => countBounded.create(1, 1));
  expect(() => countBounded.create(1, 1)).toThrow();
});

it('stops hung parsing and ignores late private results', async () => {
  let resolve: (value: unknown) => void = () => {};
  driver.loading = new Promise((done) => {
    resolve = done;
  });
  render(<PdfArtifactPreview blob={file()} title="private.pdf" />);
  await waitFor(() => expect(driver.loads).toHaveLength(1));
  await act(async () =>
    screen.getByRole('button', { name: 'Stop preview' }).click()
  );
  expect(screen.getByRole('alert')).toHaveTextContent('stopped');
  expect(driver.destroy).toHaveBeenCalled();
  expect(driver.terminate).toHaveBeenCalled();
  await act(async () =>
    resolve({
      numPages: 1,
      getPage: () => {
        throw new Error('late source');
      },
    })
  );
  expect(screen.queryByRole('img')).toBeNull();
});

// Mutation proof: remove the `if (!active) return` after getPage in
// PdfArtifactPreview, then run this test: old page dimensions overwrite Bob's
// visible canvas. Restore it and rerun before committing.
it('ignores an old account page that resolves after the new canvas is ready', async () => {
  let resolve: (value: unknown) => void = () => {};
  driver.pageResult = new Promise((done) => {
    resolve = done;
  });
  const { rerender } = render(
    <PdfArtifactPreview blob={file()} title="Alice.pdf" />
  );
  await waitFor(() => expect(driver.loads).toHaveLength(1));
  await act(async () => {});
  driver.pageResult = null;
  driver.width = driver.height = 400;
  rerender(<PdfArtifactPreview blob={file()} title="Bob.pdf" />);
  const canvas = await screen.findByRole('img', { name: 'Bob.pdf, page 1' });
  expect(canvas).toHaveAttribute('width', '600');
  await act(async () =>
    resolve({
      getViewport: () => ({ width: 10, height: 10 }),
      render: () => ({ promise: Promise.resolve(), cancel: vi.fn() }),
    })
  );
  expect(canvas).toHaveAttribute('width', '600');
  expect(canvas).toHaveAttribute('height', '600');
});

it('times out even when the file reader never completes', async () => {
  vi.useFakeTimers();
  const abort = vi.fn();
  vi.stubGlobal(
    'FileReader',
    class {
      readAsArrayBuffer(): void {}
      abort = abort;
    }
  );
  render(<PdfArtifactPreview blob={file()} title="hung.pdf" />);
  await act(async () => {
    vi.advanceTimersByTime(30_001);
  });
  expect(screen.getByRole('alert')).toHaveTextContent('unavailable');
  expect(abort).toHaveBeenCalled();
});

it('clears previous pixels immediately when blob ownership changes', async () => {
  const { rerender } = render(
    <PdfArtifactPreview blob={file()} title="Alice.pdf" />
  );
  const image = await screen.findByRole('img');
  driver.loading = new Promise(() => {});
  rerender(<PdfArtifactPreview blob={file()} title="Bob.pdf" />);
  expect(screen.queryByRole('img')).toBeNull();
  expect(image).toHaveAttribute('width', '0');
  expect(screen.getByRole('status')).toHaveTextContent('Preparing');
});

it('cancels rendering and terminates worker on unmount', async () => {
  driver.rendering = new Promise(() => {});
  const { unmount } = render(
    <PdfArtifactPreview blob={file()} title="rendering.pdf" />
  );
  await waitFor(() => expect(driver.loads).toHaveLength(1));
  // Allow getPage to finish and establish its render task.
  await act(async () => {});
  unmount();
  expect(driver.renderCancel).toHaveBeenCalled();
  expect(driver.destroy).toHaveBeenCalled();
  expect(driver.terminate).toHaveBeenCalled();
});
