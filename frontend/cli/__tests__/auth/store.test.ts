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
  subscribeConfig,
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

test('subscribers see committed changes and stop receiving them after unsubscribe', () => {
  const config: NousConfig = {
    token: 'dummy',
    user_email: 'test@example.invalid',
    organization_id: 'org',
    expires_at: '2099-01-01',
    thread_id: null,
  };
  const listener = vi.fn((snapshot: NousConfig | null) => {
    expect(loadConfig()).toEqual(snapshot);
  });
  const unsubscribe = subscribeConfig(listener);
  try {
    saveConfig(config);
    expect(listener).toHaveBeenLastCalledWith(config);
    expect(listener.mock.calls[0][0]).not.toBe(config);
    clearConfig();
    expect(listener).toHaveBeenLastCalledWith(null);
    unsubscribe();
    saveConfig(config);
    expect(listener).toHaveBeenCalledTimes(2);
  } finally {
    unsubscribe();
  }
});

test('listener failures do not fail committed writes or skip other listeners', () => {
  const config: NousConfig = {
    token: 'dummy',
    user_email: 'test@example.invalid',
    organization_id: 'org',
    expires_at: '2099-01-01',
    thread_id: null,
  };
  const warning = vi.spyOn(console, 'warn').mockImplementation(() => {});
  const unsubscribeBroken = subscribeConfig(() => {
    throw new Error('listener failure');
  });
  const listener = vi.fn();
  const unsubscribe = subscribeConfig(listener);
  try {
    expect(() => saveConfig(config)).not.toThrow();
    expect(loadConfig()).toEqual(config);
    expect(listener).toHaveBeenLastCalledWith(config);
    expect(() => clearConfig()).not.toThrow();
    expect(loadConfig()).toBeNull();
    expect(listener).toHaveBeenLastCalledWith(null);
    expect(warning).toHaveBeenCalledTimes(2);
  } finally {
    unsubscribeBroken();
    unsubscribe();
  }
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
  const listener = vi.fn();
  const unsubscribe = subscribeConfig(listener);
  try {
    expect(() => saveConfig({ ...config, token: 'replacement' })).toThrow(
      'disk failure'
    );
    expect(listener).not.toHaveBeenCalled();
  } finally {
    unsubscribe();
  }
  expect(fs.readFileSync(path.join(dir, 'config.json'), 'utf8')).toBe(before);
  expect(fs.readdirSync(dir).filter((name) => name.endsWith('.tmp'))).toEqual(
    []
  );
});
