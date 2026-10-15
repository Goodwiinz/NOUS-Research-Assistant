import { expect, test, type Page } from '@playwright/test';
import { build } from 'esbuild';
import { createHash } from 'node:crypto';
import { readFile } from 'node:fs/promises';
import { createRequire } from 'node:module';
import path from 'node:path';

const host = 'https://artifact-sharing.test';
const nonce = 'YXJ0aWZhY3Qtc2hhcmluZy1icm93c2Vy';
const shareId = '33333333-3333-4333-8333-333333333333';
const versionId = '22222222-2222-4222-8222-222222222222';
const actor = '11111111-1111-4111-8111-111111111111';
const recipient = '44444444-4444-4444-8444-444444444444';
// The production parent policy from proxy.ts, also used by pdf-preview.spec.ts.
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

test.beforeAll(async ({ browser }) => {
  console.log(`Local sharing fixture browser: Chromium ${browser.version()}`);
  const root = path.resolve(__dirname, '../..');
  const output = await build({
    stdin: {
      resolveDir: root,
      loader: 'tsx',
      contents: `
        import React,{useRef,useState} from 'react';
        import {createRoot} from 'react-dom/client';
        import {QueryClient,QueryClientProvider} from '@tanstack/react-query';
        import {SharedArtifactView} from './src/components/chat/artifact-panel/SharedArtifactView';
        import {ArtifactShareDialog} from './src/components/chat/artifact-panel/ArtifactShareDialog';
        import {useAuthStore} from '@/stores/authStore';
        const client=new QueryClient({defaultOptions:{queries:{retry:false},mutations:{retry:false}}});
        window.__sharingProof={
          cache:()=>JSON.stringify(client.getQueryCache().getAll().map(q=>q.state.data)),
          switchAccount:()=>useAuthStore.setState({user:{id:'other-actor',organization_id:'fixture-org'}}),
        };
        const version={artifactId:'fixture-artifact',versionId:'${versionId}',parentVersionId:null,
          title:'Saved v1.pdf',mimeType:'application/pdf',byteSize:1,sha256:'fixture',
          producer:'harness',createdAt:'2026-10-11T00:00:00Z',sourceIds:['fixture-private-source']};
        function Owner(){const[open,setOpen]=useState(false);const trigger=useRef(null);return <>
          <button ref={trigger} onClick={()=>setOpen(true)}>Share version</button>
          {open&&<ArtifactShareDialog version={version} triggerRef={trigger} onClose={()=>setOpen(false)}/>}
        </>}
        function App(){return <QueryClientProvider client={client}>
          <p>Local fixture: mocked auth and HTTP transport</p>
          {location.pathname==='/owner'?<Owner/>:<SharedArtifactView shareId={'${shareId}'}/>}
        </QueryClientProvider>}
        createRoot(document.getElementById('root')).render(<App/>);
      `,
    },
    bundle: true,
    write: false,
    platform: 'browser',
    jsx: 'automatic',
    format: 'esm',
    // These PDF/text proofs do not exercise math styling. Keep every production
    // renderer and hook, while omitting their imported stylesheet assets.
    loader: { '.css': 'empty' },
    tsconfig: path.join(root, 'tsconfig.json'),
    define: { 'process.env.NODE_ENV': '"production"' },
    plugins: [
      {
        name: 'explicit-local-auth-and-transport',
        setup(builder) {
          builder.onResolve(
            {
              filter:
                /^@\/(stores\/authStore|store\/chat-store|services\/api-client)$/,
            },
            ({ path: modulePath }) => ({
              path: modulePath,
              namespace: 'sharing-fixture',
            })
          );
          builder.onLoad(
            { filter: /.*/, namespace: 'sharing-fixture' },
            ({ path: modulePath }) => ({
              resolveDir: root,
              loader: 'ts',
              contents: modulePath.endsWith('authStore')
                ? `import{create}from'zustand';export const useAuthStore=create(()=>({
                    user:{id:'${actor}',organization_id:'fixture-org'},isAuthenticated:true}));`
                : modulePath.endsWith('chat-store')
                  ? `import{create}from'zustand';export const useChatStore=create(()=>({
                      currentWorkspaceId:'fixture-workspace',currentThreadId:'fixture-thread'}));
                      export const selectCurrentThreadProjectId=()=>null;`
                  : `import{useAuthStore}from'@/stores/authStore';
                    async function request(endpoint,options={}){
                      const response=await fetch('/api/v1'+endpoint,{...options,headers:{
                        'Content-Type':'application/json','X-Fixture-Actor':useAuthStore.getState().user.id}});
                      if(!response.ok)throw new Error('synthetic transport denial');return response;
                    }
                    export const api={
                      get:async(endpoint,options)=> (await request(endpoint,options)).json(),
                      post:async(endpoint,body,options)=> (await request(endpoint,{...options,method:'POST',body:JSON.stringify(body)})).json(),
                      delete:async(endpoint,options)=>{await request(endpoint,{...options,method:'DELETE'})},
                      fetchBlob:async(endpoint,signal)=>(await request(endpoint,{signal})).blob(),
                      download:async(endpoint,title)=>{const blob=await(await request(endpoint)).blob();
                        const url=URL.createObjectURL(blob);const link=document.createElement('a');
                        link.href=url;link.download=title;document.body.append(link);link.click();link.remove();
                        setTimeout(()=>URL.revokeObjectURL(url),0);},
                    };`,
            })
          );
        },
      },
    ],
  });
  bundle = output.outputFiles[0].text;
  worker = await readFile(
    createRequire(path.join(root, 'package.json')).resolve(
      'pdfjs-dist/build/pdf.worker.min.mjs'
    )
  );
});

