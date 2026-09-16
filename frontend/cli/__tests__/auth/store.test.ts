import { afterEach, expect, test, vi } from 'vitest';
import * as os from 'os';
import * as fs from 'fs';
vi.mock('fs', async (importOriginal) => {
  const actual = await importOriginal<typeof import('fs')>();
  return { ...actual, renameSync: vi.fn(actual.renameSync) };
});
import * as path from 'path';

process.env.NOUS_CONFIG_DIR = path.join(
  os.tmpdir(),
  `.nous-test-${Date.now()}`
);

import {
  loadConfig,
  saveConfig,
  clearConfig,
  NousConfig,
} from '../../auth/store';

afterEach(() => {
  vi.restoreAllMocks();
  clearConfig();
});

test('returns null when no config exists', () => {
  expect(loadConfig()).toBeNull();
});

test('saves and loads config', () => {
  const config: NousConfig = {
    token: 'tok_abc',
    user_email: 'test@example.com',
    organization_id: 'org_1',
    expires_at: '2099-01-01T00:00:00Z',
    thread_id: null,
  };
  saveConfig(config);
  expect(loadConfig()).toEqual(config);
});

test('clearConfig removes the file', () => {
  saveConfig({
    token: 'x',
    user_email: 'a@b.com',
    organization_id: 'o',
    expires_at: '2099-01-01T00:00:00Z',
    thread_id: null,
  });
  clearConfig();
  expect(loadConfig()).toBeNull();
});

test('restricts new credentials and repairs legacy permissions', () => {
  const config: NousConfig = {
    token: 'dummy',
    user_email: 'test@example.invalid',
    organization_id: 'org',
    expires_at: '2099-01-01',
    thread_id: null,
  };
  const dir = process.env.NOUS_CONFIG_DIR!;
  const file = path.join(dir, 'config.json');
  saveConfig(config);
  expect(fs.statSync(dir).mode & 0o777).toBe(0o700);
  expect(fs.statSync(file).mode & 0o777).toBe(0o600);
  fs.chmodSync(dir, 0o755);
  fs.chmodSync(file, 0o644);
  expect(loadConfig()).toEqual(config);
  expect(fs.statSync(dir).mode & 0o777).toBe(0o700);
  expect(fs.statSync(file).mode & 0o777).toBe(0o600);
  fs.chmodSync(file, 0o644);
  saveConfig({ ...config, thread_id: 'next' });
  expect(fs.statSync(file).mode & 0o777).toBe(0o600);
});

test('a failed atomic replacement preserves existing credentials and removes the temporary file', () => {
  const config: NousConfig = {
    token: 'dummy',
    user_email: 'test@example.invalid',
    organization_id: 'org',
    expires_at: '2099-01-01',
    thread_id: null,
  };
  saveConfig(config);
  const dir = process.env.NOUS_CONFIG_DIR!;
  const before = fs.readFileSync(path.join(dir, 'config.json'), 'utf8');
  vi.mocked(fs.renameSync).mockImplementationOnce(() => {
    throw new Error('disk failure');
  });
  expect(() => saveConfig({ ...config, token: 'replacement' })).toThrow(
    'disk failure'
  );
  expect(fs.readFileSync(path.join(dir, 'config.json'), 'utf8')).toBe(before);
  expect(fs.readdirSync(dir).filter((name) => name.endsWith('.tmp'))).toEqual(
    []
  );
});
