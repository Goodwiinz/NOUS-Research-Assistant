import test from 'node:test';
import assert from 'node:assert/strict';
import { mkdtemp, readFile, rm, writeFile } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';

import {
  featuresForChangedPaths,
  globToRegExp,
  loadFeatureMap,
  parseFeatureMap,
  scenariosForFeatures,
} from '../../../tests/e2e/qa/feature-map.mjs';
import { CLIConfigError, main, parseArgs } from '../../../tests/e2e/qa/cli.mjs';
import { QASession } from '../../../tests/e2e/qa/session.mjs';
import { runCampaign } from '../../../tests/e2e/qa/runner.mjs';
import { renderEvidenceReadme, writeEvidenceRecord } from '../../../tests/e2e/qa/evidence.mjs';
import { registry, smokeLogin } from '../../../tests/e2e/qa/scenarios.mjs';

const MAP = `
version: 1
features:
  - id: login
    status: covered
    surfaces: { web: ["/login"], api: [], cli: [] }
    states: [done]
    pass_criteria: ["renders"]
    scenarios: [smoke.login-availability]
    owns: ["frontend/app/(auth)/login/**"]
  - id: chat
    status: covered
    surfaces: { web: ["/chat"], api: [], cli: [] }
    states: [done]
    pass_criteria: ["streams"]
    scenarios: [workflow.reload-persistence, workflow.chat-send-stream-reload]
    owns: ["frontend/src/components/chat/**", "backend/src/api/agent/**"]
ignore: { pages: [], routers: [] }
`;

test('globToRegExp uses fnmatch semantics where * crosses slashes', () => {
  assert.ok(globToRegExp('frontend/app/(auth)/login/**').test('frontend/app/(auth)/login/page.tsx'));
  assert.ok(globToRegExp('backend/src/api/agent/*').test('backend/src/api/agent/sub/execute.py'));
  assert.ok(!globToRegExp('frontend/app/(auth)/login/**').test('frontend/app/page.tsx'));
  assert.ok(globToRegExp('a?c').test('abc'));
  assert.ok(!globToRegExp('a.c').test('abc'), 'regex metacharacters are literal');
});

test('scenariosForFeatures returns unique ids in map order and rejects unknown features', () => {
  const map = parseFeatureMap(MAP);
  assert.deepEqual(scenariosForFeatures(map, ['chat', 'login']), [
    'smoke.login-availability', 'workflow.reload-persistence', 'workflow.chat-send-stream-reload',
  ]);
  assert.throws(() => scenariosForFeatures(map, ['nope']), /Unknown feature: nope/);
});

test('featuresForChangedPaths maps changed files to owning features', () => {
  const map = parseFeatureMap(MAP);
  assert.deepEqual(featuresForChangedPaths(map, ['backend/src/api/agent/execute.py', 'README.md']), ['chat']);
  assert.deepEqual(featuresForChangedPaths(map, ['README.md']), []);
});

test('parseFeatureMap rejects a map without version 1 and a features list', () => {
  assert.throws(() => parseFeatureMap('version: 2\nfeatures: []\n'), /version 1/);
  assert.throws(() => parseFeatureMap('version: 1\n'), /features list/);
});

test('loadFeatureMap reads YAML from disk', async () => {
  const dir = await mkdtemp(join(tmpdir(), 'nous-verify-'));
  try {
    const path = join(dir, 'feature-map.yaml');
    await writeFile(path, MAP, 'utf8');
    const map = await loadFeatureMap(path);
    assert.equal(map.features.length, 2);
  } finally {
    await rm(dir, { recursive: true, force: true });
  }
});

async function withMap(work) {
  const dir = await mkdtemp(join(tmpdir(), 'nous-verify-'));
  try {
    const mapPath = join(dir, 'feature-map.yaml');
    await writeFile(mapPath, MAP, 'utf8');
    return await work(mapPath, dir);
  } finally {
    await rm(dir, { recursive: true, force: true });
  }
}

