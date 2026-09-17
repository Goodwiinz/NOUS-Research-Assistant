/**
 * Unified API Client
 *
 * This is the consolidated API client that combines functionality from:
 * - api.ts (RAGAPIClient - basic API methods)
 * - apiClient.ts (ApiClient - axios-based with interceptors)
 * - typeSafeApiClient.ts (TypeSafeAPIClient - zod validation)
 *
 * Features:
 * - Consistent error handling
 * - Type-safe request/response with optional Zod validation
 * - Request/response interceptors
 * - Retry logic with exponential backoff
 * - File upload with progress tracking
 * - Token refresh and auth management
 */

import { API_CONFIG, APIErrorClass, DEFAULT_HEADERS } from '@/types/api';
import { parseErrorBody } from '@/utils/parseErrorBody';
import { ZodSchema } from 'zod';

// ============================================================================
// Types
// ============================================================================

export interface RequestConfig extends RequestInit {
  skipValidation?: boolean;
  retries?: number;
  retryDelay?: number;
  timeout?: number;
}

/**
 * Methods safe to auto-retry (R6-M15). A replayed POST that already committed
 * server-side duplicates the create — so non-idempotent verbs never get the
 * automatic retry pass. Callers that know a specific mutating endpoint is
 * idempotent can still opt in via an explicit `retries` count.
 */
const IDEMPOTENT_METHODS: ReadonlySet<string> = new Set([
  'GET',
  'HEAD',
  'OPTIONS',
]);

export interface TypedResponse<T> {
  data: T;
  status: number;
  headers: Headers;
}

export interface UploadOptions {
  onProgress?: (progress: number) => void;
  metadata?: Record<string, string>;
  signal?: AbortSignal;
}

export interface BinaryDownloadExpectation {
  contentType?: string;
  signature?: string;
}

function composeAbortSignals(signals: AbortSignal[]): {
  signal: AbortSignal;
  cleanup: () => void;
} {
  if (signals.length === 1) {
    return { signal: signals[0], cleanup: () => undefined };
  }

  const controller = new AbortController();
  const listeners = signals.map((source) => {
    const relayAbort = (): void => controller.abort(source.reason);
    if (source.aborted) {
      relayAbort();
    } else {
      source.addEventListener('abort', relayAbort, { once: true });
    }
    return { source, relayAbort };
  });

  return {
    signal: controller.signal,
    cleanup: () => {
      listeners.forEach(({ source, relayAbort }) => {
        source.removeEventListener('abort', relayAbort);
      });
    },
  };
}

// ============================================================================
// Unified API Client
// ============================================================================

export class APIClient {
  private baseURL: string;
  private token: string | null = null;
  private explicitToken: string | null = null;
  private defaultTimeout: number;

  constructor(baseURL: string = API_CONFIG.BASE_URL) {
    this.baseURL = baseURL;
    this.defaultTimeout = API_CONFIG.TIMEOUT_MS || 30000;
  }

  // --------------------------------------------------------------------------
  // Authentication
  // --------------------------------------------------------------------------

  setAuth(token: string): void {
    this.explicitToken = token;
    this.token = token;
  }

  clearAuth(): void {
    this.explicitToken = null;
    this.token = null;
  }

  /** Load auth from the CURRENT Supabase session before each request.
   *
   * Reads the session every call rather than caching the first token forever:
   * the old short-circuit (`if (this.token) return`) kept the very first
   * access token for the singleton's whole lifetime, so after a token refresh
   * the client sent a stale token and — worse — after sign-out (or a second
   * user logging in on the same tab) it kept sending the previous user's
   * still-valid JWT until a hard reload. An explicit token set via setAuth()
   * still wins; otherwise the live session (which getSession() auto-refreshes)
   * is the source of truth, and a null session clears the token rather than
   * stranding the old one.
   *
   * Deliberately getSession() (audit #7): this only reads a token to attach to
   * an API call the backend re-validates — it makes no authz decision here, so
   * getUser()'s extra network round-trip per request would add latency for no
   * security gain. The two authz-decision sites (authStore initialize/
   * fetchProfile) use getUser().
   */
  private async ensureAuth(): Promise<void> {
    if (this.explicitToken) {
      this.token = this.explicitToken;
      return;
    }
    if (typeof window === 'undefined') return;

    try {
      const { createClient } = await import('@/lib/supabase/client');
      const supabase = createClient();
      const {
        data: { session },
      } = await supabase.auth.getSession();
      this.token = session?.access_token ?? null;
    } catch (error) {
      console.warn('Failed to load auth from Supabase session:', error);
    }
  }

