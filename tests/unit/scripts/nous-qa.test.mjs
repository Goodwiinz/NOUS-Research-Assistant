import test from 'node:test';
import assert from 'node:assert/strict';
import { mkdtemp, rm, writeFile } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';

import { isFullIdentity, main, observeSourceIdentity, parseArgs } from '../../../tests/e2e/qa/cli.mjs';
import { runCampaign, exitCodeForReport, checkExpectedBackendIdentity, sanitizeAssertionEvidence } from '../../../tests/e2e/qa/runner.mjs';
import { renderHtml } from '../../../tests/e2e/qa/report.mjs';
import { assertExactAnswer, assertIdempotentMessage, extractAnswerText, isCanonicalEmptyThreadMessageList, registry, smokeLogin } from '../../../tests/e2e/qa/scenarios.mjs';
import {
  FixtureLedger,
  FixtureOwnershipError,
  QASession,
} from '../../../tests/e2e/qa/session.mjs';
import http from 'node:http';

function delayedVisibleLocator({ appearsAfterMs = 0, count = 1, visible = true } = {}) {
  const startedAt = Date.now();
  const handle = {
    async waitFor({ timeout }) {
      const deadline = Date.now() + timeout;
      while (Date.now() < deadline && Date.now() - startedAt < appearsAfterMs) {
        await new Promise((resolve) => setTimeout(resolve, Math.min(5, Math.max(1, deadline - Date.now()))));
      }
      if (Date.now() - startedAt < appearsAfterMs || !visible) throw new Error('synthetic locator timeout');
    },
  };
  return {
    first: () => handle,
    count: async () => (Date.now() - startedAt >= appearsAfterMs ? count : 0),
  };
}

function fakeLoginSession(locators, timeoutMs = 100) {
  return {
    config: { timeoutMs },
    goto: async () => {},
    page: { locator: (selector) => locators[selector] },
  };
}

function fakeModelSession(streams) {
  const fixtureIds = [
    '81818181-8181-4181-8181-818181818181',
    '92929292-9292-4292-8292-929292929292',
    'a3a3a3a3-a3a3-43a3-83a3-a3a3a3a3a3a3',
    'b4b4b4b4-b4b4-44b4-84b4-b4b4b4b4b4b4',
    'c5c5c5c5-c5c5-45c5-85c5-c5c5c5c5c5c5',
  ];
  let fixtureIndex = 0;
  return {
    observations: [],
    login: async () => {},
    request: async () => ({ status: 201, data: { id: fixtureIds[fixtureIndex++ % fixtureIds.length] } }),
    registerFixture: () => {},
    streamAgent: async () => streams.shift() ?? { status: 200, events: [], terminal: true, acceptedRunIds: [] },
    cleanup: async () => ({ status: 'complete', retained: [], errors: [] }),
    close: async () => {},
  };
}

async function runLocalModelScenario(id, streams, runId, secrets = []) {
  const scenario = registry.find((item) => item.id === id);
  assert.ok(scenario, `${id} must remain registered`);
  return runCampaign(
    {
      suite: scenario.suite,
      selectedIds: [id],
      baseUrl: 'http://127.0.0.1:3000',
      apiUrl: 'http://127.0.0.1:8000/api/v1',
      timeoutMs: 100,
      maxTurns: 12,
      allowWrites: true,
      credentials: { email: 'qa@example.test', password: 'synthetic-password' },
      secrets,
      runId,
    },
    {
      registry: [scenario],
      sessionFactory: async () => fakeModelSession(streams),
    }
  );
}

function loginLocators(overrides = {}) {
  return {
    '#email': delayedVisibleLocator(),
    '#password': delayedVisibleLocator(),
    'form button[type="submit"]': delayedVisibleLocator(),
    ...overrides,
  };
}

test('login smoke waits for delayed visible controls before checking uniqueness', async () => {
  const startedAt = Date.now();
  const result = await smokeLogin(fakeLoginSession(loginLocators({ '#email': delayedVisibleLocator({ appearsAfterMs: 20 }) })));
  assert.equal(result.assertion, 'Accessible login fields are rendered');
  assert.ok(Date.now() - startedAt >= 15, 'the smoke check should wait for delayed React controls');
});

