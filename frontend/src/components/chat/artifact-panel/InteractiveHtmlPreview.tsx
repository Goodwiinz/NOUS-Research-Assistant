'use client';

import { useEffect, useMemo, useRef, useState, type ReactElement } from 'react';

const MAX_HEIGHT = 2000;
const MAX_SOURCE_BYTES = 2 * 1024 * 1024;
const SCRIPT_NONCE = /^[A-Za-z0-9+/_=-]{16,128}$/;
type PreviewMessage =
  { type: 'height'; height: number } | { type: 'ready' | 'error' };

/** Only layout/status crosses the opaque frame boundary; never an RPC channel. */
export function readPreviewMessage(
  data: unknown,
  nonce: string
): PreviewMessage | null {
  if (!data || typeof data !== 'object') return null;
  const value = data as Record<string, unknown>;
  if (value.nonce !== nonce) return null;
  if (value.type === 'ready' || value.type === 'error')
    return { type: value.type };
  if (
    value.type === 'height' &&
    typeof value.height === 'number' &&
    Number.isInteger(value.height) &&
    value.height > 0 &&
    value.height <= MAX_HEIGHT
  )
    return { type: 'height', height: value.height };
  return null;
}

function scriptJson(value: string): string {
  return JSON.stringify(value)
    .replace(/</g, '\\u003c')
    .replace(/\u2028/g, '\\u2028')
    .replace(/\u2029/g, '\\u2029');
}

/**
 * Blob documents inherit the creator's CSP. The first policy permits only our
 * nonce bootstrap; the intersecting second policy additionally forbids external
 * scripts even if hostile HTML copies its nonce onto a script URL.
 * Inline handlers are deliberately unsupported: use scripts/addEventListener.
 */
export function buildHtmlPreviewDocument(
  source: string,
  nonce: string,
  scriptNonce: string = nonce
): string {
  if (!/^[A-Za-z0-9-]{16,128}$/.test(nonce))
    throw new Error('Invalid preview nonce');
  if (new TextEncoder().encode(source).byteLength > MAX_SOURCE_BYTES)
    throw new Error('Preview exceeds limit');
  if (!SCRIPT_NONCE.test(scriptNonce)) throw new Error('Invalid script nonce');
  const restrictions =
    "default-src 'none'; frame-src blob:; style-src 'unsafe-inline'; img-src data: blob:; connect-src 'none'; form-action 'none'; base-uri 'none'; object-src 'none'; worker-src 'none'";
  const policy = `${restrictions}; script-src 'nonce-${scriptNonce}'`;
  const inlineOnly = `${restrictions}; script-src 'unsafe-inline'`;
  const reporter = `(() => {
    const nonce = ${scriptJson(nonce)};
    const send = (type, height) => parent.postMessage({type, nonce, ...(height ? {height} : {})}, '*');
    const measure = () => send('height', Math.max(1, Math.min(2000, Math.ceil(document.documentElement.scrollHeight))));
    addEventListener('error', () => send('error'));
    addEventListener('unhandledrejection', () => send('error'));
    addEventListener('DOMContentLoaded', () => { new ResizeObserver(measure).observe(document.documentElement); measure(); send('ready'); });
  })();`;
  return `<!doctype html><html><head><meta http-equiv="Content-Security-Policy" content="${policy}"><meta http-equiv="Content-Security-Policy" content="${inlineOnly}"><meta name="referrer" content="no-referrer"></head><body><script nonce="${scriptNonce}">
(() => {
  const nonce = ${scriptJson(nonce)};
  const scriptNonce = ${scriptJson(scriptNonce)};
  const source = ${scriptJson(source)};
  const doc = new DOMParser().parseFromString(source, 'text/html');
  doc.querySelectorAll('base, meta[http-equiv]').forEach(node => node.remove());
  doc.querySelectorAll('*').forEach(node => {
    for (const attr of Array.from(node.attributes)) if (/^on/i.test(attr.name)) node.removeAttribute(attr.name);
  });
  doc.querySelectorAll('script').forEach(node => {
    if (node.hasAttribute('src') || node.hasAttribute('href') || node.hasAttribute('xlink:href')) node.remove();
    else node.setAttribute('nonce', scriptNonce);
  });
  const csp = doc.createElement('meta');
  csp.httpEquiv = 'Content-Security-Policy'; csp.content = ${scriptJson(policy)};
  doc.head.prepend(csp);
  const status = doc.createElement('script'); status.setAttribute('nonce', scriptNonce); status.textContent = ${scriptJson(reporter)};
  csp.after(status);
  const child = document.createElement('iframe');
  child.title = 'HTML content'; child.setAttribute('sandbox', 'allow-scripts'); child.referrerPolicy = 'no-referrer';
  child.style.cssText = 'width:100%;height:100%;border:0';
  document.body.style.cssText = 'margin:0;height:100vh';
  const url = URL.createObjectURL(new Blob(['<!doctype html>' + doc.documentElement.outerHTML], {type: 'text/html'}));
  addEventListener('message', event => {
    const data = event.data;
    if (event.source !== child.contentWindow || event.origin !== 'null' || !data || data.nonce !== nonce) return;
    if (data.type === 'height' && Number.isInteger(data.height) && data.height > 0 && data.height <= 2000)
      parent.postMessage({type:'height', nonce, height:data.height}, '*');
    else if (data.type === 'error' || data.type === 'ready') parent.postMessage({type:data.type, nonce}, '*');
  });
  addEventListener('pagehide', () => URL.revokeObjectURL(url), {once:true});
  child.src = url; document.body.append(child);
})();
</script></body></html>`;
}