/** A synthetic red PDF page whose immutable bytes have a known digest. */
function pdfFixture(): Buffer {
  const commands = '1 0 0 rg 0 0 120 120 re f';
  const objects = [
    '<< /Type /Catalog /Pages 2 0 R >>',
    '<< /Type /Pages /Kids [3 0 R] /Count 1 >>',
    '<< /Type /Page /Parent 2 0 R /MediaBox [0 0 120 120] /Contents 4 0 R >>',
    `<< /Length ${commands.length} >>\nstream\n${commands}\nendstream`,
  ];
  let pdf = '%PDF-1.7\n';
  const offsets = [0];
  objects.forEach((object, index) => {
    offsets.push(Buffer.byteLength(pdf));
    pdf += `${index + 1} 0 obj\n${object}\nendobj\n`;
  });
  const xref = Buffer.byteLength(pdf);
  pdf += `xref\n0 5\n0000000000 65535 f \n${offsets
    .slice(1)
    .map((offset) => `${String(offset).padStart(10, '0')} 00000 n \n`)
    .join('')}trailer\n<< /Size 5 /Root 1 0 R >>\nstartxref\n${xref}\n%%EOF\n`;
  return Buffer.from(pdf);
}

type CreateBody = {
  version_id: string;
  audience_user_ids: string[];
  expires_at: string;
};
type BrowserProof = {
  __sharingProof: { cache: () => string; switchAccount: () => void };
};
type FixtureState = {
  bytes: Buffer;
  title: string;
  sha256: string;
  revoked: boolean;
  shares: Record<string, unknown>[];
  creates: CreateBody[];
  deletes: string[];
  requests: string[];
  aborted: string[];
  unexpected: string[];
  errors: string[];
  metadataGate: Promise<void> | null;
  contentGate: Promise<void> | null;
};

