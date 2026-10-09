import { expect, test, type Page } from '@playwright/test';
import { build } from 'esbuild';
import { readFile } from 'node:fs/promises';
import path from 'node:path';

const host = 'https://pdf-preview.test';
const nonce = 'cGRmLWNhbnZhcy1wcmV2aWV3LWJyb3dzZXI=';
// Match the complete production parent policy; PDF preview adds no exemptions.
const csp = [
  "default-src 'self'",
  "base-uri 'self'",
  "object-src 'none'",
  "frame-src 'self' blob:",
  "frame-ancestors 'none'",
  "form-action 'self'",
  `script-src 'self' 'nonce-${nonce}' 'strict-dynamic'`,
  "style-src 'self' 'unsafe-inline'",
  "img-src 'self' data: blob: https:",
  "font-src 'self' data:",
  "connect-src 'self' https: wss:",
  "worker-src 'self' blob:",
  'upgrade-insecure-requests',
].join('; ');
let bundle: string;
let worker: Buffer;

test.beforeAll(async () => {
  const root = path.resolve(__dirname, '../..');
  const output = await build({
    stdin: {
      resolveDir: root,
      loader: 'tsx',
      contents: `import React,{useState} from 'react';import{createRoot}from'react-dom/client';import{PdfArtifactPreview}from'./src/components/chat/artifact-panel/PdfArtifactPreview';function App(){const[blob,setBlob]=useState(null);return <><input aria-label="PDF fixture" type="file" onChange={e=>setBlob(e.target.files?.[0]??null)}/><button onClick={()=>setBlob(null)}>Remove PDF</button>{blob&&<PdfArtifactPreview blob={blob} title="browser.pdf"/>}</>}createRoot(document.getElementById('root')).render(<App/>);`,
    },
    bundle: true,
    write: false,
    platform: 'browser',
    jsx: 'automatic',
    format: 'esm',
    define: { 'process.env.NODE_ENV': '"production"' },
  });
  bundle = output.outputFiles[0].text;
  worker = await readFile(
    path.join(root, 'node_modules/pdfjs-dist/build/pdf.worker.min.mjs')
  );
});

async function mount(page: Page): Promise<string[]> {
  const unexpected: string[] = [];
  page.on('pageerror', (error) => {
    throw error;
  });
  await page.route('**/*', async (route) => {
    const url = route.request().url();
    if (url === `${host}/`)
      return route.fulfill({
        contentType: 'text/html',
        headers: { 'Content-Security-Policy': csp },
        body: `<!doctype html><div id="root"></div><script type="module" nonce="${nonce}" src="/preview.js"></script>`,
      });
    if (url === `${host}/preview.js`)
      return route.fulfill({ contentType: 'text/javascript', body: bundle });
    if (url === `${host}/pdfjs-dist/build/pdf.worker.min.mjs`)
      return route.fulfill({ contentType: 'text/javascript', body: worker });
    unexpected.push(url);
    return route.abort();
  });
  await page.goto(`${host}/`);
  return unexpected;
}