  private getHeaders(): HeadersInit {
    const headers: Record<string, string> = {
      ...DEFAULT_HEADERS,
    };

    if (this.token) {
      headers['Authorization'] = `Bearer ${this.token}`;
    }

    return headers;
  }

  // --------------------------------------------------------------------------
  // Core Request Methods
  // --------------------------------------------------------------------------

  async request<T>(endpoint: string, options: RequestConfig = {}): Promise<T> {
    // Ensure we have auth from Supabase session before making requests
    await this.ensureAuth();

    const {
      retries = API_CONFIG.RETRY_ATTEMPTS || 3,
      retryDelay = 1000,
      timeout = this.defaultTimeout,
      ...fetchOptions
    } = options;

    // Only idempotent verbs get the automatic retry pass — replaying a
    // committed POST would duplicate the create (R6-M15). A caller-supplied
    // `retries` count is an explicit opt-in and bypasses the verb check.
    const explicitRetries =
      options.retries !== undefined && options.retries !== null;
    const method = (fetchOptions.method ?? 'GET').toUpperCase();

    const url = endpoint.startsWith('http')
      ? endpoint
      : endpoint.startsWith('/api/')
        ? endpoint
        : `${this.baseURL}${endpoint}`;

    const controller = new AbortController();
    const callerSignal = fetchOptions.signal;
    const { signal, cleanup: cleanupAbortSignals } = composeAbortSignals(
      callerSignal ? [callerSignal, controller.signal] : [controller.signal]
    );
    const timeoutId = setTimeout(() => controller.abort(), timeout);

    try {
      const response = await fetch(url, {
        ...fetchOptions,
        headers: this.mergeHeaders(fetchOptions.headers, fetchOptions.body),
        signal,
      });

      clearTimeout(timeoutId);

      if (!response.ok) {
        const error = await this.handleErrorResponse(response);
        throw error;
      }

      // Handle empty responses
      const contentType = response.headers.get('content-type');
      if (!contentType || !contentType.includes('application/json')) {
        return {} as T;
      }

      return await response.json();
    } catch (error) {
      clearTimeout(timeoutId);

      // Caller cancellation is control flow, not a timeout or retryable error.
      if (callerSignal?.aborted) {
        throw error;
      }

      // Handle the API client's own timeout abort.
      if ((error as Error).name === 'AbortError') {
        throw new APIErrorClass({
          message: 'Request timeout',
          status_code: 408,
          type: 'http_error',
          details: { timeout },
        });
      }

      // Retry logic for network errors — idempotent methods or explicit
      // opt-in only (R6-M15)
      if (
        retries > 0 &&
        (explicitRetries || IDEMPOTENT_METHODS.has(method)) &&
        this.isRetryableError(error)
      ) {
        await this.delay(retryDelay);
        return this.request<T>(endpoint, {
          ...options,
          retries: retries - 1,
          retryDelay: retryDelay * 2, // Exponential backoff
        });
      }

      throw error;
    } finally {
      clearTimeout(timeoutId);
      cleanupAbortSignals();
    }
  }