test('--features selects the mapped scenarios across suites', async () => {
  await withMap(async (mapPath) => {
    const config = await parseArgs(['--features', 'login,chat'], { NOUS_QA_FEATURE_MAP: mapPath });
    assert.equal(config.suite, 'all');
    assert.deepEqual(config.selectedIds, ['smoke.login-availability', 'workflow.reload-persistence', 'workflow.chat-send-stream-reload']);
    assert.deepEqual(config.features, ['login', 'chat']);
    assert.match(config.evidenceDir, /\.verify-artifacts[\\/]/);
    assert.ok(config.evidenceDir.endsWith(config.runId));
    assert.equal(config.evidenceRecordDir, null);
  });
});

test('--features rejects an unknown feature as a configuration error', async () => {
  await withMap(async (mapPath) => {
    await assert.rejects(parseArgs(['--features', 'nope'], { NOUS_QA_FEATURE_MAP: mapPath }), (error) => error instanceof CLIConfigError && /Unknown feature: nope/.test(error.message));
  });
});

test('--changed-from maps changed paths to features', async () => {
  await withMap(async (mapPath) => {
    const seen = [];
    const config = await parseArgs(['--changed-from', 'origin/develop'], { NOUS_QA_FEATURE_MAP: mapPath }, {
      changedPaths: async (ref) => { seen.push(ref); return ['backend/src/api/agent/execute.py']; },
    });
    assert.deepEqual(seen, ['origin/develop']);
    assert.deepEqual(config.features, ['chat']);
    assert.deepEqual(config.selectedIds, ['workflow.reload-persistence', 'workflow.chat-send-stream-reload']);
  });
});

test('--changed-from with no mapped change is a configuration error, not a pass', async () => {
  await withMap(async (mapPath) => {
    await assert.rejects(
      parseArgs(['--changed-from', 'HEAD'], { NOUS_QA_FEATURE_MAP: mapPath }, { changedPaths: async () => ['README.md'] }),
      /No mapped feature changed since HEAD/,
    );
  });
});

test('--changed-from refuses option-like or malformed refs', async () => {
  await withMap(async (mapPath) => {
    await assert.rejects(
      parseArgs(['--changed-from', 'a b'], { NOUS_QA_FEATURE_MAP: mapPath }, { changedPaths: async () => [] }),
      /--changed-from must be a git ref/,
    );
  });
});

test('--evidence-dir and --evidence-record are resolved', async () => {
  const config = await parseArgs(['--evidence-dir', 'tmp-ev', '--evidence-record', 'docs/testing/evidence/verify-login-20260101'], {});
  assert.match(config.evidenceDir, /tmp-ev$/);
  assert.match(config.evidenceRecordDir, /verify-login-20260101$/);
});

test('main reports a selection error with exit code 2 when no mapped feature changed', async () => {
  await withMap(async (mapPath) => {
    const original = console.error;
    const lines = [];
    console.error = (line) => lines.push(line);
    try {
      const code = await main(['--changed-from', 'HEAD'], { NOUS_QA_FEATURE_MAP: mapPath }, { changedPaths: async () => [] });
      assert.equal(code, 2);
      assert.match(lines.join('\n'), /No mapped feature changed since HEAD/);
    } finally {
      console.error = original;
    }
  });
});

function fakePlaywright(record) {
  const page = {
    url: () => 'http://127.0.0.1:3000/login',
    goto: async () => ({}),
    screenshot: async (options) => { record.screenshots.push(options); },
    video: () => ({ path: async () => '/tmp/fake.webm' }),
  };
  const context = {
    tracing: {
      start: async (options) => { record.tracingStart = options; },
      stop: async (options) => { record.tracingStop = options; },
    },
    newPage: async () => page,
    close: async () => { record.contextClosed = true; },
  };
  const browser = { newContext: async (options) => { record.contextOptions = options; return context; }, close: async () => {} };
  return { chromium: { launch: async () => browser } };
}

function sessionConfig(evidenceDir) {
  return { baseUrl: 'http://127.0.0.1:3000', apiUrl: 'http://127.0.0.1:3000/api/v1', runId: 'r1', timeoutMs: 100, evidenceDir };
}

