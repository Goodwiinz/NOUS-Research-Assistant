import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { z } from 'zod';
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