async function mount(
  page: Page,
  mode: 'viewer' | 'owner',
  pdf = false,
  contentGate: Promise<void> | null = null
): Promise<FixtureState> {
  const bytes = pdf ? pdfFixture() : Buffer.from('Private saved v1 body');
  const title = pdf ? 'Saved v1.pdf' : 'Private saved v1.txt';
  const state: FixtureState = {
    bytes,
    title,
    sha256: createHash('sha256').update(bytes).digest('hex'),
    revoked: false,
    shares: [] as Record<string, unknown>[],
    creates: [] as CreateBody[],
    deletes: [] as string[],
    requests: [] as string[],
    aborted: [],
    unexpected: [] as string[],
    errors: [] as string[],
    metadataGate: null as Promise<void> | null,
    contentGate,
  };
  page.on('pageerror', (error) => state.errors.push(error.message));
  page.on('requestfailed', (request) => {
    if (request.failure()?.errorText === 'net::ERR_ABORTED')
      state.aborted.push(new URL(request.url()).pathname);
  });
  await page.route('**/*', async (route) => {
    const request = route.request();
    const url = new URL(request.url());
    if (url.origin !== host) {
      state.unexpected.push(request.url());
      return route.abort();
    }
    if (
      url.pathname === '/owner' ||
      url.pathname === `/artifacts/shared/${shareId}`
    )
      return route.fulfill({
        contentType: 'text/html',
        headers: { 'Content-Security-Policy': csp },
        body: `<!doctype html><link rel="icon" href="data:,"><style>body{font-family:sans-serif}[role=dialog]{background:white;border:1px solid;padding:20px;position:fixed;top:10%;left:10%;max-width:650px}button,input,textarea{margin:4px}textarea{display:block}</style><div id="root"></div><script type="module" nonce="${nonce}" src="/sharing.js"></script>`,
      });
    if (url.pathname === '/sharing.js')
      return route.fulfill({ contentType: 'text/javascript', body: bundle });
    if (url.pathname === '/pdfjs-dist/build/pdf.worker.min.mjs')
      return route.fulfill({ contentType: 'text/javascript', body: worker });
    if (url.pathname.startsWith('/api/v1/artifacts/')) {
      state.requests.push(`${request.method()} ${url.pathname}`);
      const json = (body: unknown, status = 200): Promise<void> =>
        route.fulfill({
          status,
          contentType: 'application/json',
          headers: { 'Cache-Control': 'private, no-store' },
          body: JSON.stringify(body),
        });
      if (request.headers()['x-fixture-actor'] !== actor)
        return json({ detail: 'fixture actor outside audience' }, 404);
      if (url.pathname.endsWith('/capabilities'))
        return json({
          editing_enabled: false,
          preview_enabled: false,
          sharing_enabled: true,
        });
      if (url.pathname === '/api/v1/artifacts/fixture-artifact/shares')
        return json(state.shares);
      if (url.pathname === '/api/v1/artifacts/shares') {
        const body = request.postDataJSON() as CreateBody;
        state.creates.push(body);
        const share = {
          id: shareId,
          version_id: body.version_id,
          expires_at: body.expires_at,
          revoked_at: null,
        };
        state.shares.push(share);
        return json(share, 201);
      }
      if (request.method() === 'DELETE') {
        state.deletes.push(url.pathname);
        state.revoked = true;
        state.shares = [];
        return route.fulfill({ status: 204 });
      }
      if (url.pathname.endsWith('/content')) {
        await state.contentGate;
        if (state.revoked) return json({ detail: 'fixture revoked' }, 404);
        return route.fulfill({
          contentType: pdf ? 'application/pdf' : 'text/plain',
          headers: { 'Cache-Control': 'private, no-store' },
          body: bytes,
        });
      }
      if (url.pathname === `/api/v1/artifacts/shares/${shareId}`) {
        await state.metadataGate;
        if (state.revoked) return json({ detail: 'fixture revoked' }, 404);
        return json({
          artifact_id: 'fixture-artifact',
          version_id: versionId,
          title,
          mime_type: pdf ? 'application/pdf' : 'text/plain',
          byte_size: bytes.length,
          sha256: state.sha256,
        });
      }
    }
    state.unexpected.push(request.url());
    return route.abort();
  });
  const response = await page.goto(
    mode === 'owner' ? `${host}/owner` : `${host}/artifacts/shared/${shareId}`
  );
  expect(response?.headers()['content-security-policy']).toBe(csp);
  return state;
}

test('renders and downloads the pinned PDF bytes under the parent CSP', async ({
  page,
}) => {
  const state = await mount(page, 'viewer', true);
  const canvas = page.getByRole('img', { name: 'Saved v1.pdf, page 1' });
  await expect(canvas).toBeVisible();
  expect(
    await canvas.evaluate((element: HTMLCanvasElement) =>
      Array.from(element.getContext('2d')!.getImageData(20, 20, 1, 1).data)
    )
  ).toEqual([255, 0, 0, 255]);
  const saved = page.waitForEvent('download');
  await page.getByRole('button', { name: 'Download Saved v1.pdf' }).click();
  const download = await saved;
  expect(download.suggestedFilename()).toBe('Saved v1.pdf');
  const stream = await download.createReadStream();
  const chunks: Buffer[] = [];
  for await (const chunk of stream!) chunks.push(Buffer.from(chunk));
  expect(createHash('sha256').update(Buffer.concat(chunks)).digest('hex')).toBe(
    state.sha256
  );
  expect(
    state.requests.filter((request) => request.endsWith('/content'))
  ).toEqual([
    `GET /api/v1/artifacts/shares/${shareId}/content`,
    `GET /api/v1/artifacts/shares/${shareId}/content`,
  ]);
  await expect(page.getByRole('button', { name: 'Edit' })).toHaveCount(0);
  await expect(page.getByLabel('Provenance')).toHaveCount(0);
  expect(state.unexpected).toEqual([]);
  expect(state.errors).toEqual([]);
});

