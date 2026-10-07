import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { z } from 'zod';
import { Blob as NativeBlob } from 'node:buffer';
import { APIClient } from '../api-client';
import { resetAccountSession } from '@/lib/account-session';

const auth = vi.hoisted(() => ({ getSession: vi.fn() }));
vi.mock('@/lib/supabase/client', () => ({ createClient: () => ({ auth }) }));

function deferred<T>(): { promise: Promise<T>; resolve: (value: T) => void } {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((yes) => {
    resolve = yes;
  });
  return { promise, resolve };
}
let client: APIClient;
const fetchMock = vi.fn();
beforeEach(() => {
  resetAccountSession();
  client = new APIClient('http://api.test');
  auth.getSession.mockResolvedValue({
    data: { session: { access_token: 'A-token' } },
  });
  fetchMock.mockReset().mockResolvedValue({
    ok: true,
    headers: new Headers(),
    json: async () => ({}),
  });
  vi.stubGlobal('fetch', fetchMock);
  vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(() => {});
  vi.stubGlobal(
    'URL',
    Object.assign(URL, {
      createObjectURL: vi.fn(() => 'blob:A'),
      revokeObjectURL: vi.fn(),
    })
  );
});
afterEach(() => {
  vi.useRealTimers();
  vi.unstubAllGlobals();
});