  async requestWithValidation<T>(
    endpoint: string,
    schema: ZodSchema<T>,
    options: RequestConfig = {}
  ): Promise<TypedResponse<T>> {
    // Keep auth behavior consistent with request()
    await this.ensureAuth();

    const response = await fetch(
      endpoint.startsWith('http') ? endpoint : `${this.baseURL}${endpoint}`,
      {
        ...options,
        headers: this.mergeHeaders(options.headers, options.body),
      }
    );

    if (!response.ok) {
      throw await this.handleErrorResponse(response);
    }

    const rawData = await response.json();

    // Validate with Zod schema
    if (!options.skipValidation) {
      const result = schema.safeParse(rawData);
      if (!result.success) {
        throw new APIErrorClass({
          message: 'Response validation failed',
          status_code: 500,
          type: 'validation_error',
          details: { errors: result.error.issues },
        });
      }
      return {
        data: result.data,
        status: response.status,
        headers: response.headers,
      };
    }

    return {
      data: rawData,
      status: response.status,
      headers: response.headers,
    };
  }

  // --------------------------------------------------------------------------
  // HTTP Methods
  // --------------------------------------------------------------------------

  async get<T>(endpoint: string, options: RequestConfig = {}): Promise<T> {
    return this.request<T>(endpoint, { ...options, method: 'GET' });
  }

  async post<T>(
    endpoint: string,
    data?: unknown,
    options: RequestConfig = {}
  ): Promise<T> {
    return this.request<T>(endpoint, {
      ...options,
      method: 'POST',
      headers: this.bodyHeaders(data, options.headers),
      body: this.serializeBody(data),
    });
  }

  /**
   * POST for slow, non-idempotent operations (arXiv search/ingest/extract/track
   * can each take minutes). Uses a 5-minute timeout and disables automatic
   * retries: replaying a POST that already committed work server-side would
   * duplicate ingests/extractions. Callers may still override `timeout` /
   * `retries` explicitly via `options`.
   */
  async postWithLongTimeout<T>(
    endpoint: string,
    data?: unknown,
    options: RequestConfig = {}
  ): Promise<T> {
    return this.post<T>(endpoint, data, {
      timeout: 300000,
      retries: 0,
      ...options,
    });
  }

  async put<T>(
    endpoint: string,
    data?: unknown,
    options: RequestConfig = {}
  ): Promise<T> {
    return this.request<T>(endpoint, {
      ...options,
      method: 'PUT',
      headers: this.bodyHeaders(data, options.headers),
      body: this.serializeBody(data),
    });
  }

  async patch<T>(
    endpoint: string,
    data?: unknown,
    options: RequestConfig = {}
  ): Promise<T> {
    return this.request<T>(endpoint, {
      ...options,
      method: 'PATCH',
      headers: this.bodyHeaders(data, options.headers),
      body: this.serializeBody(data),
    });
  }

  /**
   * Build headers for a body-carrying request. FormData bodies must NOT get a
   * JSON Content-Type — the browser sets multipart/form-data with the boundary.
   */
  private bodyHeaders(data: unknown, extra?: HeadersInit): HeadersInit {
    const isFormData =
      typeof FormData !== 'undefined' && data instanceof FormData;
    return {
      ...(isFormData ? {} : { 'Content-Type': 'application/json' }),
      ...extra,
    };
  }

  /**
   * Pass FormData through untouched; JSON-encode everything else. Returns
   * undefined for empty (null/undefined) bodies. A non-serializable body
   * (circular ref, BigInt) is surfaced as an APIErrorClass so callers can
   * handle it uniformly instead of a raw TypeError leaking out.
   */
  private serializeBody(data: unknown): BodyInit | undefined {
    if (data === undefined || data === null) return undefined;
    if (typeof FormData !== 'undefined' && data instanceof FormData) {
      return data;
    }
    try {
      return JSON.stringify(data);
    } catch (error) {
      throw new APIErrorClass({
        message: `Failed to serialize request body: ${
          error instanceof Error ? error.message : 'unknown error'
        }`,
        status_code: 0,
        type: 'validation_error',
        details: {},
      });
    }
  }