async function withEvidenceDir(work) {
  const dir = await mkdtemp(join(tmpdir(), 'nous-verify-ev-'));
  try {
    return await work(dir);
  } finally {
    await rm(dir, { recursive: true, force: true });
  }
}

test('browser context records video and trace into the evidence directory', async () => {
  await withEvidenceDir(async (dir) => {
    const record = { screenshots: [] };
    const session = new QASession(sessionConfig(dir), { playwright: fakePlaywright(record) });
    await session.openBrowser();
    assert.equal(record.contextOptions.recordVideo.dir, join(dir, 'video'));
    assert.equal(record.contextOptions.baseURL, 'http://127.0.0.1:3000');
    assert.deepEqual(record.tracingStart, { screenshots: true, snapshots: true });
    await session.close();
    assert.equal(record.tracingStop.path, join(dir, 'trace.zip'));
    assert.deepEqual(session.artifacts.traces, ['trace.zip']);
    assert.deepEqual(session.artifacts.videos, ['/tmp/fake.webm']);
  });
});

test('a session without an evidence directory records no video or trace', async () => {
  const record = { screenshots: [] };
  const session = new QASession(sessionConfig(undefined), { playwright: fakePlaywright(record) });
  await session.openBrowser();
  assert.equal(record.contextOptions.recordVideo, undefined);
  assert.equal(record.tracingStart, undefined);
  await session.close();
  assert.equal(record.tracingStop, undefined);
});

// Credential login fake: records the order of fill/click and tracing calls.
function loginPlaywright(events) {
  let path = '/login';
  const field = (name) => ({ fill: async () => { events.push(`fill ${name}`); } });
  const page = {
    url: () => `http://127.0.0.1:3000${path}`,
    goto: async (url) => { path = new URL(url).pathname === '/login' ? '/login' : new URL(url).pathname; events.push(`goto ${path}`); return {}; },
    locator: (selector) => (selector === 'form'
      ? { getByRole: () => ({ click: async () => { events.push('submit'); path = '/chat'; } }) }
      : field(selector)),
    waitForURL: async () => {},
    waitForLoadState: async () => {},
    video: () => null,
  };
  const context = {
    tracing: {
      start: async () => { events.push('tracing.start'); },
      stop: async (options) => { events.push(`tracing.stop ${options.path.split('/').at(-1)}`); },
    },
    newPage: async () => page,
    close: async () => {},
  };
  return { chromium: { launch: async () => ({ newContext: async () => context, close: async () => {} }) } };
}

test('credential login is never traced: tracing starts after login and pauses for a re-login', async () => {
  await withEvidenceDir(async (dir) => {
    const events = [];
    const session = new QASession(
      { ...sessionConfig(dir), credentials: { email: 'qa@example.test', password: 'sentinel-password' } },
      { playwright: loginPlaywright(events) },
    );
    await session.login();
    const firstStart = events.indexOf('tracing.start');
    assert.ok(firstStart > events.indexOf('submit'), `tracing started before login completed: ${events.join(', ')}`);
    events.length = 0;
    await session.login();
    assert.deepEqual(events.slice(0, 1), ['tracing.stop trace.zip']);
    assert.ok(events.indexOf('tracing.start') > events.indexOf('submit'));
    await session.close();
    assert.deepEqual(session.artifacts.traces, ['trace.zip', 'trace-2.zip']);
  });
});

test('storage-state and anonymous sessions trace from browser open', async () => {
  await withEvidenceDir(async (dir) => {
    const events = [];
    const session = new QASession(sessionConfig(dir), { playwright: loginPlaywright(events) });
    await session.openBrowser();
    assert.deepEqual(events, ['tracing.start']);
    await session.close();
  });
});

