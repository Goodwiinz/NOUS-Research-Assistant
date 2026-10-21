import test from 'node:test';
import assert from 'node:assert/strict';
import { createHash } from 'node:crypto';
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
import { BrowserUnavailableError, QASession } from '../../../tests/e2e/qa/session.mjs';
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

test('the real feature map covers every covered feature with registered scenarios', async () => {
  const map = await loadFeatureMap(join(import.meta.dirname, '../../../docs/engineering/feature-map.yaml'));
  const ids = new Set(registry.map((scenario) => scenario.id));
  // Preserve the original coverage floor even when a newly mapped feature is
  // honestly marked planned pending its user-level browser QA.
  const v1Features = [
    'login',
    'chat-send-stream-reload',
    'hitl-approve-deny',
    'document-upload',
    'project-creation-via-chat',
  ];
  for (const id of v1Features) {
    const feature = map.features.find((entry) => entry.id === id);
    assert.ok(feature, `missing v1 feature: ${id}`);
    assert.equal(feature.status, 'covered', feature.id);
  }
  for (const feature of map.features) {
    // Statuses are `planned` or `covered` (docs/plans/2026-10-08-nous-verify.md); only
    // `covered` features must map scenarios, but every mapped id must be registered.
    assert.ok(['planned', 'covered'].includes(feature.status), feature.id);
    if (feature.status === 'covered') assert.ok(feature.scenarios.length > 0, feature.id);
    for (const id of feature.scenarios) assert.ok(ids.has(id), `${feature.id}: ${id}`);
  }
  assert.deepEqual(scenariosForFeatures(map, ['hitl-approve-deny']), ['workflow.project-creation-via-chat', 'workflow.hitl-deny']);
});

function visibleLocator() {
  return { first: () => ({ waitFor: async () => {} }), count: async () => 1 };
}

