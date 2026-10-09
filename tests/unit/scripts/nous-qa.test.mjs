import test from 'node:test';
import assert from 'node:assert/strict';
import { mkdtemp, rm, writeFile } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';

import { isFullIdentity, main, observeSourceIdentity, parseArgs } from '../../../tests/e2e/qa/cli.mjs';
import { runCampaign, exitCodeForReport, checkExpectedBackendIdentity, sanitizeAssertionEvidence } from '../../../tests/e2e/qa/runner.mjs';
import { renderHtml } from '../../../tests/e2e/qa/report.mjs';
import { assertExactAnswer, assertIdempotentMessage, extractAnswerText, isCanonicalEmptyThreadMessageList, latestAlertLocator, registry, renderedTextPattern, smokeLogin } from '../../../tests/e2e/qa/scenarios.mjs';
import {
  FixtureLedger,
  FixtureOwnershipError,
  QASession,
  sanitizeStorageState,
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

test('latest alert locator scopes transport failures to the newest alert', () => {
  let lastCalled = false;
  const page = {
    getByRole(role, options) {
      assert.equal(role, 'alert');
      assert.equal(options, undefined);
      return {
        last() {
          lastCalled = true;
          return 'newest-alert';
        },
      };
    },
  };
  assert.equal(latestAlertLocator(page), 'newest-alert');
  assert.equal(lastCalled, true);
});

test('rejects a credential-bearing target URL before a campaign can start', async () => {
  await assert.rejects(
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

test('reproduction command redacts environment secrets', async () => {
  const config = await parseArgs(
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

/**
 * Sessions for the timeout tests. Each one quarantines independently (late
 * actions on it are rejected) and reports its own cleanup, so the runner's
 * merge across a quarantined and a fresh session is observable.
 */
function timeoutSessionFactory({ cleanupFor = () => ({ status: 'complete', retained: [], errors: [] }), failOpen = () => false } = {}) {
  const sessions = [];
  const attempts = { count: 0 };
  const factory = async () => {
    attempts.count += 1;
    const index = sessions.length + 1;
    if (failOpen(attempts.count)) throw new Error(`browser pool exhausted for session ${index}`);
    const session = {
      index,
      quarantined: false,
      lateRequestRejected: false,
      lateFixtureRejected: false,
      cleanedUp: false,
      closed: false,
      observations: [],
      artifacts: { videos: [`/tmp/ev/video/session-${index}.webm`], traces: [], checkpoints: [] },
      quarantine: async () => { session.quarantined = true; },
      request: async () => {
        if (session.quarantined) throw new Error('session quarantined');
        return { status: 200, data: {} };
      },
      registerFixture: () => {
        if (session.quarantined) throw new Error('session quarantined');
      },
      cleanup: async () => { session.cleanedUp = true; return cleanupFor(session); },
      close: async () => { session.closed = true; },
    };
    sessions.push(session);
    return session;
  };
  return { sessions, attempts, factory };
}

function timeoutRegistry(record) {
  return [
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
          session.lateRequestRejected = true;
        }
        try {
          session.registerFixture('thread', 'late-fixture');
        } catch {
          session.lateFixtureRejected = true;
        }
      },
    },
    {
      id: 'runs-after-timeout',
      title: 'Runs after timeout',
      suite: 'smoke',
      prerequisites: [],
      run: async (session) => {
        record.laterCaseSession = session;
        return { assertion: 'ran on a fresh session' };
      },
    },
  ];
}

const TIMEOUT_CONFIG = {
  suite: 'smoke',
  baseUrl: 'http://127.0.0.1:3000',
  apiUrl: 'http://127.0.0.1:8000/api/v1',
  timeoutMs: 20,
  maxTurns: 1,
};

test('scenario timeout quarantines that session, opens a fresh one and the later case still runs (Q-I8)', async () => {
  const record = {};
  const { sessions, factory } = timeoutSessionFactory({
    cleanupFor: (session) => (session.quarantined
      ? { status: 'incomplete', retained: [{ kind: 'thread', id: 'late-thread' }], errors: [{ kind: 'thread', id: 'late-thread', error: 'cleanup after quarantine failed' }] }
      : { status: 'complete', retained: [], errors: [] }),
  });
  const report = await runCampaign(
    { ...TIMEOUT_CONFIG, runId: 'run-timeout-quarantine' },
    { registry: timeoutRegistry(record), sessionFactory: factory }
  );
  await new Promise((resolve) => setTimeout(resolve, 75));
  assert.equal(report.cases[0].status, 'FAIL');
  assert.match(report.cases[0].reason, /timed out/i);
  assert.equal(sessions.length, 2, 'a fresh session is opened for the scenarios after the timeout');
  assert.equal(sessions[0].quarantined, true);
  assert.equal(sessions[0].lateRequestRejected, true);
  assert.equal(sessions[0].lateFixtureRejected, true);
  assert.equal(sessions[1].quarantined, false);
  assert.equal(record.laterCaseSession, sessions[1], 'the later case must run on the fresh session, never the quarantined one');
  assert.equal(report.cases[1].status, 'PASS');
  assert.ok(sessions.every((session) => session.cleanedUp && session.closed), 'cleanup and close cover every session');
  assert.equal(report.cleanup.status, 'incomplete', 'a quarantined session whose cleanup is not trusted keeps the campaign cleanup incomplete');
  assert.deepEqual(report.cleanup.retained, [{ kind: 'thread', id: 'late-thread' }]);
  assert.equal(report.cleanup.contexts, 2);
  assert.deepEqual(report.run.artifacts.videos, ['/tmp/ev/video/session-1.webm', '/tmp/ev/video/session-2.webm']);
  assert.equal(report.summary.failed, 1);
  assert.equal(report.summary.incomplete, true);
  assert.equal(exitCodeForReport(report), 1);
});

test('a quarantined session whose cleanup completes keeps the campaign cleanup complete (Q-I8)', async () => {
  const record = {};
  const { sessions, factory } = timeoutSessionFactory();
  const report = await runCampaign(
    { ...TIMEOUT_CONFIG, runId: 'run-timeout-clean' },
    { registry: timeoutRegistry(record), sessionFactory: factory }
  );
  await new Promise((resolve) => setTimeout(resolve, 75));
  assert.equal(sessions.length, 2);
  assert.equal(report.cases[1].status, 'PASS');
  assert.equal(report.cleanup.status, 'complete');
  assert.equal(report.summary.incomplete, false);
  assert.equal(exitCodeForReport(report), 1, 'the timed-out case is still a failure');
});

test('when no fresh session can be opened after a timeout the remaining cases fail once, without retrying the factory (Q-I8)', async () => {
  const record = {};
  // Attempt 2 fails; a retry on attempt 3 would succeed. The runner must not
  // retry: a late success would otherwise be opened and never used while the
  // remaining cases still fail with the stale reason.
  const { sessions, attempts, factory } = timeoutSessionFactory({ failOpen: (attempt) => attempt === 2 });
  const registry = [
    ...timeoutRegistry(record),
    { id: 'third', title: 'Third', suite: 'smoke', prerequisites: [], run: async (session) => { record.thirdCaseSession = session; return { assertion: 'ran' }; } },
  ];
  const report = await runCampaign(
    { ...TIMEOUT_CONFIG, runId: 'run-timeout-no-replacement' },
    { registry, sessionFactory: factory }
  );
  await new Promise((resolve) => setTimeout(resolve, 75));
  assert.equal(attempts.count, 2, 'the factory is tried once after the timeout, never again');
  assert.equal(sessions.length, 1);
  assert.equal(record.laterCaseSession, undefined);
  assert.equal(record.thirdCaseSession, undefined);
  assert.equal(report.cases[0].status, 'FAIL');
  for (const index of [1, 2]) {
    assert.equal(report.cases[index].status, 'FAIL');
    assert.match(report.cases[index].reason, /fresh session could not be opened/i);
    assert.match(report.cases[index].reason, /browser pool exhausted/);
  }
  assert.equal(sessions[0].cleanedUp, true);
  assert.equal(report.cleanup.contexts, 1);
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
    const config = await parseArgs([
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
    const config = await parseArgs([
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
    await assert.rejects(() => parseArgs([
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

test('storage state drops the frontend client-selection keys and keeps everything else', () => {
  const state = {
    cookies: [{ name: 'sb-project-auth-token', value: 'x', domain: 'qa.example.test' }],
    origins: [{
      origin: 'https://qa.example.test',
      localStorage: [
        { name: 'default-workspace-object', value: '{"id":"w"}' },
        { name: 'default-workspace-cached-at', value: '1' },
        { name: 'default-workspace-id', value: 'w' },
        { name: 'default-conversation-id', value: 'c' },
        { name: 'chat-storage', value: '{}' },
        { name: 'theme', value: 'dark' },
      ],
    }, { origin: 'https://other.example.test' }],
  };
  const sanitized = sanitizeStorageState(state);
  assert.deepEqual(sanitized.origins[0].localStorage, [{ name: 'theme', value: 'dark' }]);
  assert.deepEqual(sanitized.origins[1], { origin: 'https://other.example.test' });
  assert.deepEqual(sanitized.cookies, state.cookies);
  assert.equal(state.origins[0].localStorage.length, 6, 'the input is not mutated');
  assert.throws(() => sanitizeStorageState([]), /JSON object/);
});

test('opening the browser with --storage-state strips the client-selection keys before the context is created', async () => {
  const dir = await mkdtemp(join(tmpdir(), 'nous-qa-storage-state-'));
  const path = join(dir, 'state.json');
  await writeFile(path, JSON.stringify({
    cookies: [],
    origins: [{ origin: 'http://127.0.0.1:3000', localStorage: [
      { name: 'default-workspace-id', value: 'planted' },
      { name: 'sb-project-auth-token', value: 'keep' },
    ] }],
  }));
  const record = {};
  const page = { url: () => 'http://127.0.0.1:3000/dashboard' };
  const playwright = { chromium: { launch: async () => ({
    newContext: async (options) => { record.contextOptions = options; return { newPage: async () => page, close: async () => {} }; },
    close: async () => {},
  }) } };
  try {
    const session = new QASession({
      runId: 'run-storage-state-strip',
      baseUrl: 'http://127.0.0.1:3000',
      apiUrl: 'http://127.0.0.1:8000/api/v1',
      timeoutMs: 100,
      storageState: path,
    }, { playwright });
    await session.openBrowser();
    assert.deepEqual(record.contextOptions.storageState.origins[0].localStorage, [{ name: 'sb-project-auth-token', value: 'keep' }]);
    await writeFile(path, 'not json');
    const broken = new QASession({ runId: 'run-storage-state-broken', baseUrl: 'http://127.0.0.1:3000', apiUrl: 'http://127.0.0.1:8000/api/v1', timeoutMs: 100, storageState: path }, { playwright });
    await assert.rejects(broken.openBrowser(), /could not be read as JSON/);
  } finally {
    await rm(dir, { recursive: true, force: true });
  }
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

test('report retains local source commit and dirty state when supplied', async () => {
  const config = await parseArgs([], {
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

test('exact answer oracle accepts only bounded markdown or punctuation around the token', () => {
  assert.equal(
    assertExactAnswer(
      [{ event: 'token', data: { content: '\n`NOUS_QA_ACK`.\n' } }],
      'NOUS_QA_ACK'
    ),
    '\n`NOUS_QA_ACK`.\n'
  );
  assert.throws(
    () => assertExactAnswer([{ event: 'token', data: { content: 'The answer is NOUS_QA_ACK.' } }], 'NOUS_QA_ACK'),
    /exactly/i
  );
});

function fakeExportSession(exportText) {
  let nextId = 1;
  const id = () => `00000000-0000-4000-8000-${String(nextId++).padStart(12, '0')}`;
  return {
    login: async () => {},
    registerFixture: () => {},
    request: async (path, options = {}) => {
      if (path.startsWith('/api/v1/export/thread/')) {
        return { status: 200, headers: new Headers({ 'content-type': 'text/markdown' }), text: exportText(), data: null };
      }
      if (options.method === 'POST') return { status: 201, data: { id: id() } };
      throw new Error(`unexpected request ${path}`);
    },
  };
}

test('markdown export is not proven by the thread title alone (Q-C1)', async () => {
  const scenario = registry.find((item) => item.id === 'workflow.export-markdown');
  assert.ok(scenario, 'export scenario must remain registered');
  const evidence = { fixturePrefix: 'NOUS QA t1' };
  // backend/src/services/research/export_service.py MARKDOWN_TEMPLATE opens with
  // `# {{ thread.title }}` (which carries the fixture prefix) and renders each
  // message as `## {{ message.role | title }}` followed by its content. An
  // export with zero messages still contains the prefix in its title line.
  const titleOnly = fakeExportSession(() => [
    '# NOUS QA t1 export', '', '**Status**: active', '**Messages**: 0', '', '---', '', '---', '*Exported on 2026-10-09*', '',
  ].join('\n'));
  await assert.rejects(scenario.run(titleOnly, evidence), /seeded message body/);

  const bodyWithoutRole = fakeExportSession(() => '# NOUS QA t1 export\n\nNOUS QA t1 export marker\n');
  await assert.rejects(scenario.run(bodyWithoutRole, evidence), /User role heading/);

  const rendered = fakeExportSession(() => [
    '# NOUS QA t1 export', '', '**Messages**: 1', '', '---', '', '## User', '*2026-10-09*', '', 'NOUS QA t1 export marker', '', '---', '',
  ].join('\n'));
  const result = await scenario.run(rendered, evidence);
  assert.match(result.assertion, /User role heading/);
});

/**
 * /chat composer after a rejected upload, as the live frontend renders it:
 * - frontend/src/components/chat/ChatInput.tsx:505 ComposerPrimitive.Root is a
 *   <form> (assistant-ui) holding the Message textbox (:759-768), the
 *   `<input type="file" multiple aria-label="Attach file">` (:821-823) and the
 *   `aria-label="Attach image"` input (:849-852); `extraFileInput` adds a
 *   second "Attach file" input outside that form, as seen live
 * - ChatInput.tsx:624 `<ul aria-label="Attached files">`, chip span title
 *   `${name}, upload failed` (ChatInput.tsx:664-666)
 * - ChatInput.tsx:713-721 `<p role="status">Remove failed attachments before sending.</p>`
 * - useChatComposerActions.ts:115-118 toast.error(`Upload failed for ${name}.`),
 *   rendered by react-hot-toast as role="status" (never role="alert")
 */
function fakeAttachmentJourney({ chipFails = true, notice = true, alert = false, documents = () => [], extraFileInput = false } = {}) {
  const state = { files: [], registered: [], requests: [], filesByForm: { composer: [], other: [] } };
  const MESSAGE_TEXTBOX = Symbol('Message textbox');
  const fileInputs = [
    { ariaLabel: 'Attach file', form: 'composer' },
    { ariaLabel: 'Attach image', form: 'composer' },
    ...(extraFileInput ? [{ ariaLabel: 'Attach file', form: 'other' }] : []),
  ];
  // Playwright strictness: an action on a locator that resolves to more than
  // one element fails before anything happens.
  const inputLocator = (matches, source) => ({
    count: async () => matches.length,
    setInputFiles: async (file) => {
      if (matches.length !== 1) throw new Error(`strict mode violation: ${source} resolved to ${matches.length} elements`);
      state.files.push(file.name);
      state.filesByForm[matches[0].form].push(file.name);
    },
  });
  const visibleWhen = (count) => ({
    first: () => ({
      waitFor: async ({ state: wanted }) => {
        if (wanted === 'visible' && count() === 0) throw new Error('synthetic locator timeout');
      },
    }),
    count: async () => count(),
  });
  const page = {
    url: () => 'http://127.0.0.1:3000/chat',
    getByLabel: (label, options = {}) => {
      const matches = fileInputs.filter((input) => (options.exact ? input.ariaLabel === label : input.ariaLabel.includes(label)));
      return inputLocator(matches, `getByLabel('${label}')`);
    },
    locator: (selector) => {
      if (selector === 'form') {
        return {
          filter: ({ has }) => {
            // Only the composer form contains the Message textbox.
            const forms = has === MESSAGE_TEXTBOX ? ['composer'] : [];
            return {
              locator: (inner) => {
                if (inner !== 'input[type="file"][aria-label="Attach file"]') throw new Error(`unexpected locator ${inner}`);
                return inputLocator(fileInputs.filter((input) => forms.includes(input.form) && input.ariaLabel === 'Attach file'), `form >> ${inner}`);
              },
            };
          },
        };
      }
      if (selector === 'ul[aria-label="Attached files"]') {
        return {
          getByTitle: (title, options = {}) => visibleWhen(() => (
            chipFails && options.exact === true && state.files.some((name) => `${name}, upload failed` === title) ? 1 : 0
          )),
        };
      }
      throw new Error(`unexpected locator ${selector}`);
    },
    getByRole: (role, options = {}) => {
      if (role === 'textbox' && options.name === 'Message' && options.exact === true) return MESSAGE_TEXTBOX;
      if (role === 'alert') return { last: () => visibleWhen(() => (alert ? 1 : 0)).first() };
      if (role === 'status') {
        return {
          filter: ({ hasText }) => visibleWhen(() => (
            state.files.length > 0 && (
              (notice && hasText === 'Remove failed attachments before sending.')
              || state.files.some((name) => hasText === `Upload failed for ${name}.`)
            ) ? 1 : 0
          )),
        };
      }
      throw new Error(`unexpected getByRole ${role}`);
    },
  };
  const session = {
    config: { timeoutMs: 60 },
    page,
    login: async () => page,
    goto: async () => {},
    assertTrustedBrowserUrl: () => {},
    registerFixture: (kind, id, metadata) => state.registered.push([kind, id, metadata]),
    request: async (path) => {
      state.requests.push(path);
      if (path.startsWith('/api/v1/files/?')) return { status: 200, data: { files: documents(state), total: 0, page: 1, size: 50, has_more: false } };
      throw new Error(`unexpected request ${path}`);
    },
  };
  return { state, session, evidence: { fixturePrefix: 'NOUS QA t1' } };
}

test('unsupported attachment is not proven by a generic alert; it needs the composer failure chip (Q-C2)', async () => {
  const scenario = registry.find((item) => item.id === 'adversarial.unsupported-attachment');
  assert.ok(scenario, 'unsupported-attachment scenario must remain registered');
  const alertOnly = fakeAttachmentJourney({ chipFails: false, notice: false, alert: true });
  const started = Date.now();
  await assert.rejects(scenario.run(alertOnly.session, alertOnly.evidence), /NOUS_QA_t1\.exe, upload failed/);
  assert.ok(Date.now() - started < alertOnly.session.config.timeoutMs, 'the chip wait must be bounded below the scenario timeout');
});

test('unsupported attachment fails when a document was created anyway and registers it for cleanup (Q-C2)', async () => {
  const scenario = registry.find((item) => item.id === 'adversarial.unsupported-attachment');
  const leakedId = '55555555-5555-4555-8555-555555555555';
  const leaked = fakeAttachmentJourney({
    documents: () => [{ id: leakedId, filename: 'NOUS_QA_t1.exe', title: 'NOUS_QA_t1.exe' }],
  });
  await assert.rejects(scenario.run(leaked.session, leaked.evidence), /created 1 document/);
  assert.deepEqual(leaked.state.registered, [['document', leakedId, { filename: 'NOUS_QA_t1.exe' }]]);
  assert.ok(leaked.state.requests.some((path) => path === '/api/v1/files/?search=NOUS_QA_t1.exe&size=50'), 'the document search must target the exact fixture filename');
});

test('unsupported attachment targets the composer\'s own Attach file input when another file input is on the page (Q-C2)', async () => {
  const scenario = registry.find((item) => item.id === 'adversarial.unsupported-attachment');
  const crowded = fakeAttachmentJourney({ extraFileInput: true });
  const result = await scenario.run(crowded.session, crowded.evidence);
  assert.match(result.assertion, /no document was created/);
  assert.deepEqual(crowded.state.filesByForm, { composer: ['NOUS_QA_t1.exe'], other: [] }, 'the file must reach the composer input, never the stray one');
});

test('unsupported attachment passes only with the failed chip, the status notice and no created document (Q-C2)', async () => {
  const scenario = registry.find((item) => item.id === 'adversarial.unsupported-attachment');
  const clean = fakeAttachmentJourney();
  const result = await scenario.run(clean.session, clean.evidence);
  assert.match(result.assertion, /no document was created/);
  assert.deepEqual(clean.state.registered, []);
  const noNotice = fakeAttachmentJourney({ notice: false });
  await assert.rejects(scenario.run(noNotice.session, noNotice.evidence), /Remove failed attachments before sending/);
});

/**
 * /chat sidebar and composer as the live frontend renders them:
 * - frontend/src/components/chat/ChatSidebar.tsx:307 `<input aria-label="Search threads">`
 * - ChatSidebar.tsx:454 thread rows are `button.sb-conv`; their text is the thread title
 * - frontend/src/components/chat/ChatInput.tsx:768 the composer textbox is `aria-label="Message"`
 * - ChatSidebar.tsx:565-574 a failed server search (POST /api/v2/search/threads,
 *   useThreadSearch.ts) renders `<div role="alert">` "Search failed. Try again."
 *   instead of rows
 * - frontend/src/hooks/chat/useChatSession.ts:803-818 an unavailable `?thread=`
 *   falls back to the newest listed thread, shows toast.error('That
 *   conversation is no longer available.') (react-hot-toast: role="status")
 *   and router.replace()s the URL
 * The workspace whose threads the sidebar lists is resolved the way
 * frontend/src/services/workspaceService.ts _resolveDefaultWorkspace does
 * without a cached selection: highest collection_count + conversation_count
 * from GET /api/v2/workspaces, first on a tie. The fake has no localStorage
 * cache, matching a runner that never plants one and strips it from
 * --storage-state (session.mjs sanitizeStorageState).
 */
function fakeHistoryJourney({ workspaces, sidebarLists = true, staleRecovery = 'fallback', searchOutcome = { status: 200 } } = {}) {
  const state = {
    workspaces: workspaces.map((workspace) => ({ ...workspace })),
    conversations: new Map(),
    threads: new Map(),
    registered: [],
    posts: [],
    threadId: null,
    search: '',
    searchError: false,
    responseWaiters: [],
    drafts: new Map(),
    toasts: [],
    messages: [],
  };
  let nextId = 1;
  const id = () => `00000000-0000-4000-8000-${String(nextId++).padStart(12, '0')}`;
  const score = (workspace) => (workspace.collection_count ?? 0) + (workspace.conversation_count ?? 0);
  const appWorkspace = () => state.workspaces.reduce((best, current) => (score(current) > score(best) ? current : best), state.workspaces[0]);
  const workspaceThreads = () => [...state.threads.values()].filter((thread) => state.conversations.get(thread.conversationId) === appWorkspace().id);
  const listedThreads = () => (sidebarLists && !state.searchError ? workspaceThreads().filter((thread) => thread.title.includes(state.search)) : []);
  const visibleWhen = (count, onClick) => ({
    first: () => ({
      waitFor: async ({ state: wanted }) => {
        if (wanted === 'visible' && count() === 0) throw new Error('synthetic locator timeout');
      },
      isVisible: async () => count() > 0,
      click: async () => onClick?.(),
    }),
    count: async () => count(),
  });
  const page = {
    url: () => `http://127.0.0.1:3000/chat${state.threadId ? `?thread=${state.threadId}` : ''}`,
    waitForResponse: (predicate, options = {}) => new Promise((resolve, reject) => {
      state.responseWaiters.push({ predicate, resolve });
      setTimeout(() => reject(new Error('synthetic waitForResponse timeout')), options.timeout ?? 50);
    }),
    // No scenario may steer /chat through localStorage; a page.evaluate that
    // tried to plant a cache is a no-op here.
    evaluate: async () => ({}),
    locator: (selector) => {
      if (selector === 'button.sb-conv') {
        return {
          filter: ({ hasText }) => visibleWhen(
            () => listedThreads().filter((thread) => thread.title.includes(hasText)).length,
            () => {
              const target = listedThreads().find((thread) => thread.title.includes(hasText));
              if (!target) throw new Error('no sidebar row to click');
              state.threadId = target.id;
            }
          ),
        };
      }
      throw new Error(`unexpected locator ${selector}`);
    },
    getByLabel: (label) => {
      if (label === 'Search threads') {
        return {
          fill: async (value) => {
            state.search = value;
            // useThreadSearch.ts: queries of two or more characters hit the
            // server; an emptied box shows the plain list again.
            if (value.trim().length < 2) {
              state.searchError = false;
              return;
            }
            const response = { url: () => 'http://127.0.0.1:3000/api/v2/search/threads', status: () => searchOutcome.status };
            state.searchError = searchOutcome.status >= 400;
            for (const waiter of state.responseWaiters.splice(0)) if (waiter.predicate(response)) waiter.resolve(response);
          },
          inputValue: async () => state.search,
        };
      }
      throw new Error(`unexpected getByLabel ${label}`);
    },
    getByRole: (role, options = {}) => {
      if (role === 'alert') return { filter: ({ hasText }) => visibleWhen(() => (state.searchError && hasText === 'Search failed. Try again.' ? 1 : 0)) };
      if (role === 'textbox' && options.name === 'Message') {
        return {
          fill: async (value) => { state.drafts.set(state.threadId, value); },
          inputValue: async () => state.drafts.get(state.threadId) ?? '',
        };
      }
      if (role === 'status') return { filter: ({ hasText }) => visibleWhen(() => state.toasts.filter((text) => text.includes(hasText)).length) };
      throw new Error(`unexpected getByRole ${role}`);
    },
  };
  const session = {
    config: { timeoutMs: 60 },
    page,
    ledger: {
      requireOwned: (kind, fixtureId) => {
        if (!state.registered.some(([k, i]) => k === kind && i === fixtureId)) throw new Error(`unowned ${kind} ${fixtureId}`);
      },
    },
    login: async () => page,
    goto: async (path) => {
      const requested = new URL(path, 'http://127.0.0.1:3000').searchParams.get('thread');
      if (requested && !state.threads.has(requested)) {
        if (staleRecovery === 'none') {
          state.threadId = requested;
        } else {
          const newest = workspaceThreads().at(-1);
          state.threadId = newest?.id ?? null;
          if (staleRecovery === 'fallback') state.toasts.push('That conversation is no longer available.');
        }
      } else {
        state.threadId = requested;
      }
      return { status: () => 200 };
    },
    registerFixture: (kind, fixtureId, metadata) => state.registered.push([kind, fixtureId, metadata]),
    request: async (path, options = {}) => {
      const method = options.method ?? 'GET';
      if (method === 'GET' && path === '/api/v2/workspaces') return { status: 200, data: state.workspaces.map((workspace) => ({ ...workspace })) };
      const threadMessages = /^\/api\/v2\/threads\/([^/?]+)\/messages/.exec(path);
      if (method === 'GET' && threadMessages) {
        return { status: 200, data: { messages: state.messages.filter((item) => item.thread_id === threadMessages[1]) } };
      }
      const threadDetail = /^\/api\/v2\/threads\/([^/?]+)$/.exec(path);
      if (method === 'GET' && threadDetail) {
        if (state.threads.has(threadDetail[1])) return { status: 200, data: { id: threadDetail[1] } };
        const error = new Error('Request failed (404)');
        error.status = 404;
        throw error;
      }
      if (method !== 'POST') throw new Error(`unexpected request ${method} ${path}`);
      state.posts.push(path);
      if (path === '/api/v2/workspaces') {
        const workspace = { id: id(), name: options.json.name, collection_count: 0, conversation_count: 0 };
        state.workspaces.push(workspace);
        return { status: 201, data: { id: workspace.id } };
      }
      const conversation = /^\/api\/v2\/workspaces\/([^/]+)\/conversations$/.exec(path);
      if (conversation) {
        const conversationId = id();
        state.conversations.set(conversationId, conversation[1]);
        const workspace = state.workspaces.find((item) => item.id === conversation[1]);
        if (workspace) workspace.conversation_count = (workspace.conversation_count ?? 0) + 1;
        return { status: 201, data: { id: conversationId } };
      }
      if (path === '/api/v2/threads') {
        const threadId = id();
        state.threads.set(threadId, { id: threadId, title: options.json.title, conversationId: options.json.conversation_id });
        return { status: 201, data: { id: threadId } };
      }
      if (path === '/api/v2/messages') {
        const message = { id: id(), ...options.json };
        state.messages.push(message);
        return { status: 201, data: message };
      }
      throw new Error(`unexpected request POST ${path}`);
    },
  };
  return { state, session, evidence: { fixturePrefix: 'NOUS QA t1' } };
}

const MY_WORKSPACE = '11111111-1111-4111-8111-111111111111';
const BUSY_WORKSPACE = '22222222-2222-4222-8222-222222222222';

test('history scenario builds its fixtures inside the workspace /chat opens and never registers that workspace (Q-I1)', async () => {
  const scenario = registry.find((item) => item.id === 'workflow.history-search-and-draft-isolation');
  assert.ok(scenario, 'history scenario must remain registered');
  const journey = fakeHistoryJourney({ workspaces: [
    { id: MY_WORKSPACE, name: 'My Workspace', collection_count: 0, conversation_count: 1 },
    { id: BUSY_WORKSPACE, name: 'Research', collection_count: 2, conversation_count: 3 },
  ] });
  const result = await scenario.run(journey.session, journey.evidence);
  assert.match(result.assertion, /workspace \/chat opens/);
  assert.deepEqual(
    journey.state.posts.filter((path) => path.includes('/conversations')),
    [`/api/v2/workspaces/${BUSY_WORKSPACE}/conversations`, `/api/v2/workspaces/${BUSY_WORKSPACE}/conversations`]
  );
  assert.ok(!journey.state.posts.includes('/api/v2/workspaces'), 'the scenario must not create a workspace of its own');
  assert.deepEqual(journey.state.registered.map(([kind]) => kind), ['conversation', 'thread', 'conversation', 'thread']);
});

test('history scenario keeps the first workspace on a score tie, as the frontend does (Q-I1)', async () => {
  const scenario = registry.find((item) => item.id === 'workflow.history-search-and-draft-isolation');
  const journey = fakeHistoryJourney({ workspaces: [
    { id: MY_WORKSPACE, name: 'My Workspace', collection_count: 1, conversation_count: 1 },
    { id: BUSY_WORKSPACE, name: 'Research', collection_count: 0, conversation_count: 2 },
  ] });
  await scenario.run(journey.session, journey.evidence);
  assert.ok(journey.state.posts.every((path) => !path.includes('/conversations') || path === `/api/v2/workspaces/${MY_WORKSPACE}/conversations`));
});

test('history scenario fails fast and names the search response when the sidebar search errors (Q-I1)', async () => {
  const scenario = registry.find((item) => item.id === 'workflow.history-search-and-draft-isolation');
  const journey = fakeHistoryJourney({
    workspaces: [{ id: MY_WORKSPACE, name: 'My Workspace', collection_count: 0, conversation_count: 1 }],
    searchOutcome: { status: 500 },
  });
  const started = Date.now();
  await assert.rejects(
    scenario.run(journey.session, journey.evidence),
    // assert.rejects tests the RegExp against String(error), i.e. "Error: ...".
    /: Sidebar thread search failed \(UI error state; POST \/api\/v2\/search\/threads returned 500\)$/
  );
  assert.ok(Date.now() - started < journey.session.config.timeoutMs, 'the error state must end the wait at once');
});

test('history scenario fails fast and names the missing sidebar row (Q-I1)', async () => {
  const scenario = registry.find((item) => item.id === 'workflow.history-search-and-draft-isolation');
  const journey = fakeHistoryJourney({ workspaces: [{ id: MY_WORKSPACE, name: 'My Workspace', collection_count: 0, conversation_count: 1 }], sidebarLists: false });
  const started = Date.now();
  await assert.rejects(scenario.run(journey.session, journey.evidence), /Sidebar row "NOUS QA t1 history-a" did not appear within \d+ms/);
  assert.ok(Date.now() - started < journey.session.config.timeoutMs, 'the sidebar wait must be bounded below the scenario timeout');
});

test('missing-thread recovers to an owned thread with the unavailable toast and a 200 thread GET (Q-C3)', async () => {
  const scenario = registry.find((item) => item.id === 'adversarial.missing-thread-ui');
  assert.ok(scenario, 'missing-thread scenario must remain registered');
  const journey = fakeHistoryJourney({ workspaces: [
    { id: MY_WORKSPACE, name: 'My Workspace', collection_count: 0, conversation_count: 1 },
    { id: BUSY_WORKSPACE, name: 'Research', collection_count: 2, conversation_count: 3 },
  ] });
  const result = await scenario.run(journey.session, journey.evidence);
  assert.equal(result.status, undefined, 'a recovered missing thread is a PASS');
  assert.match(result.assertion, /recovered thread reads 200/);
  const [detail] = result.evidence;
  assert.match(detail.recoveredThreadId, /^00000000-0000-4000-8000-/);
  assert.notEqual(detail.recoveredThreadId, '00000000-0000-4000-8000-000000000000');
  assert.equal(detail.recoveredFixture, true);
  assert.ok(!journey.state.posts.includes('/api/v2/workspaces'), 'the scenario must not create a workspace of its own');
  assert.deepEqual(journey.state.posts.filter((path) => path.includes('/conversations')), [`/api/v2/workspaces/${BUSY_WORKSPACE}/conversations`]);
  assert.deepEqual(journey.state.registered.map(([kind]) => kind), ['conversation', 'thread']);
});

test('missing-thread fails fast when the URL keeps the stale id (Q-C3)', async () => {
  const scenario = registry.find((item) => item.id === 'adversarial.missing-thread-ui');
  const journey = fakeHistoryJourney({ workspaces: [{ id: MY_WORKSPACE, name: 'My Workspace', collection_count: 0, conversation_count: 1 }], staleRecovery: 'none' });
  const started = Date.now();
  await assert.rejects(scenario.run(journey.session, journey.evidence), /stale thread id|That conversation is no longer available/);
  assert.ok(Date.now() - started < journey.session.config.timeoutMs, 'the recovery wait must be bounded below the scenario timeout');
});

test('missing-thread fails when the app recovers without the unavailable toast (Q-C3)', async () => {
  const scenario = registry.find((item) => item.id === 'adversarial.missing-thread-ui');
  const journey = fakeHistoryJourney({ workspaces: [{ id: MY_WORKSPACE, name: 'My Workspace', collection_count: 0, conversation_count: 1 }], staleRecovery: 'silent' });
  await assert.rejects(scenario.run(journey.session, journey.evidence), /Toast "That conversation is no longer available\." did not appear within \d+ms/);
});

/**
 * Stop journey: the fake stream reports `accepted` and a first token, then
 * stays open until Stop is requested (or finishes first when
 * `completesBeforeStop`). The transcript renders the stopped answer the way
 * the live markdown renderer does (fences and line breaks gone) and shows the
 * live stopped marker `title="You stopped this response; the text above is
 * partial."`; the Stop control is `aria-label="Stop agent"`.
 */
function fakeStopJourney({
  cancel = () => ({ status: 204 }),
  job = () => ({ status: 'cancelled' }),
  completesBeforeStop = false,
  streamNeverSettles = false,
  stoppedContent = '```\n1\n2\n3\n4\n5\n6\n7\n8\n9\n10\n11\n12\n```',
  rendered = '1 2 3 4 5 6 7 8 9 10 11 12',
} = {}) {
  const runId = '77777777-7777-4777-8777-777777777777';
  const state = { prompt: null, cancelRequests: 0, terminalRuns: [], registered: [] };
  let release;
  const released = new Promise((resolve) => { release = resolve; });
  let nextId = 1;
  const id = () => `00000000-0000-4000-8000-${String(nextId++).padStart(12, '0')}`;
  const normalize = (text) => String(text).replace(/\s+/g, ' ').trim();
  // Playwright hasText: a string is a case-insensitive, whitespace-normalized
  // substring; a RegExp is tested against the normalized text.
  const matches = (hasText, text) => (hasText instanceof RegExp
    ? hasText.test(normalize(text))
    : normalize(text).toLowerCase().includes(normalize(hasText).toLowerCase()));
  const visibleWhen = (count) => ({
    first: () => ({
      waitFor: async ({ state: wanted }) => {
        if (wanted === 'visible' && count() === 0) throw new Error('synthetic locator timeout');
      },
    }),
    count: async () => count(),
  });
  const page = {
    url: () => 'http://127.0.0.1:3000/chat',
    waitForFunction: async () => {},
    reload: async () => {},
    locator: (selector) => {
      if (selector === '[data-role="assistant"]') return { filter: ({ hasText }) => visibleWhen(() => (matches(hasText, rendered) ? 1 : 0)) };
      if (selector === '[data-role="user"], [data-role="assistant"]') return { filter: ({ hasText }) => visibleWhen(() => (matches(hasText, rendered) ? 1 : 0)) };
      if (selector === '[title="You stopped this response; the text above is partial."]') return visibleWhen(() => 1);
      throw new Error(`unexpected locator ${selector}`);
    },
    getByLabel: (label) => {
      if (label === 'Stop agent') return { count: async () => 0, isVisible: async () => false };
      throw new Error(`unexpected getByLabel ${label}`);
    },
  };
  const events = [
    { event: 'status', data: { phase: 'accepted', run_id: runId } },
    { event: 'token', data: { content: '1\n' } },
    { event: 'done', data: { status: 'complete' } },
  ];
  const session = {
    config: { timeoutMs: 60 },
    page,
    login: async () => page,
    goto: async () => {},
    assertTrustedBrowserUrl: () => {},
    registerFixture: (kind, fixtureId) => state.registered.push([kind, fixtureId]),
    markRunTerminal: (terminalRunId) => state.terminalRuns.push(terminalRunId),
    streamAgent: async (payload, options = {}) => {
      state.prompt = payload.messages[0].content;
      await options.onEvent(events[0]);
      await options.onEvent(events[1]);
      if (!completesBeforeStop) await released;
      return { status: 200, events, terminal: true, acceptedRunIds: [runId] };
    },
    request: async (path, options = {}) => {
      if (options.method === 'POST' && path.startsWith('/api/v1/agent/stream/cancel/')) {
        state.cancelRequests += 1;
        // The run reaches its terminal state as Stop arrives; the SSE reader
        // may still hang (streamNeverSettles).
        if (!streamNeverSettles) release();
        const outcome = cancel(state);
        if (outcome.status >= 400) {
          const error = new Error(`Request failed (${outcome.status})`);
          error.status = outcome.status;
          throw error;
        }
        return outcome;
      }
      if (options.method === 'POST') return { status: 201, data: { id: id() } };
      if (path.startsWith('/api/v1/agent/jobs/')) return { status: 200, data: job(state) };
      if (path.includes('/messages')) {
        return { status: 200, data: { messages: [{ role: 'user', content: state.prompt }, { role: 'assistant', content: stoppedContent, stopped: true }] } };
      }
      if (path.startsWith('/api/v1/agent/stream/resume/')) return { status: 204, data: null };
      throw new Error(`unexpected request ${path}`);
    },
  };
  return { state, session, evidence: { fixturePrefix: 'NOUS QA t1', consumeModelTurn: () => 1 }, runId };
}

test('stop-active-run is BLOCKED, not FAIL, when Stop returns 409 because the run already completed (Q-I4)', async () => {
  const scenario = registry.find((item) => item.id === 'workflow.stop-active-run');
  assert.ok(scenario, 'stop scenario must remain registered');
  const raced = fakeStopJourney({ cancel: () => ({ status: 409 }), job: () => ({ status: 'completed' }) });
  const result = await scenario.run(raced.session, raced.evidence);
  assert.equal(result.status, 'BLOCKED');
  assert.equal(result.reason, 'run completed before Stop');
  assert.ok(raced.state.terminalRuns.includes(raced.runId), 'a completed run is proven terminal so cleanup does not retain its fixture tree');
  assert.match(raced.state.prompt, /one per line/, 'the prompt must force output long enough for Stop to land mid-stream');
});

test('stop-active-run is BLOCKED when the stream finishes before Stop can be sent and the job confirms completed (Q-I4)', async () => {
  const scenario = registry.find((item) => item.id === 'workflow.stop-active-run');
  const early = fakeStopJourney({ completesBeforeStop: true, job: () => ({ status: 'completed' }) });
  const result = await scenario.run(early.session, early.evidence);
  assert.equal(result.status, 'BLOCKED');
  assert.equal(result.reason, 'run completed before Stop');
  assert.equal(early.state.cancelRequests, 0);
});

test('stop-active-run fails when the stream reports done before Stop but the job is not completed (Q-I4)', async () => {
  const scenario = registry.find((item) => item.id === 'workflow.stop-active-run');
  const contradicted = fakeStopJourney({ completesBeforeStop: true, job: () => ({ status: 'running' }) });
  await assert.rejects(scenario.run(contradicted.session, contradicted.evidence), /ended before Stop \(done=true\) while the run status was running/);
});

test('stop-active-run bounds the stream wait after a 409 on a completed run (Q-I4)', async () => {
  const scenario = registry.find((item) => item.id === 'workflow.stop-active-run');
  const hung = fakeStopJourney({ cancel: () => ({ status: 409 }), job: () => ({ status: 'completed' }), streamNeverSettles: true });
  const started = Date.now();
  const result = await scenario.run(hung.session, hung.evidence);
  assert.equal(result.status, 'BLOCKED');
  assert.ok(Date.now() - started < 1_000, 'a hung SSE reader must not hold the BLOCKED verdict beyond the inner bound');
});

test('stop-active-run still fails on a 409 whose run is not complete (Q-I4)', async () => {
  const scenario = registry.find((item) => item.id === 'workflow.stop-active-run');
  const conflicted = fakeStopJourney({ cancel: () => ({ status: 409 }), job: () => ({ status: 'running' }) });
  await assert.rejects(scenario.run(conflicted.session, conflicted.evidence), /409/);
});

test('stop-active-run matches the stopped answer as the transcript renders it, not as raw markdown (Q-I4)', async () => {
  const scenario = registry.find((item) => item.id === 'workflow.stop-active-run');
  const stopped = fakeStopJourney();
  const result = await scenario.run(stopped.session, stopped.evidence);
  assert.equal(result.status, undefined, 'a durable cancellation is a PASS');
  assert.equal(stopped.state.cancelRequests, 1);
  assert.ok(stopped.state.terminalRuns.includes(stopped.runId));
});

test('rendered text pattern strips markdown and tolerates collapsed or dropped line breaks (Q-I4)', () => {
  const pattern = renderedTextPattern('```text\n**1**\n2\n- 3\n4. 4\n`5`\n6\n7\n8\n9\n```');
  assert.match('1 2 3 4 5 6 7 8', pattern, 'paragraph rendering (soft breaks become spaces)');
  assert.match('12345678', pattern, 'hard-break rendering (text nodes adjoin)');
  assert.doesNotMatch('1 2 3 5 6 7 8 9', pattern, 'a missing word must not match');
  assert.doesNotMatch('```text **1** 2', pattern, 'raw markdown is not what the transcript shows');
  assert.throws(() => renderedTextPattern('```\n```'), /no renderable text/);
});

test('rendered text pattern for a one-word stop requires the word to stand alone (Q-I4)', () => {
  const single = renderedTextPattern('**1**\n');
  assert.match('1', single);
  assert.match('You stopped 1', single);
  assert.doesNotMatch('10', single, 'a one-token stop must not match a longer number');
  assert.doesNotMatch('12:30', single);
  assert.doesNotMatch('11 12', single);
  // A words bound below two never reduces a multi-word answer to one token.
  const two = renderedTextPattern('1\n2\n3', 1);
  assert.match('1 2', two);
  assert.doesNotMatch('10 20', two);
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
