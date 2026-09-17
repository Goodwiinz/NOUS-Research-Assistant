import test from 'node:test';
import assert from 'node:assert/strict';
import { mkdtemp, rm, writeFile } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';

import { isFullIdentity, main, observeSourceIdentity, parseArgs } from '../../../tests/e2e/qa/cli.mjs';
import { runCampaign, exitCodeForReport, checkExpectedBackendIdentity } from '../../../tests/e2e/qa/runner.mjs';
import { renderHtml } from '../../../tests/e2e/qa/report.mjs';
import { assertExactAnswer, assertIdempotentMessage, extractAnswerText } from '../../../tests/e2e/qa/scenarios.mjs';
import {
  FixtureLedger,
  FixtureOwnershipError,
  QASession,
} from '../../../tests/e2e/qa/session.mjs';
import http from 'node:http';

test('rejects a credential-bearing target URL before a campaign can start', () => {
  assert.throws(
    () => parseArgs(['--base-url', 'https://qa-user:qa-password@example.test']),
    /credential|userinfo|URL/i
  );
});

test('CLI diagnostics reject credential flags without echoing their values', async () => {
  const originalError = console.error;
  const diagnostics = [];
  console.error = (...args) => diagnostics.push(args.join(' '));
  try {
    assert.equal(await main(['--password=sentinel-password'], { NOUS_QA_PASSWORD: 'sentinel-password' }), 2);
  } finally {
    console.error = originalError;
  }
  assert.equal(diagnostics.length, 1);
  assert.doesNotMatch(diagnostics[0], /sentinel-password/);
  assert.match(diagnostics[0], /credential-bearing|redacted/i);
});

test('unknown credential-looking flags do not echo their values', async () => {
  const originalError = console.error;
  const diagnostics = [];
  console.error = (...args) => diagnostics.push(args.join(' '));
  try {
    assert.equal(await main(['--fixture-password=sentinel-password'], { NOUS_QA_PASSWORD: 'sentinel-password' }), 2);
  } finally {
    console.error = originalError;
  }
  assert.equal(diagnostics.length, 1);
  assert.doesNotMatch(diagnostics[0], /sentinel-password/);
});

test('reproduction command redacts environment secrets', () => {
  const config = parseArgs(
    ['--output-dir', '/tmp/report-sentinel-password'],
    {
      NOUS_QA_BASE_URL: 'http://127.0.0.1:3000',
      NOUS_QA_EMAIL: 'qa@example.test',
      NOUS_QA_PASSWORD: 'sentinel-password',
    }
  );
  assert.doesNotMatch(config.command, /sentinel-password/);
});

test('HTML report escapes hostile values and has no executable script', () => {
  const html = renderHtml({
    schemaVersion: 1,
    run: { id: 'run-1', target: '<script>alert(1)</script>' },
    cases: [
      {
        id: 'hostile',
        status: 'FAIL',
        reason: '& < > " \'',
        evidence: '<script>window.pwned=1</script>',
      },
    ],
    cleanup: { status: 'complete' },
    summary: { passed: 0, failed: 1, blocked: 0, skipped: 0 },
  });

  assert.match(html, /&lt;script&gt;alert\(1\)&lt;\/script&gt;/);
  assert.match(html, /&lt;script&gt;window\.pwned=1&lt;\/script&gt;/);
  assert.doesNotMatch(html, /<script\b/i);
});

test('sanitizes secrets from an exception before emitting report evidence', async () => {
  const secret = 'sentinel-password-qa';
  const report = await runCampaign(
    {
      suite: 'smoke',
      baseUrl: 'http://127.0.0.1:9',
      apiUrl: 'http://127.0.0.1:9/api/v1',
      timeoutMs: 100,
      maxTurns: 1,
      runId: 'run-secret',
      secrets: [secret],
    },
    {
      registry: [
        {
          id: 'secret-failure',
          title: 'Secret failure',
          suite: 'smoke',
          prerequisites: [],
          run: async () => {
            throw new Error(`backend rejected password=${secret}`);
          },
        },
      ],
      sessionFactory: async () => ({ close: async () => {} }),
    }
  );

  assert.equal(report.cases[0].status, 'FAIL');
  assert.doesNotMatch(JSON.stringify(report), new RegExp(secret));
});