function artifactSharingDouble(options = {}) {
  const id = (number) => `00000000-0000-4000-8000-${String(number).padStart(12, '0')}`;
  const ids = { user: id(1), org: id(2), workspace: id(3), project: id(4), document: id(5), draft: id(6), artifact: id(7), version: id(8), share: id(9) };
  const acceptedId = (resource) => options.identityResource === resource ? options.identityValue : ids[resource];
  const bytes = Buffer.from('## Synthetic private section\n\nThe control number is 7314.\n');
  const version = { artifact_id: ids.artifact, version_id: ids.version, title: 'Synthetic draft', mime_type: 'text/markdown', byte_size: bytes.length, sha256: createHash('sha256').update(bytes).digest('hex') };
  const state = { calls: [], checkpoints: [], modelReservations: 0, revoked: false, cancelled: false, probeFailed: false, pagePath: '', unavailable: false, logins: 0 };
  const locator = (shown) => ({
    first: () => ({ waitFor: async () => { if (!shown()) throw new Error('Expected owned fixture was not visible'); } }),
    count: async () => shown() ? 1 : 0,
    getByText: () => locator(shown),
  });
  const page = {
    url: () => `http://127.0.0.1:3000${state.pagePath}`,
    getByRole: (role, attributes = {}) => {
      if (role === 'button' && attributes.name === 'Refresh shared version') return { click: async () => { state.unavailable = state.revoked; } };
      if (role === 'button' && attributes.name instanceof RegExp && attributes.name.test('Download Synthetic draft')) return locator(() => options.staleDownload === true);
      if (role === 'alert') return locator(() => state.unavailable);
      if (role === 'heading') return locator(() => !state.unavailable || options.stalePreview === true);
      if (role === 'button') return locator(() => !state.unavailable || options.stalePreview === true);
      return locator(() => true);
    },
    locator: () => locator(() => !state.unavailable || options.stalePreview === true),
    innerText: async () => state.unavailable && !options.stalePreview && !options.privateBodyLeak ? 'Shared file is unavailable.' : 'Synthetic draft Synthetic private section',
  };
  const session = new QASession(sessionConfig(undefined), {});
  session.page = page;
  session.login = async () => { state.logins += 1; return page; };
  session.goto = async (path) => { state.pagePath = path; return {}; };
  session.request = async (path, request = {}) => {
    state.calls.push([request.method ?? 'GET', path, request]);
    if (path === '/api/v1/artifacts/capabilities') return { status: 200, data: { enabled: options.enabled !== false, sharing_enabled: options.sharingEnabled !== false } };
    if (path === '/api/v1/auth/me') return { status: 200, data: { user: { id: ids.user, organization_id: ids.org }, organization: { id: ids.org } } };
    if (path === '/api/v2/workspaces') return { status: 201, data: { id: acceptedId('workspace') } };
    if (path === '/api/v1/projects') return { status: 201, data: { id: acceptedId('project') } };
    if (path === '/api/v1/files/upload') return { status: 200, data: { document_id: acceptedId('document') } };
    if (path.startsWith(`/api/v1/documents/${ids.document}`) && request.method !== 'DELETE') return { status: 200, data: { content_text: 'NOUS QA share-test Kestrel fixture. The control number is 7314.' } };
    if (path === `/api/v2/workspaces/${ids.workspace}/collections/${ids.project}/documents`) {
      assert.deepEqual(request.json.document_ids, [ids.document]);
      return { status: 200, data: { id: ids.project } };
    }
    if (path.includes('/documents?')) throw new Error('Sharing fixtures must not enqueue automatic extraction or KG jobs');
    if (path.includes('/drafts?')) {
      assert.equal(state.modelReservations, 1, 'Generation must reserve the campaign budget before submission');
      assert.equal(new URL(path, 'http://localhost').searchParams.get('max_sections'), '2');
      if (options.submissionReject === 'task') throw Object.assign(new Error('Generation rejected'), { status: 403 });
      return { status: 202, data: { task_id: options.identityResource === 'task' ? options.identityValue : 'draft_qa1' } };
    }
    if (path.includes('/drafts/cancel/')) {
      if (options.cancelFails) throw Object.assign(new Error('Cancel unavailable'), { status: 503 });
      state.cancelled = true;
      return { status: 200, data: { cancelled: true } };
    }
    if (path.includes('/drafts/status/')) {
      if (options.taskProbeFails && !state.probeFailed) { state.probeFailed = true; throw new Error('Task status unavailable'); }
      return { status: 200, data: { status: state.cancelled ? 'cancelled' : 'completed', draft_id: ids.draft } };
    }
    if (path === `/api/v1/artifacts/projects/${ids.project}`) return { status: 200, data: [{ current_version: { ...version, provenance: { native_item_id: ids.draft } } }] };
    if (path === `/api/v1/artifacts/versions/${ids.version}/content`) return { status: 200, bytes };
    if (path === '/api/v1/artifacts/shares') {
      assert.deepEqual(request.json.audience_user_ids, [ids.user], 'Audience identity must come from the server');
      assert.equal(request.json.version_id, ids.version);
      if (options.submissionReject === 'share') throw Object.assign(new Error('Share rejected'), { status: 403 });
      return { status: 200, data: { id: acceptedId('share'), version_id: ids.version, expires_at: request.json.expires_at, revoked_at: null } };
    }
    if (path.startsWith(`/api/v1/artifacts/shares/${ids.share}`)) {
      if (request.method === 'DELETE') {
        if (options.revokeFails) throw Object.assign(new Error('Revoke unavailable'), { status: 503 });
        state.revoked = true;
        return { status: 204, data: null };
      }
      if (request.forwardAuth === false) {
        if (options.anonymousLeak) return { status: 200, data: version, bytes };
        throw Object.assign(new Error('Unauthenticated'), { status: 401 });
      }
      if (state.revoked) {
        if (options.revokedLeak) return { status: 200, data: version, bytes };
        throw Object.assign(new Error('Revoked'), { status: 404 });
      }
      if (path.endsWith('/content')) return { status: 200, headers: new Headers({ 'cache-control': 'private, no-store' }), bytes: options.corruptBytes ? Buffer.from('different') : bytes };
      return { status: 200, headers: new Headers({ 'cache-control': 'private, no-store' }), data: { ...version, ...(options.foreignVersion ? { version_id: id(99) } : {}), ...(options.provenanceLeak ? { provenance: { source_ids: [ids.document] } } : {}) } };
    }
    if (request.method === 'DELETE') {
      if ((options.cleanupReject === 'document' && path === `/api/v1/documents/${ids.document}`) || (options.cleanupReject === 'project' && path === `/api/v1/projects/${ids.project}`)) throw Object.assign(new Error('Owned child cleanup rejected'), { status: 409 });
      return { status: 204, data: null };
    }
    throw new Error(`Unexpected fixture request ${path}`);
  };
  const evidence = { fixturePrefix: 'NOUS QA share-test', reserveModelTurn: () => { state.modelReservations += 1; }, checkpoint: async (name) => state.checkpoints.push(name) };
  return { session, evidence, state, ids, bytes };
}

