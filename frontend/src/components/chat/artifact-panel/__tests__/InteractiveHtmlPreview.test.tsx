import { act, screen } from '@testing-library/react';
import { expect, it, vi } from 'vitest';
import { render } from '@/test/test-utils';
import {
  buildHtmlPreviewDocument,
  readPreviewMessage,
  InteractiveHtmlPreview,
} from '../InteractiveHtmlPreview';

const nonce = 'preview-test-nonce-12345';
it('contains only the nonce bootstrap in the outer document and JSON-escapes closing-script source', () => {
  const html = buildHtmlPreviewDocument(
    '</script><script>parent.pwned=1</script>\u2028',
    nonce
  );
  const doc = new DOMParser().parseFromString(html, 'text/html');
  expect(doc.querySelectorAll('script')).toHaveLength(1);
  expect(doc.querySelector('script')?.nonce).toBe(nonce);
  expect(
    doc
      .querySelector('meta[http-equiv="Content-Security-Policy"]')
      ?.getAttribute('content')
  ).toContain(`script-src 'nonce-${nonce}'`);
  expect(
    doc
      .querySelector('meta[http-equiv="Content-Security-Policy"]')
      ?.getAttribute('content')
  ).toContain('frame-src blob:');
  expect(html).not.toContain('<script>parent.pwned');
  expect(html).toContain('\\u003c/script>');
});
it('rejects malformed, forged or unbounded messages and exposes only layout/error state', () => {
  expect(
    readPreviewMessage({ type: 'height', nonce, height: 400 }, nonce)
  ).toEqual({ type: 'height', height: 400 });
  for (const data of [
    { type: 'height', nonce: 'other', height: 400 },
    { type: 'height', nonce, height: 2001 },
    { type: 'height', nonce, height: NaN },
    { type: 'height', nonce, height: 0 },
    { type: 'rpc', nonce, method: 'fetch' },
    null,
    'oops',
  ])
    expect(readPreviewMessage(data, nonce)).toBeNull();
  expect(
    readPreviewMessage({ type: 'error', nonce, message: 'secret' }, nonce)
  ).toEqual({ type: 'error' });
});
it('accepts bounded messages only from its own opaque wrapper, and removes the frame on stop', async () => {
  const { user } = render(
    <InteractiveHtmlPreview source="<button>calculator</button>" />
  );
  const frame = screen.getByTitle(
    'Interactive HTML preview'
  ) as HTMLIFrameElement;
  expect(frame).toHaveAttribute('sandbox', 'allow-scripts');
  expect(frame).not.toHaveAttribute('allow');
  const match = frame.srcdoc.match(/const nonce = "([^"]+)"/);
  expect(match).not.toBeNull();
  const token = match?.[1];
  act(() =>
    window.dispatchEvent(
      new MessageEvent('message', {
        data: { type: 'height', nonce: token, height: 777 },
        source: window,
        origin: 'null',
      })
    )
  );
  expect(frame.style.height).not.toBe('777px');
  act(() =>
    window.dispatchEvent(
      new MessageEvent('message', {
        data: { type: 'height', nonce: token, height: 777 },
        source: frame.contentWindow,
        origin: 'null',
      })
    )
  );
  expect(frame.style.height).toBe('777px');
  await user.click(screen.getByRole('button', { name: 'Stop HTML preview' }));
  expect(screen.queryByTitle('Interactive HTML preview')).toBeNull();
});

it('rejects excessive source and malformed nonces before building a frame', () => {
  expect(() => buildHtmlPreviewDocument('x', 'bad')).toThrow(
    'Invalid preview nonce'
  );
  expect(() =>
    buildHtmlPreviewDocument('é'.repeat(1024 * 1024 + 1), nonce)
  ).toThrow('Preview exceeds limit');
  expect(readPreviewMessage({ type: 'ready', nonce }, nonce)).toEqual({
    type: 'ready',
  });
  expect(readPreviewMessage({ type: 'unknown', nonce }, nonce)).toBeNull();
});
it('shows a stable timeout and ignores a same-frame message from a nonopaque origin', () => {
  vi.useFakeTimers();
  try {
    render(<InteractiveHtmlPreview source="<p>source</p>" />);
    const frame = screen.getByTitle(
      'Interactive HTML preview'
    ) as HTMLIFrameElement;
    const token = frame.srcdoc.match(/const nonce = "([^"]+)"/)?.[1];
    act(() =>
      window.dispatchEvent(
        new MessageEvent('message', {
          data: { type: 'height', nonce: token, height: 777 },
          source: frame.contentWindow,
          origin: 'https://other.test',
        })
      )
    );
    expect(frame.style.height).not.toBe('777px');
    act(() => vi.advanceTimersByTime(5000));
    expect(screen.getByRole('alert')).toHaveTextContent(
      'Preview could not finish'
    );
  } finally {
    vi.useRealTimers();
  }
});
