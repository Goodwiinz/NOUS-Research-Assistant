import test from 'node:test';
import assert from 'node:assert/strict';
import { mkdtemp, rm, writeFile } from 'node:fs/promises';
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
