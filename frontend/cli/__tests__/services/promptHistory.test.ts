/**
 * @vitest-environment node
 */
import { afterEach, beforeEach, describe, expect, test, vi } from 'vitest';
import { mkdtempSync, rmSync, writeFileSync } from 'fs';
import * as os from 'os';
import * as path from 'path';

describe('promptHistory', () => {
  let tmpDir: string;
  let store: typeof import('../../services/promptHistory');

  beforeEach(async () => {
    tmpDir = mkdtempSync(path.join(os.tmpdir(), 'nous-prompthistory-'));
    process.env.NOUS_CONFIG_DIR = tmpDir;
    vi.resetModules();
    store = await import('../../services/promptHistory');
  });

  afterEach(() => {
    delete process.env.NOUS_CONFIG_DIR;
    rmSync(tmpDir, { recursive: true, force: true });
  });

  test('returns empty when file is missing', () => {
    expect(store.loadHistory()).toEqual([]);
  });

  test('appends and reads back entries in order', () => {
    store.appendHistory('first');
    store.appendHistory('second');
    store.appendHistory('third');
    expect(store.loadHistory()).toEqual(['first', 'second', 'third']);
  });

  test('skips consecutive duplicates and empty lines', () => {
    store.appendHistory('a');
    store.appendHistory('a');
    store.appendHistory('   ');
    store.appendHistory('b');
    expect(store.loadHistory()).toEqual(['a', 'b']);
  });

  test('caps entries at 500', () => {
    for (let i = 0; i < 600; i++) store.appendHistory(`q${i}`);
    const out = store.loadHistory();
    expect(out).toHaveLength(500);
    expect(out[0]).toBe('q100');
    expect(out[out.length - 1]).toBe('q599');
  });

  test('migrates legacy entries and preserves multiline prompts as single records', () => {
    writeFileSync(path.join(tmpDir, 'prompt_history'), 'first\nsecond\n');
    expect(store.loadHistory()).toEqual(['first', 'second']);
    store.appendHistory('two lines\nwith \"quotes\"');
    store.appendHistory('two lines\nwith \"quotes\"');
    expect(store.loadHistory()).toEqual([
      'first',
      'second',
      'two lines\nwith \"quotes\"',
    ]);
  });

  test('preserves valid entries around a corrupt record when appending', () => {
    writeFileSync(
      path.join(tmpDir, 'prompt_history'),
      '# nous-prompt-history-v1\n"first"\n{"broken\n"second"\n'
    );
    store.appendHistory('third');
    expect(store.loadHistory()).toEqual(['first', 'second', 'third']);
  });

  test('clearHistory empties the file', () => {
    store.appendHistory('one');
    store.clearHistory();
    expect(store.loadHistory()).toEqual([]);
  });
});