test('artifact sharing registers its browser, write and bounded generation prerequisites', () => {
  const scenario = registry.find((item) => item.id === 'workflow.artifact-version-sharing');
  assert.ok(scenario, 'The real artifact sharing scenario must be registered');
  assert.deepEqual(scenario.prerequisites, ['auth', 'writes', 'model', 'browser']);
  assert.equal(scenario.callsModel, true);
  assert.equal(scenario.createsFixtures, true);
});

test('artifact sharing exercises exact bytes, audience identity, revoked refetch and owned cleanup', async () => {
  const fixture = artifactSharingDouble();
  const scenario = registry.find((item) => item.id === 'workflow.artifact-version-sharing');
  const result = await scenario.run(fixture.session, fixture.evidence);
  assert.match(result.assertion, /exact saved version/);
  assert.deepEqual(fixture.state.checkpoints, ['artifact.fixture', 'artifact.share-created', 'artifact.shared-ready', 'artifact.revoked', 'artifact.no-private-content']);
  assert.equal(fixture.state.revoked, true);
  assert.equal(fixture.state.modelReservations, 1);
  assert.deepEqual(result.evidence[0].anonymousStatuses, [401, 401], 'Evidence must contain only statuses observed by the flow');
  assert.equal(fixture.session.pendingMutations.size, 0);
  assert.ok(!JSON.stringify(result.evidence).includes(fixture.bytes.toString()), 'Reports must not contain private bytes');
  assert.deepEqual(fixture.session.ledger.list().map((item) => item.kind), ['workspace', 'project', 'document']);
  const cleanup = await fixture.session.cleanup();
  assert.equal(cleanup.status, 'complete');
  assert.deepEqual(fixture.state.calls.filter(([method]) => method === 'DELETE').map(([, path]) => path), [
    `/api/v1/artifacts/shares/${fixture.ids.share}`, `/api/v1/documents/${fixture.ids.document}`, `/api/v1/projects/${fixture.ids.project}`, `/api/v2/workspaces/${fixture.ids.workspace}`,
  ]);
});

for (const resource of ['workspace', 'project', 'document', 'task', 'share']) {
  for (const [label, identityValue] of [['missing', undefined], ['malformed', '../foreign-id']]) {
    test(`artifact sharing retains fixtures for an accepted ${resource} with a ${label} identity`, async () => {
      const fixture = artifactSharingDouble({ identityResource: resource, identityValue });
      const scenario = registry.find((item) => item.id === 'workflow.artifact-version-sharing');
      await assert.rejects(scenario.run(fixture.session, fixture.evidence), /identity|UUID/);
      assert.ok(fixture.session.pendingMutations.size > 0, 'Unknown accepted resources must remain a cleanup risk');
      const beforeCleanup = fixture.state.calls.length;
      const cleanup = await fixture.session.cleanup();
      assert.equal(cleanup.status, 'incomplete');
      assert.equal(fixture.state.calls.length, beforeCleanup, 'Unknown child identities must retain their exact owned parents');
      assert.ok(cleanup.uncertainMutations.length > 0, 'The report must expose unknown accepted mutation state');
    });
  }
}

