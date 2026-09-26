import { existsSync, readFileSync } from 'node:fs';
import { resolve } from 'node:path';

export type AgentQaConfig =
  | { enabled: false }
  | {
      enabled: true;
      baseURL: string;
      auth: { storageState: string } | { email: string; password: string };
    };

export interface AgentQaReportPaths {
  outputDir: string;
  htmlOutputFolder: string;
  jsonOutputFile: string;
}

/** Keep live run artifacts separate from each other and the historical report. */
export function resolveAgentQaReportPaths(
  environment: NodeJS.ProcessEnv = process.env
): AgentQaReportPaths {
  const live = environment.AGENT_QA_LIVE === '1';
  const runLabel = live
    ? (environment.AGENT_QA_RUN_LABEL ?? '').trim()
    : 'offline-list';
  if (
    !/^[a-z0-9]+(?:-[a-z0-9]+)*$/.test(runLabel) ||
    runLabel.length > 80 ||
    (live && runLabel === 'offline-list')
  ) {
    throw new Error(
      'Set AGENT_QA_RUN_LABEL to a new lowercase, hyphen-separated run name.'
    );
  }

  const outputDir = `./test-results/agent-qa-runs/${runLabel}`;
  const htmlOutputFolder = `playwright-agent-qa-report/runs/${runLabel}`;
  return {
    outputDir,
    htmlOutputFolder,
    jsonOutputFile: `test-results/agent-qa-runs/${runLabel}/results.json`,
  };
}

/** Check collisions only in Playwright's coordinator, before it creates outputDir. */
export function assertAgentQaRunLocationAvailable(
  paths: AgentQaReportPaths,
  environment: NodeJS.ProcessEnv = process.env,
  baseDirectory = process.cwd()
): void {
  if (
    environment.AGENT_QA_LIVE !== '1' ||
    environment.TEST_WORKER_INDEX !== undefined
  ) {
    return;
  }
  if (
    [paths.outputDir, paths.htmlOutputFolder].some((directory) =>
      existsSync(resolve(baseDirectory, directory))
    )
  ) {
    throw new Error(
      'AGENT_QA_RUN_LABEL already has artifacts. Choose a new run name.'
    );
  }
}

/** Resolve the same configuration in the Playwright runner and its workers. */
export function resolveAgentQaConfig(
  environment: NodeJS.ProcessEnv = process.env,
  baseDirectory = process.cwd()
): AgentQaConfig {
  if (environment.AGENT_QA_LIVE !== '1') return { enabled: false };

  const target = (
    environment.AGENT_QA_BASE_URL ||
    environment.NOUS_BASE_URL ||
    environment.SMOKE_BASE_URL ||
    environment.BASE_URL ||
    ''
  ).trim();
  if (!target) throw new Error('Set AGENT_QA_BASE_URL for live agent Q&A.');

  let url: URL;
  try {
    url = new URL(target);
    if (
      !['http:', 'https:'].includes(url.protocol) ||
      url.username ||
      url.password ||
      url.pathname !== '/' ||
      url.search ||
      url.hash
    )
      throw new Error();
  } catch {
    throw new Error(
      'AGENT_QA_BASE_URL must be an HTTP(S) origin without embedded credentials'
    );
  }

  const stateFile = (
    environment.AGENT_QA_AUTH_STATE ||
    environment.NOUS_AUTH_STATE ||
    ''
  ).trim();
  if (stateFile) {
    const storageState = resolve(baseDirectory, stateFile);
    let raw: string;
    try {
      raw = readFileSync(storageState, 'utf8');
    } catch {
      throw new Error(
        'Cannot read Playwright authentication state. Check AGENT_QA_AUTH_STATE.'
      );
    }
    try {
      const state = JSON.parse(raw);
      if (!Array.isArray(state?.cookies) || !Array.isArray(state?.origins))
        throw new Error();
    } catch {
      // JSON parse errors can quote private cookie content; never forward them.
      throw new Error(
        'Invalid Playwright authentication state. Save a fresh browser session.'
      );
    }
    return { enabled: true, baseURL: url.origin, auth: { storageState } };
  }

  // Use a complete credential pair from one namespace, never mix accounts.
  const ownCredentials =
    environment.AGENT_QA_EMAIL !== undefined ||
    environment.AGENT_QA_PASSWORD !== undefined;
  const email = (
    ownCredentials ? environment.AGENT_QA_EMAIL : environment.SMOKE_USER_EMAIL
  )?.trim();
  const password = ownCredentials
    ? environment.AGENT_QA_PASSWORD
    : environment.SMOKE_USER_PASSWORD;
  if (!email || !password) {
    throw new Error(
      'Set AGENT_QA_AUTH_STATE or both AGENT_QA_EMAIL and AGENT_QA_PASSWORD for live agent Q&A.'
    );
  }
  return { enabled: true, baseURL: url.origin, auth: { email, password } };
}