test('login smoke fails within its bound when a required control is absent', async () => {
  const session = fakeLoginSession(loginLocators({ '#password': delayedVisibleLocator({ appearsAfterMs: 10_000 }) }), 25);
  await assert.rejects(() => smokeLogin(session), /Login password field is missing or not visible/);
});

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

test('CLI report paths are redacted when the configured output directory contains a secret', async () => {
  const dir = await mkdtemp(join(tmpdir(), 'nous-qa-output-'));
  const outputDir = join(dir, 'sentinel-password-reports');
  const originalLog = console.log;
  const lines = [];
  console.log = (...args) => lines.push(args.join(' '));
  try {
    const exitCode = await main([
      '--suite', 'smoke',
      '--scenario', 'stdout-path-redaction',
      '--output-dir', outputDir,
    ], {
      NOUS_QA_PASSWORD: 'sentinel-password',
    }, {
      registry: [{
        id: 'stdout-path-redaction',
        title: 'stdout path redaction',
        suite: 'smoke',
        prerequisites: [],
        run: async () => ({ assertion: 'pass' }),
      }],
      sessionFactory: async () => ({
        observations: [],
        cleanup: async () => ({ status: 'complete', retained: [], errors: [] }),
        close: async () => {},
      }),
    });
    assert.equal(exitCode, 0);
  } finally {
    console.log = originalLog;
    await rm(dir, { recursive: true, force: true });
  }
  assert.doesNotMatch(lines.join('\n'), /sentinel-password/);
  assert.match(lines.join('\n'), /JSON: .*\[REDACTED\]/);
  assert.match(lines.join('\n'), /HTML: .*\[REDACTED\]/);
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

test('authenticated validation stops after an accepted invalid response within maxTurns=1', async () => {
  const validation = registry.find((scenario) => scenario.id === 'adversarial.authenticated-bounded-validation');
  assert.ok(validation, 'validation scenario must remain registered');
  let streamCalls = 0;
  const ids = [
    '45454545-4545-4454-8454-454545454545',
    '56565656-5656-4565-8565-565656565656',
    '67676767-6767-4676-8676-676767676767',
  ];
  let nextId = 0;
  const report = await runCampaign(
    {
      suite: 'adversarial',
      selectedIds: [validation.id],
      baseUrl: 'http://127.0.0.1:3000',
      apiUrl: 'http://127.0.0.1:8000/api/v1',
      timeoutMs: 100,
      maxTurns: 1,
      allowWrites: true,
      credentials: { email: 'qa@example.test', password: 'synthetic-password' },
      runId: 'run-validation-max-one',
    },
    {
      registry: [validation],
      sessionFactory: async () => ({
        observations: [],
        login: async () => {},
        request: async () => ({ status: 201, data: { id: ids[nextId++ % ids.length] } }),
        registerFixture: () => {},
        streamAgent: async () => {
          streamCalls += 1;
          return { status: 200, acceptedRunIds: ['78787878-7878-4787-8787-787878787878'], events: [] };
        },
        cleanup: async () => ({ status: 'complete', retained: [], errors: [] }),
        close: async () => {},
      }),
    }
  );

  assert.equal(report.cases[0].status, 'FAIL');
  assert.match(report.cases[0].reason, /unexpectedly accepted/i);
  assert.equal(report.summary.modelTurns, 1, 'the accepted attempt must consume the reserved maxTurns=1 budget');
  assert.equal(streamCalls, 1, 'an accepted invalid response must halt before a second submission');
});

test('stale-thread access accepts only the canonical empty message-list response', async () => {
  const stale = registry.find((scenario) => scenario.id === 'adversarial.stale-thread-access');
  assert.ok(stale, 'stale-thread scenario must remain registered');
  const session = {
    login: async () => {},
    request: async (path) => {
      if (path.endsWith('/messages') && path.startsWith('/api/v2/')) {
        return { status: 200, data: { messages: [], total: 0, page: 1, limit: 100, has_more: false } };
      }
      return { status: 404, data: { detail: 'not found' } };
    },
  };
  await stale.run(session);
  assert.equal(isCanonicalEmptyThreadMessageList({ messages: [], total: 0, page: 1, limit: 100, has_more: false }), true);
  assert.equal(isCanonicalEmptyThreadMessageList({ messages: [], total: 0, page: 1, limit: 100, has_more: false, data: 'unexpected' }), false);

  await assert.rejects(
    () => stale.run({
      ...session,
      request: async (path) => {
        if (path.endsWith('/messages') && path.startsWith('/api/v2/')) {
          return { status: 200, data: { data: { messages: [] }, total: 0, page: 1, limit: 100, has_more: false } };
        }
        return { status: 404, data: null };
      },
    }),
    /unexpected successful response shape/
  );
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

test('unknown scenario selection is invalid and exits with code 2', async () => {
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
  assert.match(report.summary.reason, /unknown|out-of-suite/i);
});

test('invalid selected IDs are redacted on the early report return path', async () => {
  const secret = 'sentinel-invalid-selection-password';
  const report = await runCampaign(
    {
      suite: 'smoke',
      selectedIds: [`missing-${secret}`],
      baseUrl: 'http://127.0.0.1:3000',
      apiUrl: 'http://127.0.0.1:8000/api/v1',
      timeoutMs: 100,
      maxTurns: 1,
      runId: 'run-invalid-selection-redaction',
      secrets: [secret],
    },
    { registry: [], sessionFactory: async () => ({ close: async () => {} }) }
  );

  assert.equal(report.summary.invalid, true);
  assert.doesNotMatch(JSON.stringify(report), new RegExp(secret));
  assert.match(JSON.stringify(report), /\[REDACTED\]/);
});

test('mixed valid and unknown scenario selections are rejected before any selected case runs', async () => {
  let ran = false;
  const report = await runCampaign(
    {
      suite: 'smoke',
      selectedIds: ['known', 'workflow-only'],
      baseUrl: 'http://127.0.0.1:3000',
      apiUrl: 'http://127.0.0.1:8000/api/v1',
      timeoutMs: 100,
      maxTurns: 1,
      runId: 'run-invalid-selection',
    },
    {
      registry: [
        { id: 'known', title: 'known', suite: 'smoke', prerequisites: [], run: async () => { ran = true; return { assertion: 'ran' }; } },
        { id: 'workflow-only', title: 'workflow-only', suite: 'workflow', prerequisites: [], run: async () => ({ assertion: 'wrong suite' }) },
      ],
      sessionFactory: async () => ({ close: async () => {} }),
    }
  );
  assert.equal(ran, false);
  assert.equal(report.cases.length, 0);
  assert.equal(report.summary.invalid, true);
  assert.match(report.summary.reason, /unknown|out-of-suite/i);
  assert.equal(exitCodeForReport(report), 2);
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

test('deferred browser launch is quarantined and closes late resources', async () => {
  let releaseLaunch;
  let browserClosed = 0;
  const launch = new Promise((resolve) => { releaseLaunch = resolve; });
  const session = new QASession({
    runId: 'run-deferred-browser',
    baseUrl: 'http://127.0.0.1:3000',
    apiUrl: 'http://127.0.0.1:8000/api/v1',
    timeoutMs: 100,
  }, {
    playwright: {
      chromium: {
        launch: async () => launch,
      },
    },
  });
  const opening = session.openBrowser();
  await new Promise((resolve) => setTimeout(resolve, 5));
  const quarantining = session.quarantine();
  releaseLaunch({ close: async () => { browserClosed += 1; } });
  await assert.rejects(opening, /quarantined/i);
  await quarantining;
  assert.equal(browserClosed, 1);
});

test('agent streams centrally register accepted runs only for owned threads and cleanup cancels before deletion', async () => {
  const order = [];
  const runId = '11111111-1111-4111-8111-111111111111';
  const workspaceId = '22222222-2222-4222-8222-222222222222';
  const conversationId = '33333333-3333-4333-8333-333333333333';
  const threadId = '44444444-4444-4444-8444-444444444444';
  const server = http.createServer((request, response) => {
    const path = new URL(request.url, 'http://localhost').pathname;
    if (path === '/api/v1/agent/stream') {
      response.writeHead(200, { 'content-type': 'text/event-stream' });
      response.end([
        `event: status\ndata: ${JSON.stringify({ phase: 'accepted', run_id: runId })}\n`,
        'event: token\ndata: {"content":"NOUS_STREAM_ACK"}\n',
        'event: done\ndata: {"status":"complete"}\n',
      ].join('\n'));
      return;
    }
    if (path.startsWith('/api/v1/agent/stream/cancel/')) {
      order.push('cancel');
      response.writeHead(204);
      response.end();
      return;
    }
    if (path.startsWith('/api/v1/agent/jobs/')) {
      order.push('job');
      response.writeHead(200, { 'content-type': 'application/json' });
      response.end(JSON.stringify({ status: 'cancelled' }));
      return;
    }
    if (request.method === 'DELETE') {
      order.push(`delete:${path}`);
      response.writeHead(204);
      response.end();
      return;
    }
    response.writeHead(404);
    response.end();
  });
  await new Promise((resolve) => server.listen(0, '127.0.0.1', resolve));
  try {
    const session = new QASession({
      runId: 'run-stream-owned',
      baseUrl: 'http://127.0.0.1:3000',
      apiUrl: `http://127.0.0.1:${server.address().port}/api/v1`,
      timeoutMs: 500,
      authToken: 'synthetic-token',
      secrets: ['synthetic-token'],
    });
    session.registerFixture('workspace', workspaceId);
    session.registerFixture('conversation', conversationId, { workspaceId });
    session.registerFixture('thread', threadId, { conversationId });
    let seenDuringCallback = false;
    const result = await session.streamAgent({
      thread_id: threadId,
      messages: [{ role: 'user', content: 'ack' }],
      use_rag: false,
    }, {
      onEvent: (event) => {
        if (event.event === 'status') seenDuringCallback = session.activeRuns.has(runId);
      },
    });
    assert.equal(seenDuringCallback, true);
    assert.deepEqual(result.acceptedRunIds, [runId]);
    assert.equal(session.activeRuns.has(runId), false, 'terminal done stream should clear its proven run');
    await assert.rejects(
      () => session.streamAgent({ thread_id: '55555555-5555-4555-8555-555555555555', messages: [], use_rag: false }),
      /owned thread/i
    );
    session.registerActiveRun(runId, threadId);
    const cleanup = await session.cleanup();
    assert.equal(cleanup.status, 'complete');
    assert.deepEqual(order.slice(0, 2), ['cancel', 'job']);
    assert.deepEqual(order.slice(2), [
      `delete:/api/v2/threads/${threadId}`,
      `delete:/api/v2/conversations/${conversationId}`,
      `delete:/api/v2/workspaces/${workspaceId}`,
    ]);
  } finally {
    await new Promise((resolve) => server.close(resolve));
  }
});

test('a stream that times out before acceptance retains its owned thread for uncertain-run recovery', async () => {
  const workspaceId = 'cccccccc-cccc-4ccc-8ccc-cccccccccccc';
  const conversationId = 'dddddddd-dddd-4ddd-8ddd-dddddddddddd';
  const threadId = 'eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee';
  let acceptedSent = false;
  let deleteRequests = 0;
  const server = http.createServer((request, response) => {
    const path = new URL(request.url, 'http://localhost').pathname;
    if (request.method === 'POST' && path === '/api/v1/agent/stream') {
      response.writeHead(200, { 'content-type': 'text/event-stream' });
      setTimeout(() => {
        acceptedSent = true;
        response.write(`event: status\ndata: ${JSON.stringify({ phase: 'accepted', run_id: 'ffffffff-ffff-4fff-8fff-ffffffffffff' })}\n\n`);
      }, 100);
      return;
    }
    if (request.method === 'DELETE') {
      deleteRequests += 1;
      response.writeHead(204);
      response.end();
      return;
    }
    response.writeHead(404);
    response.end();
  });
  await new Promise((resolve) => server.listen(0, '127.0.0.1', resolve));
  try {
    const session = new QASession({
      runId: 'run-stream-timeout-before-accepted',
      baseUrl: 'http://127.0.0.1:3000',
      apiUrl: `http://127.0.0.1:${server.address().port}/api/v1`,
      timeoutMs: 25,
      authToken: 'synthetic-token',
      secrets: ['synthetic-token'],
    });
    session.registerFixture('workspace', workspaceId);
    session.registerFixture('conversation', conversationId, { workspaceId });
    session.registerFixture('thread', threadId, { conversationId });
    await assert.rejects(
      () => session.streamAgent({ thread_id: threadId, messages: [{ role: 'user', content: 'slow' }], use_rag: false }),
      /timed out|aborted/i
    );
    const cleanup = await session.cleanup();
    assert.equal(cleanup.status, 'incomplete');
    assert.equal(cleanup.uncertainStreams.length, 1);
    assert.equal(cleanup.uncertainStreams[0].threadId, threadId);
    assert.ok(cleanup.retained.some((item) => item.kind === 'thread' && item.id === threadId));
    assert.equal(deleteRequests, 0);
    await new Promise((resolve) => setTimeout(resolve, 120));
    assert.equal(acceptedSent, true, 'local transport should model a late accepted run after the client timeout');
  } finally {
    server.closeAllConnections?.();
    await new Promise((resolve) => server.close(resolve));
  }
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

test('timed-out mutating requests remain uncertain and prevent cleanup from claiming completion', async () => {
  let created = false;
  let deleteRequests = 0;
  let delayedResponse;
  const server = http.createServer((request, response) => {
    const path = new URL(request.url, 'http://localhost').pathname;
    if (request.method === 'POST' && path === '/api/v2/workspaces') {
      created = true;
      delayedResponse = setTimeout(() => {
        response.writeHead(201, { 'content-type': 'application/json' });
        response.end(JSON.stringify({ id: '66666666-6666-4666-8666-666666666666' }));
      }, 2_000);
      request.on('close', () => clearTimeout(delayedResponse));
      return;
    }
    if (request.method === 'DELETE') {
      deleteRequests += 1;
      response.writeHead(204);
      response.end();
      return;
    }
    response.writeHead(404);
    response.end();
  });
  await new Promise((resolve) => server.listen(0, '127.0.0.1', resolve));
  try {
    const session = new QASession({
      runId: 'run-uncertain-mutation',
      baseUrl: 'http://127.0.0.1:3000',
      apiUrl: `http://127.0.0.1:${server.address().port}/api/v1`,
      timeoutMs: 500,
      authToken: 'synthetic-token',
      secrets: ['synthetic-token'],
    });
    session.registerFixture('thread', '77777777-7777-4777-8777-777777777777');
    await assert.rejects(
      () => session.request('/api/v2/workspaces', { target: 'backend', method: 'POST', json: { name: 'delayed' }, timeoutMs: 500 }),
      /timed out|aborted/i
    );
    await new Promise((resolve) => setTimeout(resolve, 20));
    assert.equal(created, true);
    const cleanup = await session.cleanup();
    assert.equal(cleanup.status, 'incomplete');
    assert.equal(cleanup.uncertainMutations.length, 1);
    assert.match(cleanup.errors[0].reason, /outcome was not observed|pending/i);
    assert.equal(deleteRequests, 0, 'uncertain mutation must not trigger guessed cleanup deletes');
  } finally {
    clearTimeout(delayedResponse);
    server.closeAllConnections?.();
    await new Promise((resolve) => server.close(resolve));
  }
});

test('failed active-run cancellation retains the owned fixture tree and does not treat 409 as terminal', async () => {
  const runId = '88888888-8888-4888-8888-888888888888';
  const workspaceId = '99999999-9999-4999-8999-999999999999';
  const conversationId = 'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa';
  const threadId = 'bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb';
  let deleteRequests = 0;
  const server = http.createServer((request, response) => {
    const path = new URL(request.url, 'http://localhost').pathname;
    if (path.startsWith('/api/v1/agent/stream/cancel/')) {
      response.writeHead(409, { 'content-type': 'application/json' });
      response.end(JSON.stringify({ detail: 'awaiting confirmation' }));
      return;
    }
    if (request.method === 'DELETE') {
      deleteRequests += 1;
      response.writeHead(204);
      response.end();
      return;
    }
    response.writeHead(404);
    response.end();
  });
  await new Promise((resolve) => server.listen(0, '127.0.0.1', resolve));
  try {
    const session = new QASession({
      runId: 'run-cancel-409',
      baseUrl: 'http://127.0.0.1:3000',
      apiUrl: `http://127.0.0.1:${server.address().port}/api/v1`,
      timeoutMs: 50,
      authToken: 'synthetic-token',
      secrets: ['synthetic-token'],
    });
    session.registerFixture('workspace', workspaceId);
    session.registerFixture('conversation', conversationId, { workspaceId });
    session.registerFixture('thread', threadId, { conversationId });
    session.registerActiveRun(runId, threadId);
    const cleanup = await session.cleanup();
    assert.equal(cleanup.status, 'incomplete');
    assert.equal(cleanup.activeRuns[0].status, 'error');
    assert.ok(cleanup.retained.some((item) => item.kind === 'thread' && item.id === threadId));
    assert.ok(cleanup.retained.some((item) => item.kind === 'conversation' && item.id === conversationId));
    assert.ok(cleanup.retained.some((item) => item.kind === 'workspace' && item.id === workspaceId));
    assert.equal(deleteRequests, 0);
  } finally {
    await new Promise((resolve) => server.close(resolve));
  }
});

test('thread cleanup failure retains ancestors and stops all parent deletes', async () => {
  const workspaceId = '12121212-1212-4121-8121-121212121212';
  const conversationId = '23232323-2323-4232-8232-232323232323';
  const threadId = '34343434-3434-4343-8343-343434343434';
  const deletePaths = [];
  const server = http.createServer((request, response) => {
    const path = new URL(request.url, 'http://localhost').pathname;
    if (request.method === 'DELETE') {
      deletePaths.push(path);
      if (path === `/api/v2/threads/${threadId}`) {
        response.writeHead(500, { 'content-type': 'application/json' });
        response.end(JSON.stringify({ detail: 'synthetic child delete failure' }));
        return;
      }
      response.writeHead(204);
      response.end();
      return;
    }
    response.writeHead(404);
    response.end();
  });
  await new Promise((resolve) => server.listen(0, '127.0.0.1', resolve));
  try {
    const session = new QASession({
      runId: 'run-thread-delete-failure',
      baseUrl: 'http://127.0.0.1:3000',
      apiUrl: `http://127.0.0.1:${server.address().port}/api/v1`,
      timeoutMs: 100,
      authToken: 'synthetic-token',
      secrets: ['synthetic-token'],
    });
    session.registerFixture('workspace', workspaceId);
    session.registerFixture('conversation', conversationId, { workspaceId });
    session.registerFixture('thread', threadId, { conversationId });

    const cleanup = await session.cleanup();
    assert.equal(cleanup.status, 'incomplete');
    assert.deepEqual(deletePaths, [`/api/v2/threads/${threadId}`]);
    assert.equal(cleanup.uncertainMutations.length, 1);
    assert.ok(cleanup.retained.some((item) => item.kind === 'thread' && item.id === threadId));
    assert.ok(cleanup.retained.some((item) => item.kind === 'conversation' && item.id === conversationId));
    assert.ok(cleanup.retained.some((item) => item.kind === 'workspace' && item.id === workspaceId));
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
