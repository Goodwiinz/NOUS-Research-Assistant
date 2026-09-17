import { randomUUID } from 'node:crypto';

import { redactText, sanitizeRequestObservation } from './report.mjs';

const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i;
const SAFE_PATH = /^\/(?:[^\s?#]|%[0-9a-f]{2})*(?:\?[^\s#]*)?$/i;
const MAX_RESPONSE_BYTES = 5 * 1024 * 1024;
const MAX_SSE_BYTES = 2 * 1024 * 1024;

export class FixtureOwnershipError extends Error {
  constructor(kind, id) {
    super(`Refusing to mutate unowned ${kind} fixture ${id}`);
    this.name = 'FixtureOwnershipError';
    this.kind = kind;
    this.id = id;
  }
}

export class QASessionError extends Error {
  constructor(message, details = {}) {
    super(message);
    this.name = 'QASessionError';
    Object.assign(this, details);
  }
}

export class HttpError extends QASessionError {
  constructor(message, details = {}) {
    super(message, details);
    this.name = 'HttpError';
  }
}

/** Exact resource ledger for one campaign. There is no title/prefix lookup. */
export class FixtureLedger {
  constructor(runId = randomUUID()) {
    this.runId = String(runId);
    this.resourcesByKey = new Map();
  }

  register(kind, id, metadata = {}) {
    if (!kind || typeof kind !== 'string') throw new TypeError('fixture kind is required');
    if (!id || typeof id !== 'string') throw new TypeError('fixture id is required');
    const key = `${kind}:${id}`;
    if (this.resourcesByKey.has(key)) {
      throw new Error(`Fixture already registered: ${key}`);
    }
    const resource = Object.freeze({
      kind,
      id,
      runId: this.runId,
      metadata: { ...metadata },
      registeredAt: new Date().toISOString(),
    });
    this.resourcesByKey.set(key, resource);
    return resource;
  }

  owns(kind, id) {
    return this.resourcesByKey.has(`${kind}:${id}`);
  }

  requireOwned(kind, id) {
    const resource = this.resourcesByKey.get(`${kind}:${id}`);
    if (!resource) throw new FixtureOwnershipError(kind, id);
    return resource;
  }

  list() {
    return [...this.resourcesByKey.values()];
  }

  async deleteOwned(kind, id, deleter) {
    const resource = this.requireOwned(kind, id);
    if (typeof deleter !== 'function') throw new TypeError('fixture deleter is required');
    await deleter(resource);
    this.resourcesByKey.delete(`${kind}:${id}`);
    return resource;
  }
}

function originOf(value) {
  try {
    return new URL(value).origin;
  } catch {
    return null;
  }
}

function extractTokenValue(value, depth = 0) {
  if (depth > 5 || value === null || value === undefined) return null;
  if (Array.isArray(value)) {
    for (const item of value) {
      const found = extractTokenValue(item, depth + 1);
      if (found) return found;
    }
    return null;
  }
  if (typeof value !== 'object') return null;
  for (const [key, item] of Object.entries(value)) {
    if (key === 'access_token' && typeof item === 'string' && item.length > 20) return item;
    const found = extractTokenValue(item, depth + 1);
    if (found) return found;
  }
  return null;
}

const SUPABASE_AUTH_COOKIE = /^sb-[a-z0-9-]+-auth-token(?:\.(\d+))?$/i;

function trustedCookieDomain(cookie, trustedHost) {
  if (!cookie?.domain) return true;
  const domain = String(cookie.domain).replace(/^\./, '').toLowerCase();
  const host = String(trustedHost).toLowerCase();
  return domain === host || host.endsWith(`.${domain}`);
}

function decodeCookieText(value) {
  if (typeof value !== 'string' || value.length === 0) return [];
  const candidates = [];
  try {
    candidates.push(decodeURIComponent(value));
  } catch {
    // A malformed cookie is ignored; it must never become a request header.
  }
  if (!candidates.includes(value)) candidates.push(value);
  return candidates;
}

function parseSupabaseCookieValue(value) {
  for (const candidate of decodeCookieText(value)) {
    try {
      const parsed = JSON.parse(candidate);
      const token = extractTokenValue(parsed);
      if (token) return token;
    } catch {
      // Supabase SSR uses a base64-prefixed JSON value. Try the decoded JSON
      // only after direct JSON so malformed or unrelated cookies are ignored.
    }
    if (!candidate.startsWith('base64-')) continue;
    try {
      const encoded = candidate.slice('base64-'.length).replace(/-/g, '+').replace(/_/g, '/');
      const padded = encoded.padEnd(Math.ceil(encoded.length / 4) * 4, '=');
      const decoded = Buffer.from(padded, 'base64').toString('utf8');
      const parsed = JSON.parse(decoded);
      const token = extractTokenValue(parsed);
      if (token) return token;
    } catch {
      // Cookie chunks can be incomplete or malformed; continue to the next.
    }
  }
  return null;
}

function tokenFromSupabaseCookies(cookies, baseUrl) {
  if (!Array.isArray(cookies)) return null;
  let trustedHost;
  try {
    trustedHost = new URL(baseUrl).hostname;
  } catch {
    return null;
  }
  const groups = new Map();
  for (const cookie of cookies) {
    if (!trustedCookieDomain(cookie, trustedHost)) continue;
    const match = SUPABASE_AUTH_COOKIE.exec(String(cookie?.name ?? ''));
    if (!match || typeof cookie?.value !== 'string') continue;
    const baseName = String(cookie.name).replace(/\.\d+$/, '');
    const index = match[1] === undefined ? null : Number(match[1]);
    if (index !== null && (!Number.isSafeInteger(index) || index < 0)) continue;
    if (!groups.has(baseName)) groups.set(baseName, []);
    groups.get(baseName).push({ index, value: cookie.value });
  }

  for (const chunks of groups.values()) {
    const unchunked = chunks.find((chunk) => chunk.index === null);
    let value;
    if (unchunked) {
      value = unchunked.value;
    } else {
      chunks.sort((left, right) => left.index - right.index);
      if (chunks.some((chunk, index) => chunk.index !== index)) continue;
      value = chunks.map((chunk) => chunk.value).join('');
    }
    const token = parseSupabaseCookieValue(value);
    if (token) return token;
  }
  return null;
}

async function readBoundedBody(response, maxBytes) {
  const requested = Number.isFinite(maxBytes) ? maxBytes : MAX_RESPONSE_BYTES;
  const limit = Math.min(MAX_RESPONSE_BYTES, Math.max(1, requested));
  if (!response.body) {
    const raw = new Uint8Array(await response.arrayBuffer());
    if (raw.byteLength > limit) throw new QASessionError('Response body exceeded the bounded limit');
    return raw;
  }
  const reader = response.body.getReader();
  const chunks = [];
  let total = 0;
  try {
    while (true) {
      const next = await reader.read();
      if (next.done) break;
      const chunk = next.value instanceof Uint8Array ? next.value : new Uint8Array(next.value);
      total += chunk.byteLength;
      if (total > limit) {
        await reader.cancel().catch(() => {});
        throw new QASessionError('Response body exceeded the bounded limit');
      }
      chunks.push(chunk);
    }
  } finally {
    reader.releaseLock();
  }
  const output = new Uint8Array(total);
  let offset = 0;
  for (const chunk of chunks) {
    output.set(chunk, offset);
    offset += chunk.byteLength;
  }
  return output;
}

export class QASession {
  constructor(config, dependencies = {}) {
    this.config = config;
    this.secrets = Array.isArray(config.secrets) ? config.secrets : [];
    this.ledger = new FixtureLedger(config.runId);
    this.observations = [];
    this.browser = null;
    this.context = null;
    this.page = null;
    this.closed = false;
    this.quarantined = false;
    this.quarantinePromise = null;
    this.quarantineErrors = [];
    this.dependencies = dependencies;
    this.trustedBaseOrigin = originOf(config.baseUrl);
    this.trustedApiOrigin = originOf(config.apiUrl);
    this._authToken = null;
    this.activeControllers = new Set();
    this.activeRuns = new Map();
  }

  assertOperational({ internal = false } = {}) {
    if (internal) return;
    if (this.closed) throw new QASessionError('QA session is closed');
    if (this.quarantined) throw new QASessionError('Scenario timed out; QA session is quarantined');
  }

  get fixturePrefix() {
    return `NOUS QA ${this.config.runId}`;
  }

  get hasCredentials() {
    return Boolean(this.config.credentials?.email && this.config.credentials?.password);
  }

  get hasStorageState() {
    return Boolean(this.config.storageState);
  }

  async openBrowser() {
    this.assertOperational();
    if (this.page) return this.page;
    const playwright = this.dependencies.playwright ?? (await import('playwright'));
    const chromium = playwright.chromium ?? playwright.default?.chromium;
    if (!chromium) throw new QASessionError('Playwright Chromium is unavailable');
    this.browser = await chromium.launch({ headless: true });
    const contextOptions = { baseURL: this.config.baseUrl };
    if (this.config.storageState) contextOptions.storageState = this.config.storageState;
    this.context = await this.browser.newContext(contextOptions);
    this.page = await this.context.newPage();
    return this.page;
  }

  assertTrustedBrowserUrl(value) {
    const origin = originOf(value);
    if (!origin || origin !== this.trustedBaseOrigin) {
      throw new QASessionError('Browser navigation left the configured target origin');
    }
  }

  async goto(path, options = {}) {
    this.assertOperational();
    const page = await this.openBrowser();
    const target = new URL(path, `${this.config.baseUrl}/`);
    this.assertTrustedBrowserUrl(target.toString());
    let response;
    try {
      response = await page.goto(target.toString(), {
        waitUntil: options.waitUntil ?? 'domcontentloaded',
        timeout: options.timeoutMs ?? this.config.timeoutMs,
      });
    } catch (error) {
      throw new QASessionError(redactText(error?.message ?? error, this.secrets));
    }
    this.assertTrustedBrowserUrl(page.url());
    return response;
  }

  async login() {
    this.assertOperational();
    if (!this.hasCredentials && !this.hasStorageState) {
      throw new QASessionError('Authenticated scenario requires credentials or --storage-state');
    }
    const page = await this.openBrowser();
    if (this.hasStorageState) {
      await this.goto('/dashboard');
      if (/\/login(?:$|[?#])/.test(new URL(page.url()).pathname)) {
        throw new QASessionError('Storage state did not reach a protected route');
      }
      return page;
    }
    await this.goto('/login');
    // Verify the origin again immediately before entering credentials. A
    // compromised redirect must never receive owner credentials.
    this.assertTrustedBrowserUrl(page.url());
    await page.locator('#email').fill(this.config.credentials.email);
    await page.locator('#password').fill(this.config.credentials.password);
    this.assertTrustedBrowserUrl(page.url());
    await page.locator('form').getByRole('button', { name: /sign in/i }).click();
    try {
      // Login is an async SPA mutation; waiting for DOMContentLoaded alone can
      // resolve immediately while the form is still on /login.
      await page.waitForURL((url) => !/\/login(?:$|[?#])/.test(url.pathname), { timeout: this.config.timeoutMs });
    } catch {
      throw new QASessionError('Login did not reach a protected route');
    }
    await page.waitForLoadState('domcontentloaded', { timeout: this.config.timeoutMs }).catch(() => {});
    this.assertTrustedBrowserUrl(page.url());
    if (/\/login(?:$|[?#])/.test(new URL(page.url()).pathname)) {
      throw new QASessionError('Login did not complete on the configured target');
    }
    return page;
  }

  async readBrowserAuthToken(page, context) {
    if (page) {
      const values = await page.evaluate(() => {
        const entries = [];
        for (let index = 0; index < localStorage.length; index += 1) {
          const key = localStorage.key(index);
          if (!key) continue;
          entries.push(localStorage.getItem(key));
        }
        return entries;
      });
      for (const raw of values) {
        try {
          const token = extractTokenValue(JSON.parse(raw));
          if (token) {
            this._authToken = token;
            return token;
          }
        } catch {
          // Supabase stores JSON; unrelated localStorage entries are ignored.
        }
      }
    }
    if (!context) return null;
    // Supabase SSR stores sessions in chunked sb-*-auth-token cookies. Ask
    // Playwright for cookies applicable to the configured frontend URL and
    // apply a domain check again before decoding; API-origin cookies and
    // untrusted redirects must never become bearer credentials.
    this.assertTrustedBrowserUrl(this.config.baseUrl);
    const cookies = await context.cookies(this.config.baseUrl);
    return tokenFromSupabaseCookies(cookies, this.config.baseUrl);
  }

  async browserAuthToken(options = {}) {
    this.assertOperational(options);
    if (this._authToken) return this._authToken;
    const token = await this.readBrowserAuthToken(this.page, this.context);
    if (token) this._authToken = token;
    return token;
  }

  urlFor(path, target = 'api', options = {}) {
    this.assertOperational(options);
    if (typeof path !== 'string' || !SAFE_PATH.test(path)) {
      throw new QASessionError('Request path must be a relative path without credentials');
    }
    const base = target === 'frontend'
      ? this.config.baseUrl
      : target === 'backend'
        ? `${new URL(this.config.apiUrl).origin}`
        : this.config.apiUrl;
    const parsed = new URL(path, `${base}/`);
    const allowedOrigin = target === 'frontend' ? this.trustedBaseOrigin : this.trustedApiOrigin;
    if (parsed.origin !== allowedOrigin) throw new QASessionError('Request target origin is not trusted');
    return parsed.toString();
  }

  async request(path, options = {}) {
    const internal = options.internal === true;
    this.assertOperational({ internal });
    const target = options.target ?? 'api';
    const url = this.urlFor(path, target, { internal });
    const method = String(options.method ?? 'GET').toUpperCase();
    const controller = new AbortController();
    this.activeControllers.add(controller);
    const timeoutId = setTimeout(() => controller.abort(), options.timeoutMs ?? this.config.timeoutMs);
    const signal = options.signal
      ? AbortSignal.any([options.signal, controller.signal])
      : controller.signal;
    const headers = new Headers(options.headers ?? {});
    if (options.json !== undefined) {
      headers.set('content-type', 'application/json');
    }
    const started = Date.now();
    const release = () => {
      clearTimeout(timeoutId);
      this.activeControllers.delete(controller);
    };
    let response;
    try {
      const token = options.forwardAuth === false
        ? null
        : (options.internal ? this._authToken : (await this.browserAuthToken({ internal }))) ?? this.config.authToken;
      if (options.internal && options.forwardAuth !== false && !token) {
        throw new HttpError('Authenticated cleanup requires an API token', { status: 401 });
      }
      if (token) headers.set('authorization', `Bearer ${token}`);
      this.assertOperational({ internal });
      // Manual redirects ensure an API response cannot move a bearer token to
      // an untrusted origin. A redirect is reported as a bounded failure.
      response = await fetch(url, {
        method,
        headers,
        body: options.json === undefined ? options.body : JSON.stringify(options.json),
        signal,
        redirect: 'manual',
      });
    } catch (error) {
      const observation = sanitizeRequestObservation({ method, path, status: null, durationMs: Date.now() - started }, this.secrets);
      this.observations.push(observation);
      release();
      if (error?.name === 'AbortError') throw new HttpError(`Request timed out: ${method} ${path}`, { status: 408 });
      throw new HttpError(redactText(error?.message ?? error, this.secrets), { status: null });
    }
    try {
      this.observations.push(sanitizeRequestObservation({ method, path, status: response.status, durationMs: Date.now() - started }, this.secrets));
      const contentType = response.headers.get('content-type') ?? '';
      const bytes = await readBoundedBody(response, options.maxResponseBytes);
      const text = new TextDecoder().decode(bytes.slice(0, 32_000));
      let data = null;
      if (contentType.includes('json')) {
        try { data = JSON.parse(text); } catch { data = null; }
      }
      if (response.status >= 300 && response.status < 400) {
        throw new HttpError(`Request redirected (${response.status})`, { status: response.status });
      }
      if (!response.ok) {
        // Keep response bodies out of errors and reports. Scenarios assert
        // status contracts; backend detail may contain account or document
        // content unrelated to the QA fixture.
        throw new HttpError(`Request failed (${response.status})`, { status: response.status });
      }
      return { status: response.status, headers: response.headers, data, text, bytes };
    } catch (error) {
      if (error?.name === 'AbortError') {
        throw new HttpError(`Request body timed out: ${method} ${path}`, { status: 408 });
      }
      throw error;
    } finally {
      release();
    }
  }

  async streamAgent(payload, options = {}) {
    this.assertOperational();
    const path = '/api/v1/agent/stream';
    const url = this.urlFor(path, 'backend');
    const controller = new AbortController();
    this.activeControllers.add(controller);
    const timeoutId = setTimeout(() => controller.abort(), options.timeoutMs ?? this.config.timeoutMs);
    const signal = options.signal
      ? AbortSignal.any([options.signal, controller.signal])
      : controller.signal;
    const headers = new Headers({ 'content-type': 'application/json' });
    const started = Date.now();
    const release = () => {
      clearTimeout(timeoutId);
      this.activeControllers.delete(controller);
    };
    let response;
    try {
      const token = (await this.browserAuthToken()) ?? this.config.authToken;
      if (token) headers.set('authorization', `Bearer ${token}`);
      this.assertOperational();
      response = await fetch(url, {
        method: 'POST',
        headers,
        body: JSON.stringify(payload),
        signal,
        redirect: 'manual',
      });
    } catch (error) {
      this.observations.push(sanitizeRequestObservation({ method: 'POST', path, status: null, durationMs: Date.now() - started }, this.secrets));
      release();
      if (error?.name === 'AbortError') throw new HttpError('Agent stream timed out or was aborted', { status: 408 });
      throw new HttpError(redactText(error?.message ?? error, this.secrets));
    }
    this.observations.push(sanitizeRequestObservation({ method: 'POST', path, status: response.status, durationMs: Date.now() - started }, this.secrets));
    let reader = null;
    try {
      if (!response.ok || !response.body) {
        throw new HttpError(`Agent stream failed (${response.status})`, { status: response.status });
      }
      const events = [];
      reader = response.body.getReader();
      const decoder = new TextDecoder();
      let buffer = '';
      let currentEvent = '';
      let terminal = false;
      let streamBytes = 0;
      while (!terminal) {
        const chunk = await reader.read();
        if (chunk.done) break;
        streamBytes += chunk.value.byteLength;
        if (streamBytes > MAX_SSE_BYTES) {
          await reader.cancel().catch(() => {});
          throw new QASessionError('Agent stream exceeded the bounded limit');
        }
        buffer += decoder.decode(chunk.value, { stream: true });
        const lines = buffer.split('\n');
        buffer = lines.pop() ?? '';
        for (const raw of lines) {
          const line = raw.trim();
          if (!line) {
            currentEvent = '';
            continue;
          }
          if (line.startsWith('event: ')) currentEvent = line.slice(7).trim();
          if (line.startsWith('data: ') && currentEvent) {
            let data;
            try {
              data = JSON.parse(line.slice(6));
            } catch {
              // Malformed events are evidence of a failed stream, not raw data
              // to preserve in the report.
              throw new QASessionError(`Malformed SSE event: ${currentEvent}`);
            }
            const parsedEvent = { event: currentEvent, data };
            events.push(parsedEvent);
            if (typeof options.onEvent === 'function') await options.onEvent(parsedEvent);
            if (['done', 'error', 'confirmation'].includes(currentEvent)) terminal = true;
          }
        }
      }
      return { status: response.status, events, terminal };
    } catch (error) {
      if (error?.name === 'AbortError') throw new HttpError('Agent stream timed out or was aborted', { status: 408 });
      throw error;
    } finally {
      reader?.releaseLock();
      release();
    }
  }

  registerFixture(kind, id, metadata = {}) {
    this.assertOperational();
    return this.ledger.register(kind, id, metadata);
  }

  registerActiveRun(runId, threadId) {
    this.assertOperational();
    if (!runId || !threadId) throw new QASessionError('An active run requires an exact run and thread ID');
    this.activeRuns.set(String(runId), { runId: String(runId), threadId: String(threadId) });
  }

  clearActiveRun(runId) {
    this.assertOperational();
    this.activeRuns.delete(String(runId));
  }

  abort() {
    for (const controller of this.activeControllers) controller.abort();
    this.activeControllers.clear();
  }

  async quarantine() {
    if (this.quarantinePromise) return this.quarantinePromise;
    this.quarantined = true;
    this.abort();
    this.quarantinePromise = (async () => {
      // Capture a token before closing the browser so cleanup can still use
      // the explicitly authenticated API origin. A missing token remains a
      // visible cleanup failure rather than permitting an anonymous mutation.
      const page = this.page;
      const context = this.context;
      const tokenPromise = !this._authToken
        ? this.readBrowserAuthToken(page, context).then((token) => {
          if (token) this._authToken = token;
          return token;
        }).catch(() => null)
        : Promise.resolve(this._authToken);
      const browser = this.browser;
      this.page = null;
      this.context = null;
      this.browser = null;
      await Promise.race([
        tokenPromise,
        new Promise((resolve) => setTimeout(resolve, Math.min(250, this.config.timeoutMs ?? 250))),
      ]);
      for (const [label, resource] of [['browser context', context], ['browser', browser]]) {
        try {
          await resource?.close();
        } catch (error) {
          this.quarantineErrors.push({ kind: label, error: redactText(error?.message ?? error, this.secrets) });
        }
      }
    })();
    return this.quarantinePromise;
  }

  async cleanup() {
    const retained = [];
    const errors = [...this.quarantineErrors];
    const activeRuns = [];
    for (const run of this.activeRuns.values()) {
      try {
        await this.request(`/api/v1/agent/stream/cancel/${encodeURIComponent(run.threadId)}`, {
          target: 'backend',
          method: 'POST',
          json: { expected_run_id: run.runId },
          internal: true,
        });
        const deadline = Date.now() + (this.config.timeoutMs ?? 30_000);
        let terminal = false;
        while (Date.now() < deadline) {
          try {
            const status = await this.request(`/api/v1/agent/jobs/${encodeURIComponent(run.runId)}`, { target: 'backend', internal: true });
            if (['cancelled', 'completed', 'failed'].includes(status.data?.status)) {
              terminal = true;
              break;
            }
          } catch (error) {
            if (error?.status === 404) {
              terminal = true;
              break;
            }
            throw error;
          }
          await new Promise((resolve) => setTimeout(resolve, Math.min(100, Math.max(1, deadline - Date.now()))));
        }
        if (!terminal) throw new QASessionError('Owned agent run did not reach a terminal status during cleanup');
        activeRuns.push({ runId: run.runId, threadId: run.threadId, status: 'terminal' });
      } catch (error) {
        // A terminal race is safe: the exact owned run is already gone or
        // completed. Any other result remains visible as incomplete cleanup.
        if ([404, 409].includes(error?.status)) {
          activeRuns.push({ runId: run.runId, threadId: run.threadId, status: 'terminal-race' });
        } else {
          errors.push({ kind: 'agent-run', id: run.runId, error: redactText(error?.message ?? error, this.secrets) });
          activeRuns.push({ runId: run.runId, threadId: run.threadId, status: 'error' });
        }
      }
    }
    this.activeRuns.clear();
    const resources = this.ledger.list().reverse();
    for (const resource of resources) {
      try {
        await this.ledger.deleteOwned(resource.kind, resource.id, async () => {
          const path = {
            workspace: `/api/v2/workspaces/${encodeURIComponent(resource.id)}`,
            conversation: `/api/v2/conversations/${encodeURIComponent(resource.id)}`,
            thread: `/api/v2/threads/${encodeURIComponent(resource.id)}`,
            document: `/api/v1/documents/${encodeURIComponent(resource.id)}`,
            collection: `/api/v2/collections/${encodeURIComponent(resource.id)}`,
          }[resource.kind];
          if (!path) throw new QASessionError(`No cleanup route for fixture kind ${resource.kind}`);
          await this.request(path, { method: 'DELETE', internal: true });
        });
      } catch (error) {
        retained.push({ kind: resource.kind, id: resource.id });
        errors.push({ kind: resource.kind, id: resource.id, error: redactText(error?.message ?? error, this.secrets) });
      }
    }
    return { status: errors.length ? 'incomplete' : 'complete', retained, errors, activeRuns };
  }

  async close() {
    if (this.closed) return;
    this.closed = true;
    this.abort();
    const errors = [];
    for (const [label, resource] of [['browser context', this.context], ['browser', this.browser]]) {
      try {
        await resource?.close();
      } catch (error) {
        errors.push(`${label}: ${redactText(error?.message ?? error, this.secrets)}`);
      }
    }
    this.context = null;
    this.browser = null;
    this.page = null;
    if (errors.length) throw new QASessionError(`Browser close failed: ${errors.join('; ')}`);
  }
}

export async function createSession(config, dependencies = {}) {
  return new QASession(config, dependencies);
}
