import { existsSync, readFileSync } from 'node:fs';
import { spawnSync } from 'node:child_process';
import { dirname, resolve } from 'node:path';
import { randomUUID } from 'node:crypto';
import { fileURLToPath } from 'node:url';

import { redactText, writeReports } from './report.mjs';
import { exitCodeForReport, runCampaign } from './runner.mjs';
import { listScenarios } from './scenarios.mjs';

const SUITES = new Set(['smoke', 'workflow', 'adversarial', 'all']);
// A deployment identity must be a complete immutable reference.  Short
// labels, VERSION strings, and arbitrary image tags are not commit evidence.
const FULL_IDENTITY = /^(?:[0-9a-f]{40}|[0-9a-f]{64}|sha256:[0-9a-f]{64})$/i;
const MAX_EVIDENCE_AGE_MS = 24 * 60 * 60 * 1000;
const TOOL_REPO_ROOT = resolve(dirname(fileURLToPath(import.meta.url)), '../../..');

export class CLIConfigError extends Error {
  constructor(message) {
    super(message);
    this.name = 'CLIConfigError';
  }
}

function valueAfter(argv, index, flag) {
  const value = argv[index + 1];
  if (!value || value.startsWith('--')) throw new CLIConfigError(`${flag} requires a value`);
  return value;
}

function diagnosticSecrets(env = process.env) {
  return [env.NOUS_QA_PASSWORD, env.NOUS_QA_TOKEN, env.NOUS_QA_EMAIL].filter(Boolean);
}

function gitOutput(args) {
  try {
    const result = spawnSync('git', args, {
      cwd: TOOL_REPO_ROOT,
      encoding: 'utf8',
      stdio: ['ignore', 'pipe', 'ignore'],
      timeout: 1_000,
      maxBuffer: 64 * 1024,
    });
    if (result.error || result.status !== 0) return null;
    return result.stdout.trim();
  } catch {
    return null;
  }
}

export function observeSourceIdentity(env = process.env) {
  const explicitSha = typeof env.NOUS_QA_SOURCE_SHA === 'string' && env.NOUS_QA_SOURCE_SHA.trim()
    ? env.NOUS_QA_SOURCE_SHA.trim()
    : null;
  const explicitDirty = typeof env.NOUS_QA_SOURCE_DIRTY === 'string' && env.NOUS_QA_SOURCE_DIRTY.trim()
    ? env.NOUS_QA_SOURCE_DIRTY.trim().slice(0, 32)
    : null;
  const observedSha = explicitSha ?? gitOutput(['rev-parse', 'HEAD']);
  const status = explicitDirty ?? gitOutput(['status', '--porcelain']);
  const dirty = explicitDirty ?? (status === null ? 'unknown' : status.length > 0 ? 'dirty' : 'clean');
  const provenance = [
    explicitSha ? 'NOUS_QA_SOURCE_SHA override' : 'git HEAD',
    explicitDirty ? 'NOUS_QA_SOURCE_DIRTY override' : 'git worktree status',
  ].join('; ');
  return {
    sha: observedSha && /^[0-9a-f]{40}$/i.test(observedSha) ? observedSha : null,
    dirty,
    provenance,
  };
}

function diagnosticMessage(error, env = process.env) {
  return redactText(error?.message ?? error, diagnosticSecrets(env))
    .replace(/(--[a-z0-9_-]*(?:password|passwd|email|token|secret|api[-_]?key|authorization|cookie)[a-z0-9_-]*)(?:=[^\s]+)?/gi, '$1 [REDACTED]');
}

function rejectCredentialArgument(arg) {
  if (/^--[a-z0-9_-]*(?:password|passwd|email|token|secret|api[-_]?key|authorization|cookie)[a-z0-9_-]*(?:=|$)/i.test(arg)) {
    throw new CLIConfigError('Credential-bearing command-line arguments are not accepted; use the documented environment variables');
  }
}

function integer(value, flag, min, max) {
  if (!/^[0-9]+$/.test(value)) throw new CLIConfigError(`${flag} must be an integer`);
  const parsed = Number(value);
  if (!Number.isSafeInteger(parsed) || parsed < min || parsed > max) {
    throw new CLIConfigError(`${flag} must be between ${min} and ${max}`);
  }
  return parsed;
}

export function validateTargetUrl(value, label) {
  let parsed;
  try { parsed = new URL(value); } catch { throw new CLIConfigError(`${label} must be a valid URL`); }
  if (!['http:', 'https:'].includes(parsed.protocol)) throw new CLIConfigError(`${label} must use http or https`);
  if (parsed.username || parsed.password) throw new CLIConfigError(`${label} must not contain URL credentials/userinfo`);
  if (parsed.search || parsed.hash) throw new CLIConfigError(`${label} must not contain query or fragment data`);
  if (!parsed.hostname) throw new CLIConfigError(`${label} must include a hostname`);
  parsed.pathname = parsed.pathname.replace(/\/+$/, '') || '/';
  return parsed.toString().replace(/\/$/, '');
}