test('missing credentials block authenticated scenarios and cannot pass', async () => {
  const report = await runCampaign(
    {
      suite: 'workflow',
      baseUrl: 'http://127.0.0.1:3000',
      apiUrl: 'http://127.0.0.1:8000/api/v1',
      timeoutMs: 100,
      maxTurns: 1,
      runId: 'run-no-credentials',
    },
    {
      registry: [
        {
          id: 'needs-auth',
          title: 'Needs auth',
          suite: 'workflow',
          prerequisites: ['auth'],
          run: async () => ({ assertion: 'should not run' }),
        },
      ],
      sessionFactory: async () => ({ close: async () => {} }),
    }
  );

  assert.equal(report.cases[0].status, 'BLOCKED');
  assert.notEqual(report.cases[0].status, 'PASS');
  assert.equal(exitCodeForReport(report), 2);
});

test('cleanup refuses an unowned UUID', async () => {
  const ledger = new FixtureLedger('run-owned');
  ledger.register('thread', '11111111-1111-4111-8111-111111111111');

  await assert.rejects(
    () => ledger.deleteOwned('thread', '22222222-2222-4222-8222-222222222222', async () => {}),
    (error) => error instanceof FixtureOwnershipError
  );
});

test('cleanup never mutates fixtures anonymously when no token is available', async () => {
  let requests = 0;
  const server = http.createServer((_request, response) => {
    requests += 1;
    response.writeHead(204);
    response.end();
  });
  await new Promise((resolve) => server.listen(0, '127.0.0.1', resolve));
  try {
    const session = new QASession({
      runId: 'run-anonymous-cleanup',
      baseUrl: 'http://127.0.0.1:3000',
      apiUrl: `http://127.0.0.1:${server.address().port}/api/v1`,
      timeoutMs: 100,
    });
    session.registerFixture('thread', '11111111-1111-4111-8111-111111111111');
    const cleanup = await session.cleanup();
    assert.equal(cleanup.status, 'incomplete');
    assert.equal(requests, 0);
  } finally {
    await new Promise((resolve) => server.close(resolve));
  }
});

test('a hung scenario is bounded by timeout and reported as a failure', async () => {
  const started = Date.now();
  const report = await runCampaign(
    {
      suite: 'smoke',
      baseUrl: 'http://127.0.0.1:3000',
      apiUrl: 'http://127.0.0.1:8000/api/v1',
      timeoutMs: 25,
      maxTurns: 1,
      runId: 'run-timeout',
    },
    {
      registry: [
        {
          id: 'hung',
          title: 'Hung',
          suite: 'smoke',
          prerequisites: [],
          run: async () => new Promise(() => {}),
        },
      ],
      sessionFactory: async () => ({ close: async () => {} }),
    }
  );

  assert.ok(Date.now() - started < 500, 'timeout should not hang the runner');
  assert.equal(report.cases[0].status, 'FAIL');
  assert.match(report.cases[0].reason, /timed out/i);
});

test('scenario timeout awaits bounded session cleanup', async () => {
  let cleanupFinished = false;
  const report = await runCampaign(
    {
      suite: 'smoke',
      baseUrl: 'http://127.0.0.1:3000',
      apiUrl: 'http://127.0.0.1:8000/api/v1',
      timeoutMs: 25,
      maxTurns: 1,
      runId: 'run-timeout-cleanup',
    },
    {
      registry: [{
        id: 'hung-cleanup',
        title: 'Hung cleanup',
        suite: 'smoke',
        prerequisites: [],
        run: async () => new Promise(() => {}),
      }],
      sessionFactory: async () => ({
        abort: async () => {
          await new Promise((resolve) => setTimeout(resolve, 5));
          cleanupFinished = true;
        },
        close: async () => {},
      }),
    }
  );
  assert.equal(report.cases[0].status, 'FAIL');
  assert.equal(cleanupFinished, true);
});