  /**
   * Merge default + auth headers with caller overrides. For FormData bodies the
   * inherited JSON Content-Type (from DEFAULT_HEADERS) is stripped — the browser
   * must set multipart/form-data with the boundary itself, or the server rejects
   * the body with 422. The strip is case-insensitive so a caller-supplied
   * `content-type` can't leak through.
   */
  private mergeHeaders(
    extra: HeadersInit | undefined,
    body: BodyInit | null | undefined
  ): Record<string, string> {
    const merged: Record<string, string> = {
      ...(this.getHeaders() as Record<string, string>),
      ...(extra as Record<string, string>),
    };

    if (typeof FormData !== 'undefined' && body instanceof FormData) {
      for (const key of Object.keys(merged)) {
        if (key.toLowerCase() === 'content-type') {
          delete merged[key];
        }
      }
    }

    return merged;
  }

  async delete<T>(endpoint: string, options: RequestConfig = {}): Promise<T> {
    return this.request<T>(endpoint, { ...options, method: 'DELETE' });
  }

  // --------------------------------------------------------------------------
  // File Operations
  // --------------------------------------------------------------------------

  async upload<T>(
    endpoint: string,
    file: File,
    options: UploadOptions = {}
  ): Promise<T> {
    await this.ensureAuth();

    const formData = new FormData();
    formData.append('file', file);

    // Add any additional metadata
    if (options.metadata) {
      Object.entries(options.metadata).forEach(([key, value]) => {
        formData.append(key, value);
      });
    }

    // For progress tracking, we need to use XMLHttpRequest
    if (options.onProgress) {
      return this.uploadWithProgress<T>(
        endpoint,
        formData,
        options.onProgress,
        options.signal
      );
    }

    // Content-Type is intentionally not set here. request() strips the
    // application/json default (from DEFAULT_HEADERS) for FormData bodies so the
    // browser can set multipart/form-data with the boundary. Removing that strip
    // would silently break this upload path — an empty headers object alone does
    // NOT prevent the inherited JSON Content-Type.
    return this.request<T>(endpoint, {
      method: 'POST',
      body: formData,
      headers: {},
      signal: options.signal,
    });
  }

  private uploadWithProgress<T>(
    endpoint: string,
    formData: FormData,
    onProgress: (progress: number) => void,
    signal?: AbortSignal
  ): Promise<T> {
    return new Promise((resolve, reject) => {
      const xhr = new XMLHttpRequest();
      const url = endpoint.startsWith('http')
        ? endpoint
        : `${this.baseURL}${endpoint}`;

      xhr.open('POST', url);

      // Set auth headers
      if (this.token) {
        xhr.setRequestHeader('Authorization', `Bearer ${this.token}`);
      }

      xhr.upload.onprogress = (event) => {
        if (event.lengthComputable) {
          const progress = Math.round((event.loaded / event.total) * 100);
          onProgress(progress);
        }
      };

      xhr.onload = () => {
        if (xhr.status >= 200 && xhr.status < 300) {
          try {
            resolve(JSON.parse(xhr.responseText));
          } catch {
            resolve({} as T);
          }
        } else {
          // Parse the backend's structured error envelope (e.g. the
          // FileValidationError detail message) instead of discarding it in
          // favor of the generic xhr.statusText (R4-M24).
          let body: unknown = {};
          try {
            body = JSON.parse(xhr.responseText);
          } catch {
            // Response body may not be JSON
          }
          reject(
            this.toAPIError(body, xhr.status, xhr.statusText || 'Upload failed')
          );
        }
      };

      xhr.onerror = () => {
        reject(
          new APIErrorClass({
            message: 'Network error during upload',
            status_code: 0,
            type: 'http_error',
          })
        );
      };

      xhr.onabort = () => {
        reject(
          new APIErrorClass({
            message: 'Upload cancelled',
            status_code: 0,
            type: 'http_error',
          })
        );
      };

      // A bare {once:true} listener never fires on normal completion, so it
      // would hold the signal's reference to this xhr/formData alive for the
      // signal's whole lifetime. Remove it ourselves once the request settles.
      const onAbort = (): void => xhr.abort();
      xhr.onloadend = () => signal?.removeEventListener('abort', onAbort);

      if (signal?.aborted) {
        // xhr.abort() on an UNSENT request never fires onabort — reject
        // directly instead of relying on the event (mirrors composeAbortSignals'
        // already-aborted check above).
        reject(
          new APIErrorClass({
            message: 'Upload cancelled',
            status_code: 0,
            type: 'http_error',
          })
        );
        return;
      }
      signal?.addEventListener('abort', onAbort, { once: true });

      xhr.send(formData);
    });
  }