export function isFullIdentity(value) {
  return typeof value === 'string' && FULL_IDENTITY.test(value);
}

function readDeploymentEvidence(path, _baseUrl, apiUrl) {
  const fullPath = resolve(path);
  if (!existsSync(fullPath)) throw new CLIConfigError(`Deployment evidence file does not exist: ${fullPath}`);
  let value;
  try { value = JSON.parse(readFileSync(fullPath, 'utf8')); } catch { throw new CLIConfigError('Deployment evidence must be valid JSON'); }
  if (!value || typeof value !== 'object' || Array.isArray(value)) throw new CLIConfigError('Deployment evidence must be a JSON object');
  for (const key of ['targetUrl', 'backendSha', 'observedAt', 'provenance']) {
    if (typeof value[key] !== 'string' || !value[key].trim()) throw new CLIConfigError(`Deployment evidence requires ${key}`);
  }
  const targetUrl = validateTargetUrl(value.targetUrl, 'deployment evidence targetUrl');
  const observed = new URL(targetUrl);
  // Deployment evidence identifies the backend operator observed.  The
  // frontend target is deliberately not accepted here because a frontend and
  // backend may be deployed independently and the backend API can be on a
  // different origin.
  const acceptedTargets = [new URL(apiUrl), new URL(new URL(apiUrl).origin)]
    .map((value) => ({ origin: value.origin, pathname: value.pathname.replace(/\/$/, '') || '/' }));
  const matchesConfiguredTarget = acceptedTargets.some((candidate) =>
    candidate.origin === observed.origin &&
    candidate.pathname === (observed.pathname.replace(/\/$/, '') || '/'));
  if (!matchesConfiguredTarget) {
    throw new CLIConfigError('Deployment evidence targetUrl does not match the configured backend API target');
  }
  if (!isFullIdentity(value.backendSha)) throw new CLIConfigError('Deployment evidence backendSha must be a full git SHA or digest');
  if (value.frontendSha !== undefined && (typeof value.frontendSha !== 'string' || !isFullIdentity(value.frontendSha))) {
    throw new CLIConfigError('Deployment evidence frontendSha must be a full git SHA or digest');
  }
  const observedAtMs = Date.parse(value.observedAt);
  if (!Number.isFinite(observedAtMs)) throw new CLIConfigError('Deployment evidence observedAt must be an ISO timestamp');
  const age = Date.now() - observedAtMs;
  if (age < -5 * 60 * 1000 || age > MAX_EVIDENCE_AGE_MS) throw new CLIConfigError('Deployment evidence is stale or from the future');
  if (/[\u0000-\u001f\u007f]/.test(value.provenance) || value.provenance.length > 500) throw new CLIConfigError('Deployment evidence provenance is invalid');
  return {
    targetUrl,
    backendSha: value.backendSha,
    frontendSha: value.frontendSha ?? null,
    observedAt: new Date(observedAtMs).toISOString(),
    provenance: value.provenance,
    path: fullPath,
  };
}

function shellQuote(value) {
  return `'${String(value).replace(/'/g, "'\\''")}'`;
}

function usage() {
  return [
    'Usage: pnpm qa:nous [options]',
    '',
    '  --list                         list scenarios without network/browser work',
    '  --base-url URL                 configured frontend target (default http://127.0.0.1:3000)',
    '  --api-url URL                  explicitly configured backend API base',
    '  --suite smoke|workflow|adversarial|all',
    '  --scenario ID                  select one scenario (repeatable)',
    '  --output-dir DIR               private JSON/HTML report directory',
    '  --storage-state FILE           Playwright storage state for authenticated runs',
    '  --allow-writes                 permit synthetic writes and cleanup',
    '  --expected-backend-sha VALUE   require a server or matching operator identity',
    '  --deployment-evidence FILE     validated operator-observed deployment JSON',
    '  --timeout-ms N                 per-scenario timeout (1..300000)',
    '  --max-turns N                  model turn bound (1..50, default 12)',
    '  --help                         show this help',
  ].join('\n');
}