test('scenario timeout quarantines late session actions and halts later cases', async () => {
  let quarantined = false;
  let lateRequestRejected = false;
  let lateFixtureRejected = false;
  let laterCaseRan = false;
  const report = await runCampaign(
    {
      suite: 'smoke',
      baseUrl: 'http://127.0.0.1:3000',
      apiUrl: 'http://127.0.0.1:8000/api/v1',
      timeoutMs: 20,
      maxTurns: 1,
      runId: 'run-timeout-quarantine',
    },
    {
      registry: [
        {
          id: 'late-action',
          title: 'Late action',
          suite: 'smoke',
          prerequisites: [],
          run: async (session) => {
            await new Promise((resolve) => setTimeout(resolve, 50));
            try {
              await session.request('/late-mutation', { method: 'POST' });
            } catch {
              lateRequestRejected = true;
            }
            try {
              session.registerFixture('thread', 'late-fixture');
            } catch {
              lateFixtureRejected = true;
            }
          },
        },
        {
          id: 'must-not-run',
          title: 'Must not run',
          suite: 'smoke',
          prerequisites: [],
          run: async () => {
            laterCaseRan = true;
            return { assertion: 'ran' };
          },
        },
      ],
      sessionFactory: async () => ({
        observations: [],
        quarantine: async () => { quarantined = true; },
        request: async () => {
          if (quarantined) throw new Error('session quarantined');
          return { status: 200, data: {} };
        },
        registerFixture: () => {
          if (quarantined) throw new Error('session quarantined');
        },
        cleanup: async () => ({ status: 'complete', retained: [], errors: [] }),
        close: async () => {},
      }),
    }
  );
  await new Promise((resolve) => setTimeout(resolve, 75));
  assert.equal(report.cases[0].status, 'FAIL');
  assert.match(report.cases[0].reason, /timed out/i);
  assert.equal(quarantined, true);
  assert.equal(lateRequestRejected, true);
  assert.equal(lateFixtureRejected, true);
  assert.equal(laterCaseRan, false);
  assert.equal(report.cases[1].status, 'BLOCKED');
});

test('session close errors remain visible in the cleanup report', async () => {
  const report = await runCampaign(
    {
      suite: 'smoke',
      baseUrl: 'http://127.0.0.1:3000',
      apiUrl: 'http://127.0.0.1:8000/api/v1',
      timeoutMs: 100,
      maxTurns: 1,
      runId: 'run-close-error',
    },
    {
      registry: [{
        id: 'close-error',
        title: 'Close error',
        suite: 'smoke',
        prerequisites: [],
        run: async () => ({ assertion: 'case ran' }),
      }],
      sessionFactory: async () => ({
        observations: [],
        cleanup: async () => ({ status: 'complete', retained: [], errors: [] }),
        close: async () => { throw new Error('browser close sentinel'); },
      }),
    }
  );
  assert.equal(report.cleanup.status, 'incomplete');
  assert.match(report.cleanup.errors[0].message, /browser close sentinel/i);
  assert.equal(report.summary.incomplete, true);
});

test('empty scenario selection is incomplete and exits with code 2', async () => {
  const report = await runCampaign(
    {
      suite: 'smoke',
      selectedIds: ['does-not-exist'],
      baseUrl: 'http://127.0.0.1:3000',
      apiUrl: 'http://127.0.0.1:8000/api/v1',
      timeoutMs: 100,
      maxTurns: 1,
      runId: 'run-empty',
    },
    { registry: [], sessionFactory: async () => ({ close: async () => {} }) }
  );

  assert.equal(report.cases.length, 0);
  assert.equal(exitCodeForReport(report), 2);
  assert.match(report.summary.reason, /no scenarios/i);
});