test('an open revoked share drops its private DOM and Query data on refetch', async ({
  page,
}) => {
  const state = await mount(page, 'viewer');
  await expect(page.locator('pre')).toHaveText('Private saved v1 body');
  let release!: () => void;
  state.metadataGate = new Promise<void>((done) => {
    release = done;
  });
  state.revoked = true;
  await page.getByRole('button', { name: 'Refresh shared version' }).click();
  await expect(page.getByRole('status')).toHaveText(
    'Checking shared file access…'
  );
  await expect(page.locator('pre')).toHaveCount(0);
  await expect(page.getByRole('heading', { name: state.title })).toHaveCount(0);
  release();
  await expect(page.getByRole('alert')).toContainText(
    'Shared file is unavailable'
  );
  const cache = await page.evaluate(() =>
    (window as Window & BrowserProof).__sharingProof.cache()
  );
  expect(cache).not.toContain(state.title);
  expect(cache).not.toContain('Private saved v1 body');
  expect(state.unexpected).toEqual([]);
  expect(state.errors).toEqual([]);
});

test('dialog creates and revokes the selected version with keyboard focus', async ({
  page,
}) => {
  const state = await mount(page, 'owner');
  const trigger = page.getByRole('button', {
    name: 'Share version',
    exact: true,
  });
  await trigger.focus();
  await page.keyboard.press('Enter');
  const audience = page.getByLabel('Audience user IDs');
  await expect(audience).toBeFocused();
  await page.keyboard.press('Shift+Tab');
  expect(
    await page.evaluate(() =>
      Boolean(document.activeElement?.closest('[role=dialog]'))
    )
  ).toBe(true);
  await audience.fill(`not-a-user`);
  await page.getByRole('button', { name: 'Create share link' }).click();
  await expect(page.getByRole('alert')).toContainText('1–100 valid user IDs');
  expect(state.creates).toHaveLength(0);
  await audience.fill(`${recipient}, ${recipient.toUpperCase()}`);
  const expiry = page.getByLabel('Expires at');
  expect(
    new Date((await expiry.getAttribute('max'))!).getTime()
  ).toBeLessThanOrEqual(Date.now() + 7 * 86400000);
  await page.getByRole('button', { name: 'Create share link' }).focus();
  await page.keyboard.press('Enter');
  const link = page.getByRole('link', { name: 'Open shared version' });
  await expect(link).toHaveAttribute('href', `/artifacts/shared/${shareId}`);
  expect(state.creates).toHaveLength(1);
  expect(state.creates[0].version_id).toBe(versionId);
  expect(state.creates[0].audience_user_ids).toEqual([recipient]);
  expect(state.creates[0].expires_at).toMatch(/Z$/);
  expect(new Date(state.creates[0].expires_at).getTime()).toBeGreaterThan(
    Date.now()
  );
  await page.getByRole('button', { name: 'Copy share link' }).click();
  expect(await page.evaluate(() => navigator.clipboard.readText())).toBe(
    `${host}/artifacts/shared/${shareId}`
  );
  await page.keyboard.press('Escape');
  await expect(page.getByRole('dialog')).toHaveCount(0);
  await expect(trigger).toBeFocused();
  await page.keyboard.press('Enter');
  await expect(link).toBeVisible();
  await page.getByRole('button', { name: 'Revoke share link' }).focus();
  await page.keyboard.press('Enter');
  await expect(link).toHaveCount(0);
  await expect(page.getByRole('status')).toHaveText('Share link revoked.');
  expect(state.deletes).toEqual([`/api/v1/artifacts/shares/${shareId}`]);
  await page.keyboard.press('Escape');
  await expect(trigger).toBeFocused();
  expect(state.unexpected).toEqual([]);
  expect(state.errors).toEqual([]);
});

test('changing account aborts an old pending byte read and never reveals it', async ({
  page,
}) => {
  let release!: () => void;
  const gate = new Promise<void>((done) => {
    release = done;
  });
  // Pause bytes from the first request before the real Query can cache them.
  const state = await mount(page, 'viewer', false, gate);
  await expect
    .poll(() => state.requests.some((url) => url.endsWith('/content')))
    .toBe(true);
  await page.evaluate(() =>
    (window as Window & BrowserProof).__sharingProof.switchAccount()
  );
  await expect(page.getByRole('alert')).toContainText(
    'Shared file is unavailable'
  );
  await expect
    .poll(() => state.aborted)
    .toContain(`/api/v1/artifacts/shares/${shareId}/content`);
  release();
  await expect(page.locator('pre')).toHaveCount(0);
  await expect(page.getByRole('heading', { name: state.title })).toHaveCount(0);
  expect(
    await page.evaluate(() =>
      (window as Window & BrowserProof).__sharingProof.cache()
    )
  ).not.toContain(state.title);
  expect(state.unexpected).toEqual([]);
  expect(state.errors).toEqual([]);
});