export function parseArgs(argv = [], env = process.env) {
  if (!Array.isArray(argv)) throw new CLIConfigError('argv must be an array');
  let listOnly = false;
  let baseUrl = env.NOUS_QA_BASE_URL ?? 'http://127.0.0.1:3000';
  let apiUrl = env.NOUS_QA_API_URL ?? null;
  let suite = env.NOUS_QA_SUITE ?? 'smoke';
  let outputDir = env.NOUS_QA_OUTPUT_DIR ?? null;
  let storageState = env.NOUS_QA_STORAGE_STATE ?? null;
  let allowWrites = false;
  let expectedBackendSha = env.NOUS_QA_EXPECTED_BACKEND_SHA ?? null;
  let deploymentEvidencePath = null;
  let timeoutMs = 30_000;
  let maxTurns = 12;
  const selectedIds = [];
  let help = false;

  for (let index = 0; index < argv.length; index += 1) {
    const arg = argv[index];
    rejectCredentialArgument(arg);
    if (arg === '--list') { listOnly = true; continue; }
    if (arg === '--allow-writes') { allowWrites = true; continue; }
    if (arg === '--help' || arg === '-h') { help = true; continue; }
    if (arg === '--base-url') { baseUrl = valueAfter(argv, index++, arg); continue; }
    if (arg === '--api-url') { apiUrl = valueAfter(argv, index++, arg); continue; }
    if (arg === '--suite') { suite = valueAfter(argv, index++, arg); continue; }
    if (arg === '--scenario' || arg === '--select') { selectedIds.push(valueAfter(argv, index++, arg)); continue; }
    if (arg === '--output-dir') { outputDir = valueAfter(argv, index++, arg); continue; }
    if (arg === '--storage-state') { storageState = valueAfter(argv, index++, arg); continue; }
    if (arg === '--expected-backend-sha') { expectedBackendSha = valueAfter(argv, index++, arg); continue; }
    if (arg === '--deployment-evidence') { deploymentEvidencePath = valueAfter(argv, index++, arg); continue; }
    if (arg === '--timeout-ms') { timeoutMs = integer(valueAfter(argv, index++, arg), arg, 1, 300_000); continue; }
    if (arg === '--max-turns') { maxTurns = integer(valueAfter(argv, index++, arg), arg, 1, 50); continue; }
    throw new CLIConfigError(`Unknown argument: ${arg}`);
  }
  if (!SUITES.has(suite)) throw new CLIConfigError(`Unknown suite: ${suite}`);
  baseUrl = validateTargetUrl(baseUrl, '--base-url');
  apiUrl = validateTargetUrl(apiUrl ?? `${new URL(baseUrl).origin}/api/v1`, '--api-url');
  if (expectedBackendSha && !isFullIdentity(expectedBackendSha)) throw new CLIConfigError('--expected-backend-sha must be a full git SHA or digest');
  if (storageState) {
    storageState = resolve(storageState);
    if (!existsSync(storageState)) throw new CLIConfigError(`Storage state file does not exist: ${storageState}`);
  }
  const credentials = env.NOUS_QA_EMAIL && env.NOUS_QA_PASSWORD
    ? { email: env.NOUS_QA_EMAIL, password: env.NOUS_QA_PASSWORD }
    : null;
  const secrets = [env.NOUS_QA_PASSWORD, env.NOUS_QA_TOKEN, credentials?.email].filter(Boolean);
  const deploymentEvidence = deploymentEvidencePath ? readDeploymentEvidence(deploymentEvidencePath, baseUrl, apiUrl) : null;
  const runId = `${new Date().toISOString().replace(/[-:.TZ]/g, '').slice(0, 14)}-${randomUUID().slice(0, 8)}`;
  const reportDir = resolve(outputDir ?? `.nous-qa-reports/${runId}`);
  const command = redactText(['pnpm', 'qa:nous', ...argv].map(shellQuote).join(' '), secrets);
  return {
    listOnly,
    help,
    suite,
    selectedIds,
    baseUrl,
    apiUrl,
    outputDir: reportDir,
    storageState,
    allowWrites,
    expectedBackendSha,
    deploymentEvidence,
    timeoutMs,
    maxTurns,
    credentials,
    authToken: env.NOUS_QA_TOKEN ?? null,
    secrets,
    runId,
    command,
    sourceIdentity: observeSourceIdentity(env),
  };
}

export async function main(argv = process.argv.slice(2), env = process.env, dependencies = {}) {
  let config;
  try {
    config = parseArgs(argv, env);
  } catch (error) {
    console.error(`nous-qa: ${diagnosticMessage(error, env)}`);
    return 2;
  }
  if (config.help) {
    console.log(usage());
    return 0;
  }
  if (config.listOnly) {
    console.log(JSON.stringify(listScenarios(config.suite).map((scenario) => ({
      id: scenario.id,
      title: scenario.title,
      suite: scenario.suite,
      prerequisites: scenario.prerequisites,
      mode: scenario.mode ?? 'live',
      createsFixtures: Boolean(scenario.createsFixtures),
      callsModel: Boolean(scenario.callsModel),
    })), null, 2));
    return 0;
  }
  let report;
  try {
    report = await runCampaign(config, dependencies);
    const paths = await writeReports(report, config.outputDir);
    console.log(`NOUS QA ${report.run.id}: ${report.summary.passed} passed, ${report.summary.failed} failed, ${report.summary.blocked} blocked, ${report.summary.skipped} skipped`);
    console.log(`JSON: ${paths.jsonPath}`);
    console.log(`HTML: ${paths.htmlPath}`);
    return exitCodeForReport(report);
  } catch (error) {
    console.error(`nous-qa: ${diagnosticMessage(error, env)}`);
    return 2;
  }
}

export { usage };