test('deployment evidence is validated and target-bound', async () => {
  const dir = await mkdtemp(join(tmpdir(), 'nous-qa-test-'));
  const path = join(dir, 'evidence.json');
  await writeFile(
    path,
    JSON.stringify({
      targetUrl: 'https://qa.example.test',
      backendSha: 'f'.repeat(40),
      frontendSha: 'e'.repeat(40),
      observedAt: new Date().toISOString(),
      provenance: 'local test fixture',
    })
  );
  try {
    const config = parseArgs([
      '--base-url',
      'https://qa.example.test',
      '--expected-backend-sha',
      'f'.repeat(40),
      '--deployment-evidence',
      path,
    ]);
    assert.equal(config.deploymentEvidence.backendSha, 'f'.repeat(40));
    assert.equal(config.deploymentEvidence.provenance, 'local test fixture');
  } finally {
    await rm(dir, { recursive: true, force: true });
  }
});

test('backend deployment evidence may bind to the explicitly configured API origin', async () => {
  const dir = await mkdtemp(join(tmpdir(), 'nous-qa-api-evidence-'));
  const path = join(dir, 'evidence.json');
  await writeFile(path, JSON.stringify({
    targetUrl: 'https://api.qa.example.test',
    backendSha: 'a'.repeat(40),
    observedAt: new Date().toISOString(),
    provenance: 'operator fixture',
  }));
  try {
    const config = parseArgs([
      '--base-url', 'https://qa.example.test',
      '--api-url', 'https://api.qa.example.test/api/v1',
      '--expected-backend-sha', 'a'.repeat(40),
      '--deployment-evidence', path,
    ]);
    assert.equal(config.deploymentEvidence.targetUrl, 'https://api.qa.example.test');
  } finally {
    await rm(dir, { recursive: true, force: true });
  }
});

test('deployment evidence cannot bind to a different frontend origin', async () => {
  const dir = await mkdtemp(join(tmpdir(), 'nous-qa-frontend-evidence-'));
  const path = join(dir, 'evidence.json');
  await writeFile(path, JSON.stringify({
    targetUrl: 'https://frontend.qa.example.test',
    backendSha: 'a'.repeat(40),
    observedAt: new Date().toISOString(),
    provenance: 'operator fixture',
  }));
  try {
    assert.throws(() => parseArgs([
      '--base-url', 'https://frontend.qa.example.test',
      '--api-url', 'https://backend.qa.example.test/api/v1',
      '--deployment-evidence', path,
    ]), /backend API target/i);
  } finally {
    await rm(dir, { recursive: true, force: true });
  }
});

test('deployment identities require a complete SHA or digest', () => {
  assert.equal(isFullIdentity('a'.repeat(40)), true);
  assert.equal(isFullIdentity(`sha256:${'b'.repeat(64)}`), true);
  assert.equal(isFullIdentity('version-dev'), false);
  assert.equal(isFullIdentity('abcdef'), false);
});

test('does not enter credentials after a login navigation leaves the trusted origin', async () => {
  let filled = false;
  const page = {
    url: () => 'https://evil.example.test/login',
    goto: async () => ({ status: () => 200 }),
    locator: () => ({ fill: async () => { filled = true; } }),
  };
  const session = new QASession({
    runId: 'run-origin',
    baseUrl: 'https://qa.example.test',
    apiUrl: 'https://api.example.test/api/v1',
    timeoutMs: 100,
    credentials: { email: 'qa@example.test', password: 'sentinel' },
    secrets: ['sentinel'],
  });
  session.page = page;
  await assert.rejects(() => session.login(), /target origin/i);
  assert.equal(filled, false);
});

test('login waits for an asynchronous protected-route transition', async () => {
  let location = 'https://qa.example.test/login';
  const page = {
    url: () => location,
    goto: async () => ({ status: () => 200 }),
    waitForURL: async (predicate) => {
      if (!predicate(new URL(location))) throw new Error('still on login');
    },
    waitForLoadState: async () => {},
    locator: (selector) => {
      if (selector === 'form') return { getByRole: () => ({ click: async () => { location = 'https://qa.example.test/dashboard'; } }) };
      return { fill: async () => {} };
    },
  };
  const session = new QASession({
    runId: 'run-login-wait',
    baseUrl: 'https://qa.example.test',
    apiUrl: 'https://api.example.test/api/v1',
    timeoutMs: 100,
    credentials: { email: 'qa@example.test', password: 'sentinel' },
    secrets: ['sentinel'],
  });
  session.page = page;
  await session.login();
  assert.equal(new URL(page.url()).pathname, '/dashboard');
});

