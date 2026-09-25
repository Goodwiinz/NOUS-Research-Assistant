// @vitest-environment node
import { mkdirSync, mkdtempSync, rmSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { afterEach, describe, expect, it } from 'vitest';

import {
  assertAgentQaRunLocationAvailable,
  resolveAgentQaConfig,
  resolveAgentQaReportPaths,
} from '../agentQaConfig';

const temporaryDirectories: string[] = [];
function stateDirectory(): string {
  const directory = mkdtempSync(join(tmpdir(), 'agent-qa-auth-'));
  temporaryDirectories.push(directory);
  return directory;
}

afterEach(() => {
  for (const directory of temporaryDirectories.splice(0)) {
    rmSync(directory, { recursive: true });
  }
});

describe('resolveAgentQaConfig', () => {
  it('leaves unrequested live runs disabled even when credentials exist', () => {
    expect(
      resolveAgentQaConfig({ AGENT_QA_EMAIL: 'tester@example.test' })
    ).toEqual({ enabled: false });
  });

  it('fails an explicitly enabled run instead of silently skipping without a target', () => {
    expect(() => resolveAgentQaConfig({ AGENT_QA_LIVE: '1' })).toThrow(
      'AGENT_QA_BASE_URL'
    );
  });

  it('requires authentication for an explicitly enabled run', () => {
    expect(() =>
      resolveAgentQaConfig({
        AGENT_QA_LIVE: '1',
        AGENT_QA_BASE_URL: 'https://example.test',
      })
    ).toThrow(
      'AGENT_QA_AUTH_STATE or both AGENT_QA_EMAIL and AGENT_QA_PASSWORD'
    );
  });

  it('preserves password bytes and accepts the existing smoke-test environment', () => {
    expect(
      resolveAgentQaConfig({
        AGENT_QA_LIVE: '1',
        SMOKE_BASE_URL: ' https://example.test/ ',
        SMOKE_USER_EMAIL: ' tester@example.test ',
        SMOKE_USER_PASSWORD: ' password with spaces ',
      })
    ).toEqual({
      enabled: true,
      baseURL: 'https://example.test',
      auth: {
        email: 'tester@example.test',
        password: ' password with spaces ',
      },
    });
  });

  it('accepts a saved browser session without a password', () => {
    const directory = stateDirectory();
    writeFileSync(
      join(directory, 'session.json'),
      JSON.stringify({ cookies: [], origins: [] })
    );
    expect(
      resolveAgentQaConfig(
        {
          AGENT_QA_LIVE: '1',
          NOUS_BASE_URL: 'https://example.test',
          NOUS_AUTH_STATE: 'session.json',
        },
        directory
      )
    ).toEqual({
      enabled: true,
      baseURL: 'https://example.test',
      auth: {
        storageState: join(directory, 'session.json'),
      },
    });
  });

  it('does not combine a Q&A email with a different smoke account password', () => {
    expect(() =>
      resolveAgentQaConfig({
        AGENT_QA_LIVE: '1',
        AGENT_QA_BASE_URL: 'https://example.test',
        AGENT_QA_EMAIL: 'qa@example.test',
        SMOKE_USER_EMAIL: 'smoke@example.test',
        SMOKE_USER_PASSWORD: 'smoke-password',
      })
    ).toThrow(
      'AGENT_QA_AUTH_STATE or both AGENT_QA_EMAIL and AGENT_QA_PASSWORD'
    );
  });

  it('fails before launching a browser when the saved session is missing or malformed', () => {
    const directory = stateDirectory();
    const environment = {
      AGENT_QA_LIVE: '1',
      AGENT_QA_BASE_URL: 'https://example.test',
      AGENT_QA_AUTH_STATE: 'session.json',
    };
    expect(() => resolveAgentQaConfig(environment, directory)).toThrow(
      'Cannot read Playwright authentication state'
    );
    writeFileSync(join(directory, 'session.json'), '{private-token');
    expect(() => resolveAgentQaConfig(environment, directory)).toThrow(
      'Invalid Playwright authentication state'
    );
    try {
      resolveAgentQaConfig(environment, directory);
    } catch (error) {
      expect(String(error)).not.toContain('private-token');
    }
    writeFileSync(join(directory, 'session.json'), '{}');
    expect(() => resolveAgentQaConfig(environment, directory)).toThrow(
      'Invalid Playwright authentication state'
    );
  });

  it.each([
    'not a URL',
    'file:///tmp/page',
    'https://user:secret@example.test',
  ])('rejects an invalid target without echoing it: %s', (baseURL) => {
    expect(() =>
      resolveAgentQaConfig({ AGENT_QA_LIVE: '1', AGENT_QA_BASE_URL: baseURL })
    ).toThrow(
      'AGENT_QA_BASE_URL must be an HTTP(S) origin without embedded credentials'
    );
  });
});

describe('resolveAgentQaReportPaths', () => {
  it('requires a separate, named output location for live runs', () => {
    expect(() => resolveAgentQaReportPaths({ AGENT_QA_LIVE: '1' })).toThrow(
      'AGENT_QA_RUN_LABEL'
    );
  });

  it('keeps all report files outside the historical result paths', () => {
    expect(
      resolveAgentQaReportPaths({
        AGENT_QA_LIVE: '1',
        AGENT_QA_RUN_LABEL: '2026-09-25-targeted-v122',
      })
    ).toEqual({
      outputDir: './test-results/agent-qa-runs/2026-09-25-targeted-v122',
      htmlOutputFolder:
        'playwright-agent-qa-report/runs/2026-09-25-targeted-v122',
      jsonOutputFile:
        'test-results/agent-qa-runs/2026-09-25-targeted-v122/results.json',
    });
  });

  it.each(['../old', 'same/name', '-bad', 'BAD', '', 'offline-list'])(
    'rejects an unsafe or unlabelled live output: %s',
    (label) => {
      expect(() =>
        resolveAgentQaReportPaths({
          AGENT_QA_LIVE: '1',
          AGENT_QA_RUN_LABEL: label,
        })
      ).toThrow('AGENT_QA_RUN_LABEL');
    }
  );

  it('uses disposable offline paths when just listing cases', () => {
    expect(resolveAgentQaReportPaths({})).toEqual({
      outputDir: './test-results/agent-qa-runs/offline-list',
      htmlOutputFolder: 'playwright-agent-qa-report/runs/offline-list',
      jsonOutputFile: 'test-results/agent-qa-runs/offline-list/results.json',
    });
  });

  it('refuses to overwrite artifacts from a previous live run', () => {
    const directory = stateDirectory();
    mkdirSync(join(directory, 'test-results/agent-qa-runs/previous'), {
      recursive: true,
    });
    const environment = {
      AGENT_QA_LIVE: '1',
      AGENT_QA_RUN_LABEL: 'previous',
    };
    const paths = resolveAgentQaReportPaths(environment);
    expect(() =>
      assertAgentQaRunLocationAvailable(paths, environment, directory)
    ).toThrow('AGENT_QA_RUN_LABEL already has artifacts');
  });

  it('allows a worker to reuse its coordinator-created output directory', () => {
    const directory = stateDirectory();
    const environment = {
      AGENT_QA_LIVE: '1',
      AGENT_QA_RUN_LABEL: 'worker-run',
    };
    const paths = resolveAgentQaReportPaths(environment);
    mkdirSync(join(directory, paths.outputDir), { recursive: true });

    const workerEnvironment = { ...environment, TEST_WORKER_INDEX: '0' };
    expect(resolveAgentQaReportPaths(workerEnvironment)).toEqual(paths);
    expect(() =>
      assertAgentQaRunLocationAvailable(paths, workerEnvironment, directory)
    ).not.toThrow();
  });
});