test('a quarantined (timed-out) session still stops tracing', async () => {
  await withEvidenceDir(async (dir) => {
    const record = { screenshots: [] };
    const session = new QASession(sessionConfig(dir), { playwright: fakePlaywright(record) });
    await session.openBrowser();
    await session.quarantine();
    assert.equal(record.tracingStop.path, join(dir, 'trace.zip'));
    assert.deepEqual(session.artifacts.traces, ['trace.zip']);
  });
});

test('checkpoint takes a full-page screenshot named by scenario and checkpoint', async () => {
  await withEvidenceDir(async (dir) => {
    const record = { screenshots: [] };
    const session = new QASession(sessionConfig(dir), { playwright: fakePlaywright(record) });
    await session.openBrowser();
    const item = await session.checkpoint('smoke.login-availability', 'login.form');
    assert.deepEqual(item, { kind: 'checkpoint', name: 'login.form', file: 'smoke.login-availability--login.form.png' });
    assert.equal(record.screenshots[0].fullPage, true);
    assert.equal(record.screenshots[0].path, join(dir, 'checkpoints', 'smoke.login-availability--login.form.png'));
    assert.deepEqual(session.artifacts.checkpoints, [item]);
  });
});

test('checkpoint rejects invalid names and a missing page', async () => {
  const session = new QASession(sessionConfig('/nonexistent-ev'), {});
  await assert.rejects(session.checkpoint('x', 'Bad Name'), /Checkpoint name/);
  await assert.rejects(session.checkpoint('x', '../escape'), /Checkpoint name/);
  await assert.rejects(session.checkpoint('smoke.login-availability', 'login.form'), /no open page/);
});

function fakeRunnerSession(taken) {
  return {
    observations: [],
    artifacts: { videos: ['/tmp/ev/video/a.webm'], traces: ['trace.zip'], checkpoints: taken },
    checkpoint: async (scenarioId, name) => {
      const item = { kind: 'checkpoint', name, file: `${scenarioId}--${name}.png` };
      taken.push(item);
      return item;
    },
    cleanup: async () => ({ status: 'complete', retained: [], errors: [] }),
    close: async () => {},
  };
}

test('runner passes evidence.checkpoint to scenarios and lists checkpoints on the case', async () => {
  const taken = [];
  const registry = [{ id: 'smoke.x', title: 'x', suite: 'smoke', prerequisites: [], async run(_s, evidence) { await evidence.checkpoint('x.one'); return { assertion: 'ok' }; } }];
  const report = await runCampaign(
    { baseUrl: 'http://127.0.0.1:3000', apiUrl: 'http://127.0.0.1:3000/api/v1', evidenceDir: '/tmp/ev' },
    { sessionFactory: async () => fakeRunnerSession(taken), registry },
  );
  assert.equal(report.cases[0].status, 'PASS');
  assert.deepEqual(report.cases[0].checkpoints, [{ kind: 'checkpoint', name: 'x.one', file: 'smoke.x--x.one.png' }]);
  assert.equal(report.run.evidenceDir, '/tmp/ev');
  assert.deepEqual(report.run.artifacts, { videos: ['/tmp/ev/video/a.webm'], traces: ['trace.zip'], checkpointCount: 1 });
});

test('a failing scenario keeps the checkpoints it took', async () => {
  const taken = [];
  const registry = [{ id: 'smoke.y', title: 'y', suite: 'smoke', prerequisites: [], async run(_s, evidence) { await evidence.checkpoint('y.one'); throw new Error('boom'); } }];
  const report = await runCampaign(
    { baseUrl: 'http://127.0.0.1:3000', apiUrl: 'http://127.0.0.1:3000/api/v1' },
    { sessionFactory: async () => fakeRunnerSession(taken), registry },
  );
  assert.equal(report.cases[0].status, 'FAIL');
  assert.deepEqual(report.cases[0].checkpoints.map((item) => item.name), ['y.one']);
  assert.equal(report.run.evidenceDir, null);
});