export function InteractiveHtmlPreview({
  source,
}: {
  source: string;
}): ReactElement {
  const [nonce] = useState(() => crypto.randomUUID());
  const [scriptNonce] = useState(() => {
    const trusted =
      typeof window === 'undefined'
        ? null
        : window.document.querySelector<HTMLScriptElement>('script[nonce]')
            ?.nonce;
    return trusted && SCRIPT_NONCE.test(trusted) ? trusted : null;
  });
  const [height, setHeight] = useState(360);
  const [status, setStatus] = useState<'loading' | 'ready' | 'error'>(
    'loading'
  );
  const [stopped, setStopped] = useState(false);
  const frame = useRef<HTMLIFrameElement>(null);
  const document = useMemo(
    () =>
      scriptNonce ? buildHtmlPreviewDocument(source, nonce, scriptNonce) : null,
    [source, nonce, scriptNonce]
  );
  useEffect(() => {
    if (stopped || !scriptNonce) return;
    const timeout = window.setTimeout(() => setStatus('error'), 5000);
    const receive = (event: MessageEvent): void => {
      if (
        event.source !== frame.current?.contentWindow ||
        event.origin !== 'null'
      )
        return;
      const message = readPreviewMessage(event.data, nonce);
      if (!message) return;
      if (message.type === 'height') setHeight(message.height);
      else {
        window.clearTimeout(timeout);
        setStatus(message.type);
      }
    };
    window.addEventListener('message', receive);
    return () => {
      window.clearTimeout(timeout);
      window.removeEventListener('message', receive);
    };
  }, [nonce, stopped, scriptNonce]);
  if (!document)
    return (
      <p role="alert" className="p-4">
        Preview is unavailable for this page. Use the source below.
      </p>
    );
  return (
    <div className="p-4">
      <p className="mb-3 text-sm">
        Interactive preview supports embedded scripts. Inline event attributes
        and external resources are unavailable; source remains below.
      </p>
      {!stopped && (
        <>
          <button
            type="button"
            className="mb-3 rounded-md border px-3 py-2 text-sm"
            onClick={() => setStopped(true)}
          >
            Stop HTML preview
          </button>
          {status === 'loading' && <p role="status">Starting preview…</p>}
          {status === 'error' && (
            <p role="alert">
              Preview could not finish. You can stop it and use the source.
            </p>
          )}
          <iframe
            ref={frame}
            title="Interactive HTML preview"
            srcDoc={document}
            sandbox="allow-scripts"
            referrerPolicy="no-referrer"
            className="w-full border"
            style={{ height }}
          />
        </>
      )}
    </div>
  );
}