  async download(url: string, filename?: string): Promise<void> {
    await this.ensureAuth();

    const response = await fetch(
      url.startsWith('http') ? url : `${this.baseURL}${url}`,
      {
        headers: this.getHeaders(),
      }
    );

    if (!response.ok) {
      throw await this.handleErrorResponse(response);
    }

    const blob = await response.blob();
    const downloadUrl = window.URL.createObjectURL(blob);
    const link = document.createElement('a');
    link.href = downloadUrl;
    link.download = filename || this.extractFilename(response) || 'download';
    document.body.appendChild(link);
    link.click();
    document.body.removeChild(link);
    window.URL.revokeObjectURL(downloadUrl);
  }

  /**
   * Authenticated POST that streams a blob back and triggers a browser save.
   * Mirrors {@link download} but for POST endpoints (the thread-export route
   * is POST with format/options carried as query params). An optional JSON
   * `body` is sent when provided; otherwise the request body is empty.
   */
  async downloadPost(
    url: string,
    filename?: string,
    body?: unknown,
    expectation?: BinaryDownloadExpectation
  ): Promise<void> {
    await this.ensureAuth();

    const response = await fetch(
      url.startsWith('http') ? url : `${this.baseURL}${url}`,
      {
        method: 'POST',
        headers: this.getHeaders(),
        body: body !== undefined ? JSON.stringify(body) : undefined,
      }
    );

    if (!response.ok) {
      throw await this.handleErrorResponse(response);
    }

    const blob = await response.blob();
    if (
      expectation &&
      !(await this.matchesDownloadExpectation(response, blob, expectation))
    ) {
      throw new APIErrorClass({
        message: 'Export response did not match the requested format',
        status_code: 502,
        type: 'http_error',
      });
    }

    const downloadUrl = window.URL.createObjectURL(blob);
    const link = document.createElement('a');
    link.href = downloadUrl;
    link.download = this.extractFilename(response) || filename || 'download';
    document.body.appendChild(link);
    link.click();
    document.body.removeChild(link);
    window.URL.revokeObjectURL(downloadUrl);
  }

  private async matchesDownloadExpectation(
    response: Response,
    blob: Blob,
    expectation: BinaryDownloadExpectation
  ): Promise<boolean> {
    if (expectation.contentType) {
      const actualContentType = (
        response.headers.get('content-type') ||
        blob.type ||
        ''
      )
        .split(';', 1)[0]
        .trim()
        .toLowerCase();
      if (actualContentType !== expectation.contentType.toLowerCase()) {
        return false;
      }
    }

    if (expectation.signature) {
      const expected = new TextEncoder().encode(expectation.signature);
      const actual = new Uint8Array(
        await this.readBlobBytes(blob.slice(0, expected.byteLength))
      );
      if (
        actual.byteLength < expected.byteLength ||
        expected.some((value, index) => actual[index] !== value)
      ) {
        return false;
      }
    }

    return true;
  }

  private readBlobBytes(blob: Blob): Promise<ArrayBuffer> {
    const blobWithArrayBuffer = blob as Blob & {
      arrayBuffer?: () => Promise<ArrayBuffer>;
    };
    if (blobWithArrayBuffer.arrayBuffer) {
      return blobWithArrayBuffer.arrayBuffer();
    }

    return new Promise((resolve, reject) => {
      const reader = new FileReader();
      reader.onload = () => resolve(reader.result as ArrayBuffer);
      reader.onerror = () =>
        reject(reader.error || new Error('Unable to inspect download bytes'));
      reader.readAsArrayBuffer(blob);
    });
  }