test('a checkpoint on a session that cannot screenshot fails the scenario', async () => {
  const registry = [{ id: 'smoke.z', title: 'z', suite: 'smoke', prerequisites: [], async run(_s, evidence) { await evidence.checkpoint('z.one'); return {}; } }];
  const report = await runCampaign(
    { baseUrl: 'http://127.0.0.1:3000', apiUrl: 'http://127.0.0.1:3000/api/v1' },
    { sessionFactory: async () => ({ observations: [], cleanup: async () => ({ status: 'complete', retained: [], errors: [] }) }), registry },
  );
  assert.equal(report.cases[0].status, 'FAIL');
  assert.match(report.cases[0].reason, /cannot take screenshots/);
});

const REPORT = {
  run: {
    id: 'r1', startedAt: '2026-10-08T10:00:00.000Z', finishedAt: '2026-10-08T10:01:00.000Z', command: "'pnpm' 'qa:nous' '--features' 'login'", target: 'http://127.0.0.1:3000',
    localSource: { sha: 'a'.repeat(40), dirty: 'clean', provenance: 'git HEAD' }, configuration: { apiUrl: 'http://127.0.0.1:8000/api/v1', features: ['login'] },
    observedIdentity: { backendSha: null, frontendSha: null, provenance: null }, evidenceDir: '/tmp/ev', artifacts: { videos: ['/tmp/ev/video/x.webm'], traces: ['trace.zip'], checkpointCount: 1 },
  },
  cases: [
    { id: 'smoke.login-availability', title: 'Login page renders', status: 'PASS', reason: null, checkpoints: [{ kind: 'checkpoint', name: 'login.form', file: 'smoke.login-availability--login.form.png' }] },
    { id: 'workflow.x', title: 'x', status: 'BLOCKED', reason: 'Missing credentials | retry', checkpoints: [] },
  ],
  cleanup: { status: 'complete', retained: [], errors: [] },
  summary: { selected: 2, passed: 1, failed: 0, blocked: 1, skipped: 0, incomplete: true },
};