test('invalid credentials never count as authenticated when login stays on /login', async () => {
  const page = {
    url: () => 'https://qa.example.test/login',
    goto: async () => ({ status: () => 200 }),
    waitForURL: async () => { throw new Error('still on login'); },
    waitForLoadState: async () => {},
    locator: (selector) => selector === 'form'
      ? { getByRole: () => ({ click: async () => {} }) }
      : { fill: async () => {} },
  };
  const session = new QASession({
    runId: 'run-login-invalid',
    baseUrl: 'https://qa.example.test',
    apiUrl: 'https://api.example.test/api/v1',
    timeoutMs: 100,
    credentials: { email: 'qa@example.test', password: 'sentinel' },
    secrets: ['sentinel'],
  });
  session.page = page;
  await assert.rejects(() => session.login(), /protected route/i);
});

test('storage state is rejected when protected navigation redirects to /login', async () => {
  const page = {
    url: () => 'https://qa.example.test/login',
    goto: async () => ({ status: () => 302 }),
  };
  const session = new QASession({
    runId: 'run-storage-login',
    baseUrl: 'https://qa.example.test',
    apiUrl: 'https://api.example.test/api/v1',
    timeoutMs: 100,
    storageState: '/tmp/local-storage-state.json',
  });
  session.page = page;
  await assert.rejects(() => session.login(), /protected route/i);
});

test('browser auth decodes ordered Supabase SSR cookie chunks only on the trusted frontend domain', async () => {
  const token = 'synthetic-cookie-access-token-0123456789';
  const encoded = `base64-${Buffer.from(JSON.stringify({ access_token: token })).toString('base64url')}`;
  const chunkSize = 11;
  const chunks = Array.from({ length: Math.ceil(encoded.length / chunkSize) }, (_, index) => encoded.slice(index * chunkSize, (index + 1) * chunkSize));
  const config = {
    runId: 'run-cookie-auth',
    baseUrl: 'https://qa.example.test',
    apiUrl: 'https://api.example.test/api/v1',
    timeoutMs: 100,
  };
  const session = new QASession(config);
  session.page = { evaluate: async () => [] };
  session.context = {
    cookies: async (url) => {
      assert.equal(url, config.baseUrl);
      return [
        ...chunks.map((value, index) => ({
          name: `sb-project-auth-token.${index}`,
          value: encodeURIComponent(value),
          domain: index % 2 ? '.qa.example.test' : 'qa.example.test',
        })).reverse(),
        { name: 'sb-project-auth-token.0', value: chunks[0], domain: '.api.example.test' },
      ];
    },
  };
  const actual = await session.browserAuthToken();
  assert.equal(actual, token);
  assert.equal(session._authToken, token);
  assert.doesNotMatch(JSON.stringify(session.observations), /synthetic-cookie-access-token/);
});

test('manual API redirects do not forward bearer credentials cross-origin', async () => {
  let leakedAuthorization = null;
  const destination = http.createServer((request, response) => {
    leakedAuthorization = request.headers.authorization ?? null;
    response.writeHead(200, { 'content-type': 'application/json' });
    response.end('{}');
  });
  const redirector = http.createServer((_request, response) => {
    response.writeHead(302, { location: `http://127.0.0.1:${destination.address().port}/secret` });
    response.end();
  });
  await new Promise((resolve) => destination.listen(0, '127.0.0.1', resolve));
  await new Promise((resolve) => redirector.listen(0, '127.0.0.1', resolve));
  try {
    const session = new QASession({
      runId: 'run-redirect',
      baseUrl: 'http://127.0.0.1:3000',
      apiUrl: `http://127.0.0.1:${redirector.address().port}/api/v1`,
      timeoutMs: 500,
      authToken: 'sentinel-token',
      secrets: ['sentinel-token'],
    });
    await assert.rejects(() => session.request('/redirect', { target: 'backend' }), /redirected|failed/i);
    assert.equal(leakedAuthorization, null);
  } finally {
    await new Promise((resolve) => redirector.close(resolve));
    await new Promise((resolve) => destination.close(resolve));
  }
});