  /**
   * Fetch a file as an authenticated blob and return an object URL for inline
   * rendering (e.g. a PDF/image preview). Unlike download(), this does not
   * trigger a save dialog. Follows the backend's 302 redirect to a signed
   * storage URL transparently. The caller owns the returned objectUrl and MUST
   * call URL.revokeObjectURL(objectUrl) when done to avoid leaking memory.
   */
  async fetchObjectUrl(
    url: string
  ): Promise<{ objectUrl: string; contentType: string }> {
    await this.ensureAuth();

    const response = await fetch(
      url.startsWith('http') ? url : `${this.baseURL}${url}`,
      {
        headers: this.getHeaders(),
      }
    );

    if (!response.ok) {
      throw await this.handleErrorResponse(response);
    }

    const blob = await response.blob();
    return {
      objectUrl: window.URL.createObjectURL(blob),
      contentType: response.headers.get('content-type') || blob.type || '',
    };
  }

  // --------------------------------------------------------------------------
  // Helper Methods
  // --------------------------------------------------------------------------

  private async handleErrorResponse(
    response: Response
  ): Promise<APIErrorClass> {
    let errorData: unknown = {};

    try {
      errorData = await response.json();
    } catch {
      // Response body may not be JSON
    }

    return this.toAPIError(errorData, response.status, response.statusText);
  }

  // Shared by handleErrorResponse (fetch path) and uploadWithProgress's XHR
  // error branch (R4-M24) — both need the same envelope-parsing → APIErrorClass
  // construction, just from a differently-sourced raw body.
  private toAPIError(
    body: unknown,
    statusCode: number,
    statusText: string
  ): APIErrorClass {
    // The backend rewrites every error into the structured envelope
    // `{ error: { message, status_code, type, details? } }`. Parse it so the
    // user sees the real cause, and surface `type` so callers can branch on
    // auth_error / rate_limit instead of only the raw HTTP status text.
    const parsed = parseErrorBody(body, statusText);
    const rawDetails =
      typeof body === 'object' && body !== null
        ? (body as Record<string, unknown>)
        : undefined;

    return new APIErrorClass({
      message: parsed.message,
      status_code: statusCode,
      type: parsed.type ?? 'http_error',
      details: parsed.details ?? rawDetails,
      ...(parsed.silent !== undefined ? { silent: parsed.silent } : {}),
    });
  }

  private isRetryableError(error: unknown): boolean {
    if (error instanceof APIErrorClass) {
      const statusCode = error.error.status_code;
      // Don't retry client errors (4xx except 429)
      if (statusCode >= 400 && statusCode < 500 && statusCode !== 429) {
        return false;
      }
      // Retry server errors (5xx) and rate limiting (429)
      return statusCode >= 500 || statusCode === 429;
    }
    // Retry network errors
    return true;
  }

  private delay(ms: number): Promise<void> {
    return new Promise((resolve) => setTimeout(resolve, ms));
  }

  private extractFilename(response: Response): string | null {
    const disposition = response.headers.get('content-disposition');
    if (disposition) {
      const match = disposition.match(/filename="?([^";\n]+)"?/);
      if (match) return match[1];
    }
    return null;
  }
}

// ============================================================================
// Singleton Instance
// ============================================================================

let apiClient: APIClient | null = null;

export function getAPIClient(): APIClient {
  if (!apiClient) {
    apiClient = new APIClient();
  }
  return apiClient;
}

export function initializeAPIClient(baseURL?: string): APIClient {
  apiClient = new APIClient(baseURL);
  return apiClient;
}

// ============================================================================
// Default Export
// ============================================================================

export const api = getAPIClient();
export default APIClient;