for (const resource of ['document', 'project']) {
  test(`artifact sharing preserves known parents after an observed ${resource} cleanup rejection`, async () => {
    const fixture = artifactSharingDouble({ cleanupReject: resource });
    const scenario = registry.find((item) => item.id === 'workflow.artifact-version-sharing');
    await scenario.run(fixture.session, fixture.evidence);
    const beforeCleanup = fixture.state.calls.length;
    const cleanup = await fixture.session.cleanup();
    assert.equal(cleanup.status, 'incomplete');
    assert.deepEqual(cleanup.retained.map((item) => item.kind), resource === 'document' ? ['document', 'project', 'workspace'] : ['project', 'workspace']);
    assert.deepEqual(fixture.state.calls.slice(beforeCleanup).map(([, path]) => path), resource === 'document' ? [`/api/v1/documents/${fixture.ids.document}`] : [`/api/v1/documents/${fixture.ids.document}`, `/api/v1/projects/${fixture.ids.project}`]);
    assert.equal(fixture.session.pendingMutations.size, 0, 'Observed child rejection must preserve known parents without inventing uncertainty');
  });
}

for (const resource of ['task', 'share']) {
  test(`artifact sharing clears protection after an observed ${resource} rejection`, async () => {
    const fixture = artifactSharingDouble({ submissionReject: resource });
    const scenario = registry.find((item) => item.id === 'workflow.artifact-version-sharing');
    await assert.rejects(scenario.run(fixture.session, fixture.evidence), /rejected/);
    assert.equal(fixture.session.pendingMutations.size, 0);
    assert.equal((await fixture.session.cleanup()).status, 'complete');
  });
}

for (const [option, expected] of [
  ['foreignVersion', /pinned version/], ['corruptBytes', /exact saved bytes/], ['provenanceLeak', /private provenance/],
  ['anonymousLeak', /Anonymous share access/], ['revokedLeak', /Revoked share access/], ['stalePreview', /private content/],
  ['staleDownload', /private content/], ['privateBodyLeak', /private content in the document/],
]) {
  test(`artifact sharing fails its ${option} assertion and still revokes its exact fixture`, async () => {
    const fixture = artifactSharingDouble({ [option]: true });
    const scenario = registry.find((item) => item.id === 'workflow.artifact-version-sharing');
    await assert.rejects(scenario.run(fixture.session, fixture.evidence), expected);
    assert.equal(fixture.state.revoked, true);
    assert.equal(fixture.session.pendingMutations.size, 0);
    assert.equal((await fixture.session.cleanup()).status, 'complete');
  });
}

for (const option of ['enabled', 'sharingEnabled']) {
  test(`artifact sharing is BLOCKED before fixtures or generation when ${option} is false`, async () => {
    const fixture = artifactSharingDouble({ [option]: false });
    const scenario = registry.find((item) => item.id === 'workflow.artifact-version-sharing');
    const result = await scenario.run(fixture.session, fixture.evidence);
    assert.equal(result.status, 'BLOCKED');
    assert.match(result.reason, /capability/);
    assert.equal(fixture.state.modelReservations, 0);
    assert.equal(fixture.session.ledger.list().length, 0);
    assert.ok(fixture.state.calls.every(([method]) => method === 'GET'));
  });
}

test('artifact sharing cannot enter its live flow without auth and explicit writes', async () => {
  const scenario = registry.find((item) => item.id === 'workflow.artifact-version-sharing');
  for (const extra of [{ allowWrites: true }, { credentials: { email: 'qa@example.test', password: 'test-password' } }]) {
    const report = await runCampaign(
      { baseUrl: 'http://127.0.0.1:3000', apiUrl: 'http://127.0.0.1:3000/api/v1', suite: 'workflow', ...extra },
      { sessionFactory: async () => fakeRunnerSession([]), registry: [scenario] },
    );
    assert.equal(report.cases[0].status, 'BLOCKED');
    assert.equal(report.summary.passed, 0);
  }
});

test('artifact sharing cancels and verifies an accepted generation after a status failure', async () => {
  const fixture = artifactSharingDouble({ taskProbeFails: true });
  const scenario = registry.find((item) => item.id === 'workflow.artifact-version-sharing');
  await assert.rejects(scenario.run(fixture.session, fixture.evidence), /Task status unavailable/);
  assert.equal(fixture.state.cancelled, true);
  assert.equal(fixture.session.pendingMutations.size, 0);
  assert.equal((await fixture.session.cleanup()).status, 'complete');
});