test('evidence README records SHA, target, per-scenario result and checkpoints, and maps BLOCKED to NOT RUN', () => {
  const text = renderEvidenceReadme(REPORT);
  assert.match(text, /^# Verification evidence: login \(2026-10-08\)/);
  assert.match(text, new RegExp('a'.repeat(40)));
  assert.match(text, /http:\/\/127\.0\.0\.1:3000/);
  assert.match(text, /\| smoke\.login-availability \| PASS \|/);
  assert.match(text, /\| workflow\.x \| BLOCKED \(NOT RUN\) \| Missing credentials \\\| retry \|/);
  assert.match(text, /\| smoke\.login-availability \| login\.form \| smoke\.login-availability--login\.form\.png \|/);
  assert.match(text, /Overall: BLOCKED/);
  assert.doesNotMatch(text, /\]\(/, 'binaries are listed by name, never linked');
});

test('overall is PASS only when every case passed and cleanup completed', () => {
  const passing = { ...REPORT, cases: [REPORT.cases[0]], summary: { ...REPORT.summary, selected: 1, blocked: 0, incomplete: false } };
  assert.match(renderEvidenceReadme(passing), /Overall: PASS \(assertions\); visual confirmation pending/);
  const failing = { ...passing, summary: { ...passing.summary, failed: 1 } };
  assert.match(renderEvidenceReadme(failing), /Overall: FAILED/);
  const dirtyCleanup = { ...passing, cleanup: { status: 'incomplete', retained: [{ kind: 'thread', id: 'x' }], errors: [] } };
  assert.match(renderEvidenceReadme(dirtyCleanup), /Overall: BLOCKED/);
});

test('writeEvidenceRecord creates the directory and README', async () => {
  const dir = await mkdtemp(join(tmpdir(), 'nous-verify-'));
  try {
    const path = await writeEvidenceRecord(REPORT, join(dir, 'verify-login-20261008'));
    assert.equal(path, join(dir, 'verify-login-20261008', 'README.md'));
    assert.match(await readFile(path, 'utf8'), /^# Verification evidence: login/);
  } finally {
    await rm(dir, { recursive: true, force: true });
  }
});

test('main writes the evidence README when --evidence-record is given', async () => {
  const dir = await mkdtemp(join(tmpdir(), 'nous-verify-main-'));
  const original = console.log;
  const lines = [];
  console.log = (line) => lines.push(String(line));
  try {
    const registry = [{ id: 'smoke.x', title: 'x', suite: 'smoke', prerequisites: [], async run(_s, evidence) { await evidence.checkpoint('x.one'); return { assertion: 'ok' }; } }];
    const record = join(dir, 'verify-x-20261008');
    const code = await main(
      ['--scenario', 'smoke.x', '--output-dir', join(dir, 'report'), '--evidence-dir', join(dir, 'ev'), '--evidence-record', record],
      {},
      { sessionFactory: async () => fakeRunnerSession([]), registry },
    );
    assert.equal(code, 0);
    const text = await readFile(join(record, 'README.md'), 'utf8');
    assert.match(text, /\| smoke\.x \| PASS \|/);
    assert.match(text, /\| smoke\.x \| x\.one \| smoke\.x--x\.one\.png \|/);
    assert.ok(lines.some((line) => line.startsWith('Evidence: ')));
  } finally {
    console.log = original;
    await rm(dir, { recursive: true, force: true });
  }
});

test('v1 verification scenarios are registered with honest prerequisites', () => {
  const byId = Object.fromEntries(registry.map((scenario) => [scenario.id, scenario]));
  assert.deepEqual(byId['workflow.login-authenticated'].prerequisites, ['auth', 'browser']);
  for (const id of ['workflow.chat-send-stream-reload', 'workflow.project-creation-via-chat', 'workflow.hitl-deny']) {
    assert.deepEqual(byId[id].prerequisites, ['auth', 'writes', 'model', 'browser'], id);
    assert.equal(byId[id].callsModel, true, id);
    assert.equal(byId[id].createsFixtures, true, id);
    assert.equal(byId[id].suite, 'workflow', id);
  }
  assert.ok(byId['workflow.document-upload-and-attachment'].prerequisites.includes('browser'));
});

test('the real feature map covers every v1 feature with registered scenarios', async () => {
  const map = await loadFeatureMap(join(import.meta.dirname, '../../../docs/engineering/feature-map.yaml'));
  const ids = new Set(registry.map((scenario) => scenario.id));
  for (const feature of map.features) {
    assert.equal(feature.status, 'covered', feature.id);
    assert.ok(feature.scenarios.length > 0, feature.id);
    for (const id of feature.scenarios) assert.ok(ids.has(id), `${feature.id}: ${id}`);
  }
  assert.deepEqual(scenariosForFeatures(map, ['hitl-approve-deny']), ['workflow.project-creation-via-chat', 'workflow.hitl-deny']);
});

function visibleLocator() {
  return { first: () => ({ waitFor: async () => {} }), count: async () => 1 };
}

test('login smoke takes the login.form checkpoint and still works without evidence', async () => {
  const taken = [];
  const session = {
    config: { timeoutMs: 100 },
    goto: async () => {},
    page: { locator: () => visibleLocator() },
  };
  await smokeLogin(session, { checkpoint: async (name) => taken.push(name) });
  assert.deepEqual(taken, ['login.form']);
  await smokeLogin(session);
});

test('authenticated login scenario takes the login.landed checkpoint', async () => {
  const taken = [];
  const scenario = registry.find((item) => item.id === 'workflow.login-authenticated');
  const session = { login: async () => ({ url: () => 'http://127.0.0.1:3000/chat' }) };
  const result = await scenario.run(session, { checkpoint: async (name) => taken.push(name) });
  assert.deepEqual(taken, ['login.landed']);
  assert.deepEqual(result.evidence, [{ path: '/chat' }]);
});

test('authenticated login scenario fails if the browser is still on /login', async () => {
  const scenario = registry.find((item) => item.id === 'workflow.login-authenticated');
  const session = { login: async () => ({ url: () => 'http://127.0.0.1:3000/login' }) };
  await assert.rejects(scenario.run(session, { checkpoint: async () => {} }), /still on \/login/);
});

test('project fixtures have a cleanup route', async () => {
  const calls = [];
  const session = new QASession({ baseUrl: 'http://127.0.0.1:3000', apiUrl: 'http://127.0.0.1:3000/api/v1', runId: 'r1', timeoutMs: 100 }, {});
  session.request = async (path, options) => { calls.push([options.method, path]); return { status: 204, data: null }; };
  session.registerFixture('project', '11111111-1111-4111-8111-111111111111', {});
  const result = await session.cleanup();
  assert.equal(result.status, 'complete');
  assert.deepEqual(calls, [['DELETE', '/api/v1/projects/11111111-1111-4111-8111-111111111111']]);
});

test('--list with --features lists only the selected scenarios (dry run)', async () => {
  const original = console.log;
  const lines = [];
  console.log = (line) => lines.push(String(line));
  try {
    const code = await main(['--list', '--features', 'login'], {});
    assert.equal(code, 0);
    assert.deepEqual(JSON.parse(lines.join('\n')).map((item) => item.id), ['smoke.login-availability', 'workflow.login-authenticated']);
  } finally {
    console.log = original;
  }
});

test('--changed-from fails closed (exit 2) when git cannot diff the ref', async () => {
  const original = console.error;
  const lines = [];
  console.error = (line) => lines.push(String(line));
  try {
    const code = await main(['--list', '--changed-from', 'no-such-ref-for-nous-verify'], {});
    assert.equal(code, 2);
    assert.match(lines.join('\n'), /git diff failed for --changed-from no-such-ref-for-nous-verify/);
  } finally {
    console.error = original;
  }
});

test('--features with an explicit non-all --suite is a configuration error', async () => {
  await withMap(async (mapPath) => {
    await assert.rejects(
      parseArgs(['--suite', 'smoke', '--features', 'login'], { NOUS_QA_FEATURE_MAP: mapPath }),
      /--features\/--changed-from select across suites/,
    );
    const config = await parseArgs(['--suite', 'all', '--features', 'login'], { NOUS_QA_FEATURE_MAP: mapPath });
    assert.equal(config.suite, 'all');
  });
});

// Minimal fake browser + API for the model journeys. Transcript rows are
// { role, text }; locator('[data-role=...]').filter({ hasText }) only matches
// rows of the listed roles, so role filtering is actually exercised.
function fakeJourney({ onSend = () => {}, projects = () => [], messages = () => [] } = {}) {
  const state = { transcript: [], dialogVisible: false, registered: [], decision: null };
  const roleLocator = (selector) => {
    const roles = [...selector.matchAll(/data-role="([a-z]+)"/g)].map((match) => match[1]);
    return {
      filter: ({ hasText }) => {
        const matches = () => state.transcript.filter((row) => roles.includes(row.role) && row.text.includes(hasText));
        return {
          first: () => ({ waitFor: async () => { if (matches().length === 0) throw new Error(`no ${roles.join('/')} row with ${hasText}`); } }),
          count: async () => matches().length,
        };
      },
    };
  };
  let draft = '';
  const page = {
    url: () => 'http://127.0.0.1:3000/chat',
    evaluate: async () => ({}),
    reload: async () => {},
    locator: roleLocator,
    getByRole: (role, options = {}) => {
      if (role === 'textbox') return { fill: async (value) => { draft = value; } };
      if (role === 'button' && options.name === 'Send message') {
        return { click: async () => { state.transcript.push({ role: 'user', text: draft }); onSend(state, draft); } };
      }
      if (role === 'alertdialog') {
        return {
          waitFor: async ({ state: wanted }) => {
            if ((wanted === 'visible') !== state.dialogVisible) throw new Error(`dialog not ${wanted}`);
          },
          getByRole: (_role, { name }) => ({
            click: async () => {
              state.decision = name;
              state.dialogVisible = false;
              if (name === 'Deny') state.transcript.push({ role: 'assistant', text: "Action cancelled by user. Let me know if you'd like to proceed differently." });
            },
          }),
        };
      }
      throw new Error(`unexpected getByRole ${role}`);
    },
  };
  let nextId = 1;
  const id = () => `00000000-0000-4000-8000-${String(nextId++).padStart(12, '0')}`;
  const session = {
    config: { timeoutMs: 50 },
    page,
    login: async () => page,
    goto: async () => {},
    registerFixture: (kind, fixtureId) => state.registered.push([kind, fixtureId]),
    request: async (path, options = {}) => {
      if (options.method === 'POST') return { status: 201, data: { id: id() } };
      if (path.startsWith('/api/v1/projects')) return { status: 200, data: { projects: projects(state) } };
      if (path.includes('/messages')) return { status: 200, data: { messages: messages(state) } };
      throw new Error(`unexpected request ${path}`);
    },
  };
  const evidence = { fixturePrefix: 'NOUS QA t1', consumeModelTurn: () => 1, checkpoint: async () => {} };
  return { state, session, evidence };
}

test('chat send only accepts the token from an assistant row, not the echoed prompt', async () => {
  const scenario = registry.find((item) => item.id === 'workflow.chat-send-stream-reload');
  // The user prompt contains KestrelAck42; no assistant ever answers.
  const silent = fakeJourney({ messages: (state) => state.transcript.map((row) => ({ role: row.role, content: row.text })) });
  await assert.rejects(scenario.run(silent.session, silent.evidence), /no assistant row with KestrelAck42/);

  const answered = fakeJourney({
    onSend: (state) => state.transcript.push({ role: 'assistant', text: 'KestrelAck42' }),
    messages: (state) => state.transcript.map((row) => ({ role: row.role, content: row.text })),
  });
  const result = await scenario.run(answered.session, answered.evidence);
  assert.match(result.assertion, /streamed an answer/);
});

test('HITL deny fails when the project exists anyway and registers it for cleanup', async () => {
  const scenario = registry.find((item) => item.id === 'workflow.hitl-deny');
  const leaked = fakeJourney({
    onSend: (state) => { state.dialogVisible = true; },
    projects: () => [{ id: '11111111-1111-4111-8111-111111111111', name: 'NOUS QA t1 project deny' }],
  });
  await assert.rejects(scenario.run(leaked.session, leaked.evidence), /Denied project creation still created a project/);
  assert.equal(leaked.state.decision, 'Deny');
  assert.deepEqual(leaked.state.registered.at(-1), ['project', '11111111-1111-4111-8111-111111111111']);
});

test('HITL deny passes only when no project appears and the transcript records the cancellation', async () => {
  const scenario = registry.find((item) => item.id === 'workflow.hitl-deny');
  const clean = fakeJourney({ onSend: (state) => { state.dialogVisible = true; } });
  const result = await scenario.run(clean.session, clean.evidence);
  assert.match(result.assertion, /left no project behind/);

  const silent = fakeJourney({ onSend: (state) => { state.dialogVisible = true; } });
  silent.session.page.getByRole = ((original) => (role, options) => {
    const locator = original(role, options);
    if (role !== 'alertdialog') return locator;
    return { ...locator, getByRole: () => ({ click: async () => { silent.state.dialogVisible = false; } }) };
  })(silent.session.page.getByRole);
  await assert.rejects(scenario.run(silent.session, silent.evidence), /no assistant row with Action cancelled by user/);
});

test('a project that appears after the approval wait fails is still registered for cleanup', async () => {
  const scenario = registry.find((item) => item.id === 'workflow.project-creation-via-chat');
  // The dialog never shows (wait times out) but the agent created the project anyway.
  const late = fakeJourney({ projects: () => [{ id: '22222222-2222-4222-8222-222222222222', name: 'NOUS QA t1 project approve' }] });
  await assert.rejects(scenario.run(late.session, late.evidence), /dialog not visible/);
  assert.deepEqual(late.state.registered.at(-1), ['project', '22222222-2222-4222-8222-222222222222']);
  assert.equal(late.state.registered.filter(([kind]) => kind === 'project').length, 1);
});