/** Synthetic PDF with page colors plus JavaScript and URI actions, no private data. */
function fixture({
  pages = 2,
  huge = false,
}: { pages?: number; huge?: boolean } = {}): Buffer {
  const objects: string[] = [
    '<< /Type /Catalog /Pages 2 0 R /OpenAction 3 0 R >>',
    `<< /Type /Pages /Kids [${Array.from({ length: pages }, (_, i) => `${4 + i * 2} 0 R`).join(' ')}] /Count ${pages} >>`,
    '<< /S /JavaScript /JS (window.__pdfPwned=1;fetch("https://pdf-probe.test/javascript")) >>',
  ];
  for (let i = 0; i < pages; i++) {
    const content = `${i % 2 ? '0 1 0' : '1 0 0'} rg 0 0 120 120 re f 0 0 0 rg BT /F1 14 Tf 8 50 Td (Canvas PDF) Tj ET`;
    objects.push(
      `<< /Type /Page /Parent 2 0 R /MediaBox [0 0 ${huge ? '100000 100000' : '120 120'}] /Resources << /Font << /F1 ${4 + pages * 2} 0 R >> >> /Annots [<< /Type /Annot /Subtype /Link /Rect [0 0 120 120] /A << /S /URI /URI (https://pdf-probe.test/link) >> >>] /Contents ${5 + i * 2} 0 R >>`
    );
    objects.push(
      `<< /Length ${content.length} >>\nstream\n${content}\nendstream`
    );
  }
  objects.push('<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>');
  let pdf = '%PDF-1.7\n';
  const offsets = [0];
  objects.forEach((object, index) => {
    offsets.push(Buffer.byteLength(pdf));
    pdf += `${index + 1} 0 obj\n${object}\nendobj\n`;
  });
  const xref = Buffer.byteLength(pdf);
  pdf += `xref\n0 ${objects.length + 1}\n0000000000 65535 f \n${offsets
    .slice(1)
    .map((offset) => `${String(offset).padStart(10, '0')} 00000 n \n`)
    .join(
      ''
    )}trailer\n<< /Size ${objects.length + 1} /Root 1 0 R >>\nstartxref\n${xref}\n%%EOF\n`;
  return Buffer.from(pdf);
}

test('real local worker renders pixels and pages under parent CSP without PDF actions', async ({
  page,
}) => {
  const unexpected = await mount(page);
  const workers: string[] = [];
  page.on('worker', (value) => workers.push(value.url()));
  await page.getByLabel('PDF fixture').setInputFiles({
    name: 'actions.pdf',
    mimeType: 'application/pdf',
    buffer: fixture(),
  });
  const canvas = page.getByRole('img', { name: 'browser.pdf, page 1' });
  await expect(canvas).toBeVisible();
  const first = await canvas.evaluate((element: HTMLCanvasElement) =>
    Array.from(element.getContext('2d')!.getImageData(20, 20, 1, 1).data)
  );
  expect(first).toEqual([255, 0, 0, 255]);
  // Standard PDF text must also draw without remote font assets.
  expect(
    await canvas.evaluate((element: HTMLCanvasElement) => {
      const data = element
        .getContext('2d')!
        .getImageData(0, 0, element.width, element.height).data;
      let black = 0;
      for (let i = 0; i < data.length; i += 4)
        if (
          data[i] < 30 &&
          data[i + 1] < 30 &&
          data[i + 2] < 30 &&
          data[i + 3] === 255
        )
          black++;
      return black;
    })
  ).toBeGreaterThan(20);
  await page.getByRole('button', { name: 'Next page' }).click();
  const second = page.getByRole('img', { name: 'browser.pdf, page 2' });
  await expect(second).toBeVisible();
  expect(
    await second.evaluate((element: HTMLCanvasElement) =>
      Array.from(element.getContext('2d')!.getImageData(20, 20, 1, 1).data)
    )
  ).toEqual([0, 255, 0, 255]);
  expect(workers.length).toBe(2);
  expect(
    workers.every(
      (url) => url === `${host}/pdfjs-dist/build/pdf.worker.min.mjs`
    )
  ).toBe(true);
  expect(
    await page.evaluate(
      () => (window as Window & { __pdfPwned?: number }).__pdfPwned
    )
  ).toBeUndefined();
  expect(await page.locator('iframe,object,embed,a,svg').count()).toBe(0);
  expect(unexpected).toEqual([]);
  await page.getByRole('button', { name: 'Remove PDF' }).click();
  await expect(page.locator('canvas')).toHaveCount(0);
});