test('response body reads remain bounded after headers arrive', async () => {
  const server = http.createServer((_request, response) => {
    response.writeHead(200, { 'content-type': 'application/json' });
    response.write('{"partial":');
  });
  await new Promise((resolve) => server.listen(0, '127.0.0.1', resolve));
  try {
    const session = new QASession({
      runId: 'run-body-timeout',
      baseUrl: 'http://127.0.0.1:3000',
      apiUrl: `http://127.0.0.1:${server.address().port}/api/v1`,
      timeoutMs: 25,
    });
    await assert.rejects(() => session.request('/never-finishes', { target: 'backend' }), /timed out|aborted/i);
  } finally {
    await new Promise((resolve) => server.close(resolve));
  }
});

test('response byte cap rejects a finite oversized body', async () => {
  const server = http.createServer((_request, response) => {
    response.writeHead(200, { 'content-type': 'text/plain' });
    response.end('0123456789abcdef');
  });
  await new Promise((resolve) => server.listen(0, '127.0.0.1', resolve));
  try {
    const session = new QASession({
      runId: 'run-body-cap',
      baseUrl: 'http://127.0.0.1:3000',
      apiUrl: `http://127.0.0.1:${server.address().port}/api/v1`,
      timeoutMs: 500,
    });
    await assert.rejects(() => session.request('/oversized', { target: 'backend', maxResponseBytes: 8 }), /bounded limit/i);
  } finally {
    await new Promise((resolve) => server.close(resolve));
  }
});

test('non-success response bodies stay out of session errors', async () => {
  const server = http.createServer((_request, response) => {
    response.writeHead(500, { 'content-type': 'application/json' });
    response.end(JSON.stringify({ detail: 'unrelated-account-body' }));
  });
  await new Promise((resolve) => server.listen(0, '127.0.0.1', resolve));
  try {
    const session = new QASession({
      runId: 'run-error-body',
      baseUrl: 'http://127.0.0.1:3000',
      apiUrl: `http://127.0.0.1:${server.address().port}/api/v1`,
      timeoutMs: 500,
    });
    await assert.rejects(
      () => session.request('/private-error', { target: 'backend' }),
      (error) => error.status === 500 && !error.message.includes('unrelated-account-body')
    );
  } finally {
    await new Promise((resolve) => server.close(resolve));
  }
});

test('report retains local source commit and dirty state when supplied', () => {
  const config = parseArgs([], {
    NOUS_QA_BASE_URL: 'http://127.0.0.1:3000',
    NOUS_QA_SOURCE_SHA: 'f'.repeat(40),
    NOUS_QA_SOURCE_DIRTY: 'dirty',
  });
  assert.deepEqual(config.sourceIdentity, {
    sha: 'f'.repeat(40),
    dirty: 'dirty',
    provenance: 'NOUS_QA_SOURCE_SHA override; NOUS_QA_SOURCE_DIRTY override',
  });
});

test('source identity falls back to bounded git HEAD and worktree observation', () => {
  const identity = observeSourceIdentity({});
  assert.match(identity.sha, /^[0-9a-f]{40}$/i);
  assert.ok(['clean', 'dirty', 'unknown'].includes(identity.dirty));
  assert.match(identity.provenance, /git HEAD/);
  assert.match(identity.provenance, /git worktree status/);
});

test('an assertion failure keeps exit code 1 even when another case is blocked', () => {
  assert.equal(
    exitCodeForReport({ summary: { failed: 1, blocked: 1, incomplete: true, selected: 2 } }),
    1
  );
});