for (const options of [{ taskProbeFails: true, cancelFails: true }, { revokeFails: true }]) {
  test(`artifact sharing retains owned fixtures when ${options.cancelFails ? 'cancel' : 'revoke'} cleanup is unproven`, async () => {
    const fixture = artifactSharingDouble(options);
    const scenario = registry.find((item) => item.id === 'workflow.artifact-version-sharing');
    await assert.rejects(scenario.run(fixture.session, fixture.evidence));
    assert.ok(fixture.session.pendingMutations.size > 0);
    const beforeCleanup = fixture.state.calls.length;
    const cleanup = await fixture.session.cleanup();
    assert.equal(cleanup.status, 'incomplete');
    assert.equal(fixture.state.calls.length, beforeCleanup, 'No parent may be deleted while child cleanup is unproven');
    assert.ok(cleanup.retained.some((item) => item.id === fixture.ids.workspace));
  });
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
      // The live /chat composer's Send button has no aria-label; its
      // accessible name is its visible text, exactly "Send".
      if (role === 'button' && options.name === 'Send' && options.exact === true) {
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

test('the recorded command replaces path-valued flag values with [path]', async () => {
  const dir = await mkdtemp(join(tmpdir(), 'nous-verify-cmd-'));
  try {
    const state = join(dir, 'state.json');
    await writeFile(state, '{}', 'utf8');
    const config = await parseArgs([
      '--evidence-dir', '/Users/someone/private/ev',
      '--evidence-record', '/Users/someone/repo/docs/testing/evidence/verify-x',
      '--output-dir', '/Users/someone/reports',
      '--storage-state', state,
      '--timeout-ms', '1000',
    ], {});
    assert.doesNotMatch(config.command, /someone|state\.json/);
    assert.equal((config.command.match(/\[path\]/g) ?? []).length, 4);
    assert.match(config.command, /'--timeout-ms' '1000'/);
  } finally {
    await rm(dir, { recursive: true, force: true });
  }
});

test('evidence README shows the evidence dir relative to the repo root, not the cwd', async () => {
  const repoRoot = join(import.meta.dirname, '../../..');
  const original = process.cwd();
  process.chdir(tmpdir());
  try {
    const text = renderEvidenceReadme({ ...REPORT, run: { ...REPORT.run, evidenceDir: join(repoRoot, '.verify-artifacts', 'r1') } });
    assert.match(text, /Binaries \(not committed\): `\.verify-artifacts\/r1`/);
    const outside = renderEvidenceReadme({ ...REPORT, run: { ...REPORT.run, evidenceDir: '/Users/someone/elsewhere/r1' } });
    assert.doesNotMatch(outside, /someone/);
  } finally {
    process.chdir(original);
  }
});

test('a Chromium launch failure surfaces as BrowserUnavailableError', async () => {
  const playwright = { chromium: { launch: async () => { throw new Error("browserType.launch: Executable doesn't exist at /x/chrome-headless-shell"); } } };
  const session = new QASession(sessionConfig(undefined), { playwright });
  await assert.rejects(session.openBrowser(), (error) => {
    assert.ok(error instanceof BrowserUnavailableError);
    assert.match(error.message, /playwright install chromium-headless-shell/);
    return true;
  });
});

test('a scenario whose browser cannot launch is BLOCKED, not FAIL', async () => {
  const registry = [{ id: 'smoke.b', title: 'b', suite: 'smoke', prerequisites: [], async run() { throw new BrowserUnavailableError('Chromium could not launch'); } }];
  const report = await runCampaign(
    { baseUrl: 'http://127.0.0.1:3000', apiUrl: 'http://127.0.0.1:3000/api/v1' },
    { sessionFactory: async () => fakeRunnerSession([]), registry },
  );
  assert.equal(report.cases[0].status, 'BLOCKED');
  assert.match(report.cases[0].reason, /Chromium could not launch/);
});