test('malformed and over-limit files fail safely without a native plugin', async ({
  page,
}) => {
  const unexpected = await mount(page);
  await page.getByLabel('PDF fixture').setInputFiles({
    name: 'invalid.pdf',
    mimeType: 'application/pdf',
    buffer: Buffer.from('private invalid bytes'),
  });
  await expect(page.getByRole('alert')).toHaveText(/unavailable/);
  await expect(page.getByRole('img')).toHaveCount(0);
  await page.getByLabel('PDF fixture').setInputFiles({
    name: 'oversized.pdf',
    mimeType: 'application/pdf',
    buffer: Buffer.alloc(2 * 1024 * 1024 + 1),
  });
  await expect(page.getByRole('alert')).toHaveText(/unavailable/);
  expect(await page.locator('body').innerText()).not.toContain(
    'private invalid bytes'
  );
  expect(await page.locator('iframe,object,embed').count()).toBe(0);
  expect(unexpected).toEqual([]);
});

test('huge page dimensions are scaled and excessive page counts are rejected', async ({
  page,
}) => {
  await mount(page);
  await page.getByLabel('PDF fixture').setInputFiles({
    name: 'huge.pdf',
    mimeType: 'application/pdf',
    buffer: fixture({ pages: 1, huge: true }),
  });
  const canvas = page.getByRole('img');
  await expect(canvas).toBeVisible();
  const pixels = await canvas.evaluate(
    (element: HTMLCanvasElement) => element.width * element.height
  );
  expect(pixels).toBeLessThanOrEqual(2_000_000);
  await page.getByLabel('PDF fixture').setInputFiles({
    name: 'long.pdf',
    mimeType: 'application/pdf',
    buffer: fixture({ pages: 101 }),
  });
  await expect(page.getByRole('alert')).toHaveText(/unavailable/);
  await expect(page.getByRole('img')).toHaveCount(0);
});

// Optional actual Next proof. A disposable, untracked /pdf-artifact-qa page
// mounts this component behind the same "PDF fixture" file input. Start the
// default Next bundler, then set PDF_NEXT_PREVIEW_URL to that local page. Remove
// the QA route afterwards; it is deliberately absent from the shipped app.
test('actual Next bundles the local worker under production parent CSP', async ({
  page,
}) => {
  const url = process.env.PDF_NEXT_PREVIEW_URL;
  test.skip(!url, 'Requires a disposable actual Next QA route');
  let policy = '';
  await page.route(url!, async (route) => {
    const response = await route.fetch();
    const headers = response.headers();
    policy = (headers['content-security-policy'] ?? '')
      .replace(" 'unsafe-eval'", '')
      .replace(
        ' http://localhost:8000 ws://localhost:8000 http://127.0.0.1:8000 ws://127.0.0.1:8000',
        ''
      );
    if (!policy.includes('upgrade-insecure-requests'))
      policy += '; upgrade-insecure-requests';
    await route.fulfill({
      response,
      headers: { ...headers, 'content-security-policy': policy },
    });
  });
  const workers: string[] = [];
  page.on('worker', (worker) => workers.push(worker.url()));
  await page.goto(url!);
  expect(policy).toContain("'strict-dynamic'");
  expect(policy).toContain("object-src 'none'");
  expect(policy).toContain("worker-src 'self' blob:");
  expect(policy).not.toContain('unsafe-eval');
  await expect(page.getByLabel('PDF fixture')).toBeEnabled();
  await page.getByLabel('PDF fixture').setInputFiles({
    name: 'next-worker.pdf',
    mimeType: 'application/pdf',
    buffer: fixture(),
  });
  const canvas = page.getByRole('img', { name: 'browser.pdf, page 1' });
  await expect(canvas).toBeVisible();
  expect(
    await canvas.evaluate((element: HTMLCanvasElement) =>
      Array.from(element.getContext('2d')!.getImageData(20, 20, 1, 1).data)
    )
  ).toEqual([255, 0, 0, 255]);
  expect(workers).toHaveLength(1);
  expect(new URL(workers[0]).origin).toBe(new URL(url!).origin);
  expect(new URL(workers[0]).pathname).toMatch(/^\/_next\//);
  expect(await page.locator('iframe,object,embed').count()).toBe(0);
  await page.getByRole('button', { name: 'Remove PDF' }).click();
  await expect(page.locator('canvas')).toHaveCount(0);
});