test('expected backend identity blocks write cases before fixture mutation', async () => {
  let factoryCalls = 0;
  const report = await runCampaign(
    {
      suite: 'workflow',
      baseUrl: 'http://127.0.0.1:3000',
      apiUrl: 'http://127.0.0.1:8000/api/v1',
      expectedBackendSha: 'b'.repeat(40),
      allowWrites: true,
      credentials: { email: 'qa@example.test', password: 'sentinel' },
      secrets: ['sentinel'],
      timeoutMs: 100,
      maxTurns: 1,
      runId: 'run-identity-gate',
    },
    {
      registry: [{
        id: 'write-case',
        title: 'Write case',
        suite: 'workflow',
        prerequisites: ['auth', 'writes'],
        run: async () => { throw new Error('must not run'); },
      }],
      sessionFactory: async () => {
        factoryCalls += 1;
        return { close: async () => {} };
      },
    }
  );
  assert.equal(factoryCalls, 1);
  assert.equal(report.cases[0].status, 'BLOCKED');
  assert.match(report.cases[0].reason, /identity/i);
});

test('actual full health identity can satisfy the central write gate', async () => {
  let ran = false;
  const expected = 'c'.repeat(40);
  const report = await runCampaign(
    {
      suite: 'workflow',
      baseUrl: 'http://127.0.0.1:3000',
      apiUrl: 'http://127.0.0.1:8000/api/v1',
      expectedBackendSha: expected,
      allowWrites: true,
      credentials: { email: 'qa@example.test', password: 'sentinel' },
      secrets: ['sentinel'],
      timeoutMs: 100,
      maxTurns: 1,
      runId: 'run-health-identity-gate',
    },
    {
      registry: [{
        id: 'write-case-health-identity',
        title: 'Write case health identity',
        suite: 'workflow',
        prerequisites: ['auth', 'writes'],
        run: async () => { ran = true; return { assertion: 'ran after health identity' }; },
      }],
      sessionFactory: async () => ({
        observations: [],
        request: async (path) => {
          assert.equal(path, '/health');
          return { data: { status: 'healthy', git_sha: expected } };
        },
        cleanup: async () => ({ status: 'complete', retained: [], errors: [] }),
        close: async () => {},
      }),
    }
  );
  assert.equal(ran, true);
  assert.equal(report.cases[0].status, 'PASS');
  assert.equal(report.run.observedIdentity.backendSha, expected);
});

test('a mismatching live identity blocks even matching external evidence', () => {
  const expected = 'c'.repeat(40);
  const result = checkExpectedBackendIdentity({
    expectedBackendSha: expected,
    deploymentEvidence: { backendSha: expected, provenance: 'operator fixture' },
  }, { git_sha: 'd'.repeat(40) });
  assert.equal(result.status, 'blocked');
  assert.match(result.reason, /does not match/i);
});

test('exact answer oracle rejects empty, partial, and substring matches', () => {
  const exact = [
    { event: 'token', data: { content: 'NOUS_' } },
    { event: 'token', data: { content: 'QA_ACK' } },
    { event: 'done', data: { status: 'complete' } },
  ];
  assert.equal(extractAnswerText(exact), 'NOUS_QA_ACK');
  assert.equal(assertExactAnswer(exact, 'NOUS_QA_ACK'), 'NOUS_QA_ACK');
  assert.throws(() => assertExactAnswer([{ event: 'done', data: {} }], 'NOUS_QA_ACK'), /empty/i);
  assert.throws(() => assertExactAnswer([{ event: 'token', data: { content: 'NOUS_QA_ACK_EXTRA' } }], 'NOUS_QA_ACK'), /exactly/i);
  assert.throws(() => assertExactAnswer([{ event: 'token', data: { content: 'answer includes NOUS_QA_ACK' } }], 'NOUS_QA_ACK'), /exactly/i);
});

test('idempotency oracle rejects a duplicate persisted row even when IDs are echoed', () => {
  const row = { client_message_id: 'cmid-1', content: 'exact content' };
  assert.throws(() => assertIdempotentMessage({
    firstId: 'message-1',
    secondId: 'message-1',
    messages: [row, { ...row, content: 'duplicate content' }],
    clientMessageId: 'cmid-1',
    content: 'exact content',
  }), /exactly one/i);
  assert.throws(() => assertIdempotentMessage({
    firstId: 'message-1',
    secondId: 'message-2',
    messages: [row],
    clientMessageId: 'cmid-1',
    content: 'exact content',
  }), /different message ID/i);
});
