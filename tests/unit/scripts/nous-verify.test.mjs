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
    assert.equal(session.artifacts.trace, 'trace.zip');
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

test('a quarantined (timed-out) session still stops tracing', async () => {
  await withEvidenceDir(async (dir) => {
    const record = { screenshots: [] };
    const session = new QASession(sessionConfig(dir), { playwright: fakePlaywright(record) });
    await session.openBrowser();
    await session.quarantine();
    assert.equal(record.tracingStop.path, join(dir, 'trace.zip'));
    assert.equal(session.artifacts.trace, 'trace.zip');
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
    artifacts: { videos: ['/tmp/ev/video/a.webm'], trace: 'trace.zip', checkpoints: taken },
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
  assert.deepEqual(report.run.artifacts, { videos: ['/tmp/ev/video/a.webm'], trace: 'trace.zip', checkpointCount: 1 });
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
    observedIdentity: { backendSha: null, frontendSha: null, provenance: null }, evidenceDir: '/tmp/ev', artifacts: { videos: ['/tmp/ev/video/x.webm'], trace: 'trace.zip', checkpointCount: 1 },
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