describe('API account cancellation', () => {
  it('discards an export when the account changes during signature inspection', async () => {
    const bytes = deferred<ArrayBuffer>();
    const blob = new NativeBlob(['%PDF-1.7 A-only'], {
      type: 'application/pdf',
    });
    const prefix = blob.slice(0, 5);
    const read = vi
      .spyOn(prefix, 'arrayBuffer')
      .mockReturnValueOnce(bytes.promise);
    vi.spyOn(blob, 'slice').mockReturnValueOnce(prefix);
    fetchMock.mockResolvedValueOnce({
      ok: true,
      headers: new Headers({ 'content-type': 'application/pdf' }),
      blob: async () => blob,
    });

    const exported = client
      .downloadPost('/export', 'private.pdf', undefined, {
        contentType: 'application/pdf',
        signature: '%PDF-',
      })
      .catch((error: Error) => error);
    await vi.waitFor(() => expect(read).toHaveBeenCalledOnce());
    resetAccountSession();
    bytes.resolve(new TextEncoder().encode('%PDF-').buffer);

    expect(await exported).toMatchObject({ name: 'AbortError' });
    expect(URL.createObjectURL).not.toHaveBeenCalled();
    expect(HTMLAnchorElement.prototype.click).not.toHaveBeenCalled();
  });

  it('cancels validated A work without cancelling a new B request', async () => {
    const oldBody = deferred<unknown>();
    const newBody = deferred<unknown>();
    const readOld = vi.fn(() => oldBody.promise);
    const readNew = vi.fn(() => newBody.promise);
    const schema = z.object({ owner: z.string() });
    const oldCaller = new AbortController();
    const newCaller = new AbortController();
    fetchMock
      .mockResolvedValueOnce({
        ok: true,
        status: 200,
        headers: new Headers(),
        json: readOld,
      })
      .mockResolvedValueOnce({
        ok: true,
        status: 200,
        headers: new Headers(),
        json: readNew,
      });
    const old = client
      .requestWithValidation('http://api.test/A', schema, {
        signal: oldCaller.signal,
      })
      .catch((error: Error) => error);
    await vi.waitFor(() => expect(readOld).toHaveBeenCalledOnce());

    resetAccountSession();
    client.setAuth('B-token');
    const current = client.requestWithValidation('/B', schema, {
      signal: newCaller.signal,
    });
    await vi.waitFor(() => expect(readNew).toHaveBeenCalledOnce());
    oldCaller.abort();
    oldBody.resolve({ owner: 'A-only' });
    newBody.resolve({ owner: 'B-only' });

    expect(await old).toMatchObject({ name: 'AbortError' });
    expect(await current).toMatchObject({
      data: { owner: 'B-only' },
      status: 200,
    });
    expect(fetchMock.mock.calls[0][1].signal.aborted).toBe(true);
    expect(fetchMock.mock.calls[1][1].signal.aborted).toBe(false);
    expect(fetchMock.mock.calls[1][1].headers.Authorization).toBe(
      'Bearer B-token'
    );
  });

  it.each(['request', 'validation', 'blob', 'preview', 'download', 'export'])(
    'aborts %s and discards a body that finishes after the account changes',
    async (kind) => {
      const body = deferred<unknown>();
      const read = vi.fn(() => body.promise);
      fetchMock.mockResolvedValueOnce({
        ok: true,
        status: 200,
        headers: new Headers({ 'content-type': 'application/json' }),
        json: read,
        blob: read,
      });
      const operations = {
        request: () => client.get('/private'),
        validation: () => client.requestWithValidation('/private', z.unknown()),
        blob: () => client.fetchBlob('/private'),
        preview: () => client.fetchObjectUrl('/private'),
        download: () => client.download('/private'),
        export: () => client.downloadPost('/private'),
      };
      const result = operations[kind as keyof typeof operations]().catch(
        (error: Error) => error
      );
      await vi.waitFor(() => expect(read).toHaveBeenCalledOnce());
      const signal = fetchMock.mock.calls[0][1].signal as
        AbortSignal | undefined;
      resetAccountSession();
      body.resolve(
        kind === 'request' || kind === 'validation'
          ? { text: 'A-only' }
          : new Blob(['A-only'])
      );
      expect(await result).toMatchObject({ name: 'AbortError' });
      expect(signal?.aborted).toBe(true);
      expect(URL.createObjectURL).not.toHaveBeenCalled();
    }
  );

  it('does not start a request with a delayed A token after B signs in', async () => {
    const session = deferred<unknown>();
    auth.getSession.mockReturnValueOnce(session.promise);
    fetchMock.mockResolvedValue({
      ok: true,
      headers: new Headers(),
      json: async () => ({}),
    });
    const old = client.get('/A').catch((error: Error) => error);
    await vi.waitFor(() => expect(auth.getSession).toHaveBeenCalledOnce());
    resetAccountSession();
    client.setAuth('B-token');
    session.resolve({ data: { session: { access_token: 'A-token' } } });
    expect(await old).toMatchObject({ name: 'AbortError' });
    expect(fetchMock).not.toHaveBeenCalled();
    await client.get('/B');
    expect(fetchMock.mock.calls[0][1].headers.Authorization).toBe(
      'Bearer B-token'
    );
  });

  it('does not retry A work with B credentials after a backoff', async () => {
    vi.useFakeTimers();
    client.setAuth('A-token');
    fetchMock.mockRejectedValueOnce(new TypeError('network failure'));
    const old = client
      .get('/A', { retryDelay: 100 })
      .catch((error: Error) => error);
    await vi.advanceTimersByTimeAsync(1);
    expect(fetchMock).toHaveBeenCalledOnce();
    resetAccountSession();
    client.setAuth('B-token');
    await vi.advanceTimersByTimeAsync(100);
    expect(await old).toMatchObject({ name: 'AbortError' });
    expect(fetchMock).toHaveBeenCalledOnce();
  });

  it('does not send an upload cancelled while credentials are loading', async () => {
    const session = deferred<unknown>();
    auth.getSession.mockReturnValueOnce(session.promise);
    const caller = new AbortController();
    const progress = vi.fn();
    const xhr = {
      upload: {},
      open: vi.fn(),
      setRequestHeader: vi.fn(),
      status: 200,
      responseText: '{"text":"A-only"}',
      onload: () => {},
      send: vi.fn(() => xhr.onload()),
    };
    vi.stubGlobal(
      'XMLHttpRequest',
      vi.fn(function () {
        return xhr;
      })
    );
    const upload = client
      .upload('/A', new File(['A'], 'a.txt'), {
        signal: caller.signal,
        onProgress: progress,
      })
      .catch((error: Error) => error);
    await vi.waitFor(() => expect(auth.getSession).toHaveBeenCalledOnce());
    caller.abort();
    session.resolve({ data: { session: { access_token: 'A-token' } } });
    await vi.waitFor(() => expect(xhr.open).toHaveBeenCalledOnce());
    expect(xhr.send).not.toHaveBeenCalled();

    expect(await upload).toMatchObject({
      name: 'APIError',
      message: 'Upload cancelled',
    });
    expect(progress).not.toHaveBeenCalled();
  });

  it('aborts uploads and ignores buffered progress and load events', async () => {
    class FakeXHR {
      upload = { onprogress: (_: ProgressEvent): void => {} };
      onload = (): void => {};
      onabort = (): void => {};
      onloadend = (): void => {};
      status = 200;
      responseText = '{"text":"A-only"}';
      open(): void {}
      setRequestHeader(): void {}
      send = vi.fn();
      abort = vi.fn(() => {
        this.onabort();
        this.onloadend();
      });
    }
    const xhr = new FakeXHR();
    vi.stubGlobal(
      'XMLHttpRequest',
      vi.fn(function (): FakeXHR {
        return xhr;
      })
    );
    const progress = vi.fn();
    const old = client
      .upload('/A', new File(['A'], 'a.txt'), { onProgress: progress })
      .catch((error: Error) => error);
    await vi.waitFor(() => expect(xhr.send).toHaveBeenCalledOnce());
    resetAccountSession();
    xhr.upload.onprogress({
      lengthComputable: true,
      loaded: 1,
      total: 1,
    } as ProgressEvent);
    xhr.onload();
    expect(await old).toMatchObject({ name: 'AbortError' });
    expect(xhr.abort).toHaveBeenCalledOnce();
    expect(progress).not.toHaveBeenCalled();
  });
});
