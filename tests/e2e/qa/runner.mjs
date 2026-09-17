import { randomUUID } from 'node:crypto';

import {
  CASE_STATUSES,
  REPORT_SCHEMA_VERSION,
  configForReport,
  redactValue,
  sanitizeError,
} from './report.mjs';
import { createSession } from './session.mjs';
import { registry as defaultRegistry } from './scenarios.mjs';

const SUITES = new Set(['smoke', 'workflow', 'adversarial', 'all']);
const FULL_IDENTITY = /^(?:[0-9a-f]{40}|[0-9a-f]{64}|sha256:[0-9a-f]{64})$/i;

class ScenarioTimeoutError extends Error {
  constructor(timeoutMs) {
    super(`Scenario timed out after ${timeoutMs}ms`);
    this.name = 'ScenarioTimeoutError';
    this.code = 'SCENARIO_TIMEOUT';
    this.timedOut = true;
  }
}

function nowIso(clock = Date) {
  return new clock().toISOString();
}

function validConfig(config = {}) {
  if (!SUITES.has(config.suite ?? 'smoke')) throw new Error(`Unknown suite: ${config.suite}`);
  if (!config.baseUrl || !config.apiUrl) throw new Error('baseUrl and apiUrl are required');
  if (!Number.isInteger(config.timeoutMs) || config.timeoutMs < 1 || config.timeoutMs > 300_000) {
    throw new Error('timeoutMs must be an integer between 1 and 300000');
  }
  if (!Number.isInteger(config.maxTurns) || config.maxTurns < 1 || config.maxTurns > 50) {
    throw new Error('maxTurns must be an integer between 1 and 50');
  }
}

function selectedScenarios(config, registry) {
  const suites = config.suite === 'all' ? new Set(['smoke', 'workflow', 'adversarial']) : new Set([config.suite]);
  const selectedIds = Array.isArray(config.selectedIds) && config.selectedIds.length
    ? new Set(config.selectedIds)
    : null;
  return registry.filter((scenario) => {
    if (!suites.has(scenario.suite)) return false;
    return !selectedIds || selectedIds.has(scenario.id);
  });
}

function selectionError(config, registry) {
  if (!Array.isArray(config.selectedIds) || config.selectedIds.length === 0) return null;
  const suites = config.suite === 'all'
    ? new Set(['smoke', 'workflow', 'adversarial'])
    : new Set([config.suite]);
  const available = new Set(registry
    .filter((scenario) => suites.has(scenario.suite))
    .map((scenario) => scenario.id));
  const unknown = [...new Set(config.selectedIds)].filter((id) => !available.has(id));
  return unknown.length > 0
    ? `Unknown or out-of-suite scenario selection: ${unknown.join(', ')}`
    : null;
}

function prerequisiteResult(scenario, config, globalWriteGate = null) {
  const prerequisites = Array.isArray(scenario.prerequisites) ? scenario.prerequisites : [];
  if (globalWriteGate && (prerequisites.includes('writes') || scenario.callsModel)) {
    return globalWriteGate;
  }
  for (const prerequisite of prerequisites) {
    const name = typeof prerequisite === 'string' ? prerequisite : prerequisite?.id;
    switch (name) {
      case 'auth':
        if (!config.credentials?.email || !config.credentials?.password) {
          if (!config.storageState) return { status: 'BLOCKED', reason: 'Missing credentials: provide NOUS_QA_EMAIL/NOUS_QA_PASSWORD or --storage-state' };
        }
        break;
      case 'writes':
        if (!config.allowWrites) return { status: 'BLOCKED', reason: 'Scenario requires explicit --allow-writes' };
        break;
      case 'model':
        if (!config.allowWrites) return { status: 'BLOCKED', reason: 'Model scenario requires explicit --allow-writes' };
        if (!config.credentials?.email || !config.credentials?.password) {
          if (!config.storageState) return { status: 'BLOCKED', reason: 'Missing credentials for model scenario' };
        }
        break;
      case 'identity':
        // The health scenario performs the actual server identity probe. Do
        // not pre-block it when external evidence is absent: a server may
        // expose a full identity even though the public health contract does
        // not require one.
        break;
      case 'browser':
        // Playwright availability is checked by the session when the scenario
        // starts. Keeping this prerequisite declarative makes --list useful.
        break;
      case 'anonymous':
        if (config.storageState) return { status: 'BLOCKED', reason: 'Anonymous browser scenario cannot run with --storage-state' };
        break;
      case 'deployment':
        if (!config.deploymentEvidence) return { status: 'BLOCKED', reason: 'Operator deployment evidence is required' };
        break;
      default:
        break;
    }
  }
  return null;
}

function caseResult(scenario, status, details = {}) {
  const normalized = CASE_STATUSES.includes(status) ? status : 'FAIL';
  return {
    id: scenario.id,
    title: scenario.title,
    suite: scenario.suite,
    status: normalized,
    reason: details.reason ?? null,
    assertion: details.assertion ?? null,
    evidence: details.evidence ?? [],
    prerequisites: scenario.prerequisites ?? [],
    mode: scenario.mode ?? 'live',
    createsFixtures: Boolean(scenario.createsFixtures),
    callsModel: Boolean(scenario.callsModel),
    durationMs: details.durationMs ?? 0,
    requests: details.requests ?? [],
  };
}

async function runWithTimeout(work, timeoutMs, onTimeout) {
  let timer;
  let timedOut = false;
  let cleanupPromise = Promise.resolve();
  const operation = Promise.resolve().then(work);
  const timeout = new Promise((_, reject) => {
    timer = setTimeout(() => {
      timedOut = true;
      cleanupPromise = Promise.resolve().then(() => onTimeout?.());
      // The cleanup is awaited below with its own bound. Attaching a handler
      // here also prevents a late abort/close rejection becoming unhandled if
      // the cleanup itself outlives that bound.
      cleanupPromise.catch(() => {});
      reject(new ScenarioTimeoutError(timeoutMs));
    }, timeoutMs);
  });
  // Attach a rejection handler to the operation even when the timeout wins;
  // a browser request that cannot be interrupted must not become an unhandled
  // rejection after the report has been emitted.
  operation.catch(() => {});
  try {
    return await Promise.race([operation, timeout]);
  } finally {
    clearTimeout(timer);
    if (timedOut) {
      await Promise.race([
        cleanupPromise,
        new Promise((resolve) => setTimeout(resolve, Math.min(1_000, timeoutMs))),
      ]).catch(() => {});
    }
  }
}

function identityFromHealth(data) {
  if (!data || typeof data !== 'object') return null;
  for (const key of ['git_sha', 'commit_sha', 'build_sha', 'revision', 'digest']) {
    if (typeof data[key] === 'string' && FULL_IDENTITY.test(data[key])) return { value: data[key], field: key, provenance: 'server-health' };
  }
  return null;
}

export function checkExpectedBackendIdentity(config, healthData) {
  if (!config.expectedBackendSha) return { status: 'not-required' };
  const actual = identityFromHealth(healthData);
  if (actual?.value === config.expectedBackendSha) return { status: 'matched', ...actual };
  if (actual) {
    return { status: 'blocked', reason: 'Server health exposed a backend identity that does not match --expected-backend-sha' };
  }
  const evidence = config.deploymentEvidence;
  if (FULL_IDENTITY.test(evidence?.backendSha ?? '') && evidence?.backendSha === config.expectedBackendSha) {
    return { status: 'matched', value: evidence.backendSha, provenance: evidence.provenance };
  }
  return { status: 'blocked', reason: 'Expected backend SHA could not be attested by the server or matching operator evidence' };
}

async function identityGateForWrites(session, config) {
  if (!config.expectedBackendSha) return { gate: null, actual: null };
  const evidenceMatches = FULL_IDENTITY.test(config.deploymentEvidence?.backendSha ?? '')
    && config.deploymentEvidence.backendSha === config.expectedBackendSha;
  let actual = null;
  if (typeof session?.request === 'function') {
    try {
      const health = await session.request('/health', { target: 'backend', forwardAuth: false });
      actual = identityFromHealth(health.data);
    } catch {
      // A separately observed, fresh identity can still authorize the gate
      // when the public health endpoint is unavailable.
    }
  }
  if (actual?.value === config.expectedBackendSha) {
    return { gate: null, actual };
  }
  if (actual && actual.value !== config.expectedBackendSha) {
    return {
      gate: { status: 'BLOCKED', reason: 'Server health exposed a backend identity that does not match --expected-backend-sha' },
      actual,
    };
  }
  if (evidenceMatches) return { gate: null, actual: null };
  return {
    gate: { status: 'BLOCKED', reason: 'Expected backend SHA must match an actual server identity or validated deployment evidence before writes or model execution' },
    actual: null,
  };
}

export function exitCodeForReport(report) {
  const summary = report?.summary ?? {};
  if (summary.failed > 0) return 1;
  if (summary.invalid || summary.blocked || summary.skipped || summary.incomplete || summary.selected === 0) return 2;
  return 0;
}

export async function runCampaign(config, options = {}) {
  const normalizedConfig = { suite: 'smoke', timeoutMs: 30_000, maxTurns: 12, ...config };
  validConfig(normalizedConfig);
  const clock = options.clock ?? Date;
  const registry = options.registry ?? defaultRegistry;
  const invalidSelectionReason = selectionError(normalizedConfig, registry);
  const scenarios = invalidSelectionReason ? [] : selectedScenarios(normalizedConfig, registry);
  const runId = normalizedConfig.runId ?? `${new Date().toISOString().replace(/[-:.TZ]/g, '').slice(0, 14)}-${randomUUID().slice(0, 8)}`;
  const startedAt = nowIso(clock);
  const report = {
    schemaVersion: REPORT_SCHEMA_VERSION,
    run: {
      id: runId,
      startedAt,
      finishedAt: null,
      command: normalizedConfig.command ?? 'pnpm qa:nous',
      target: normalizedConfig.baseUrl,
      localSource: normalizedConfig.sourceIdentity ?? {
        sha: process.env.NOUS_QA_SOURCE_SHA ?? null,
        dirty: process.env.NOUS_QA_SOURCE_DIRTY ?? 'unknown',
      },
      configuration: configForReport({ ...normalizedConfig, runId }),
      observedIdentity: {
        backendSha: normalizedConfig.deploymentEvidence?.backendSha ?? null,
        frontendSha: normalizedConfig.deploymentEvidence?.frontendSha ?? null,
        provenance: normalizedConfig.deploymentEvidence?.provenance ?? null,
      },
    },
    cases: [],
    cleanup: { status: 'not-started', retained: [], errors: [] },
    summary: {
      selected: scenarios.length,
      passed: 0,
      failed: 0,
      blocked: 0,
      skipped: 0,
      incomplete: false,
      invalid: false,
      reason: null,
      modelTurns: 0,
      maxModelTurns: normalizedConfig.maxTurns,
    },
  };
  if (scenarios.length === 0) {
    report.summary.reason = invalidSelectionReason ?? 'No scenarios selected';
    report.summary.invalid = Boolean(invalidSelectionReason);
    report.summary.incomplete = true;
    report.run.modelTurns = 0;
    report.run.finishedAt = nowIso(clock);
    return report;
  }

  let session;
  let modelTurns = 0;
  let globalWriteGate = null;
  let campaignHalted = false;
  try {
    session = await (options.sessionFactory ?? createSession)(
      { ...normalizedConfig, runId },
      options.dependencies ?? {}
    );
    if (normalizedConfig.expectedBackendSha && scenarios.some((scenario) =>
      (scenario.prerequisites ?? []).includes('writes') || scenario.callsModel
    )) {
      const identityProbe = await identityGateForWrites(session, normalizedConfig);
      globalWriteGate = identityProbe.gate;
      report.run.identityProbe = identityProbe.actual
        ? { field: identityProbe.actual.field, provenance: identityProbe.actual.provenance }
        : { status: globalWriteGate ? 'blocked' : 'external-evidence' };
      if (identityProbe.actual) {
        report.run.observedIdentity = {
          backendSha: identityProbe.actual.value,
          frontendSha: normalizedConfig.deploymentEvidence?.frontendSha ?? null,
          provenance: identityProbe.actual.provenance,
        };
      }
    }
    for (const scenario of scenarios) {
      const started = Date.now();
      if (campaignHalted) {
        report.cases.push(caseResult(scenario, 'BLOCKED', {
          reason: 'Campaign halted after a timed-out scenario; the session remained quarantined to prevent late actions',
          durationMs: Date.now() - started,
          requests: [],
        }));
        continue;
      }
      const prerequisite = prerequisiteResult(scenario, normalizedConfig, globalWriteGate);
      if (prerequisite) {
        report.cases.push(caseResult(scenario, prerequisite.status, {
          reason: prerequisite.reason,
          durationMs: Date.now() - started,
          requests: [],
        }));
        continue;
      }
      const beforeRequests = session?.observations?.length ?? 0;
      try {
        const evidence = {
          runId,
          fixturePrefix: session?.fixturePrefix ?? `NOUS QA ${runId}`,
          maxTurns: normalizedConfig.maxTurns,
          record: (item) => redactValue(item, normalizedConfig.secrets ?? []),
          identity: (data) => {
            const actual = identityFromHealth(data);
            if (actual) {
              report.run.observedIdentity = {
                backendSha: actual.value,
                frontendSha: normalizedConfig.deploymentEvidence?.frontendSha ?? null,
                provenance: actual.provenance,
              };
            }
            return checkExpectedBackendIdentity(normalizedConfig, data);
          },
          consumeModelTurn: () => {
            modelTurns += 1;
            if (modelTurns > normalizedConfig.maxTurns) {
              throw new Error(`Model turn bound exceeded (${normalizedConfig.maxTurns})`);
            }
            return modelTurns;
          },
        };
        const modelTurnsAtStart = modelTurns;
        const result = await runWithTimeout(
          () => scenario.run(session, evidence),
          normalizedConfig.timeoutMs,
          () => session?.quarantine?.() ?? session?.abort?.()
        );
        const status = result?.status ?? 'PASS';
        const details = typeof result === 'object' && result !== null ? result : {};
        if (scenario.callsModel && status === 'PASS' && modelTurns === modelTurnsAtStart) {
          throw new Error('Model scenario did not account for a model turn');
        }
        report.cases.push(caseResult(scenario, status, {
          ...details,
          reason: details.reason ?? (status === 'PASS' ? null : undefined),
          durationMs: Date.now() - started,
          requests: (session?.observations ?? []).slice(beforeRequests),
        }));
      } catch (error) {
        report.cases.push(caseResult(scenario, 'FAIL', {
          reason: sanitizeError(error, normalizedConfig.secrets ?? []).message,
          evidence: [sanitizeError(error, normalizedConfig.secrets ?? [])],
          durationMs: Date.now() - started,
          requests: (session?.observations ?? []).slice(beforeRequests),
        }));
        if (error?.code === 'SCENARIO_TIMEOUT' || error?.timedOut === true) campaignHalted = true;
      }
    }
  } catch (error) {
    const safe = sanitizeError(error, normalizedConfig.secrets ?? []);
    report.summary.reason = `Session setup failed: ${safe.message}`;
    for (const scenario of scenarios) {
      report.cases.push(caseResult(scenario, 'FAIL', { reason: safe.message, evidence: [safe] }));
    }
  } finally {
    if (session) {
      try {
        report.cleanup = typeof session.cleanup === 'function'
          ? await session.cleanup()
          : { status: 'complete', retained: [], errors: [] };
      } catch (error) {
        const safe = sanitizeError(error, normalizedConfig.secrets ?? []);
        report.cleanup = { status: 'incomplete', retained: [], errors: [safe] };
      }
      try {
        await session.close?.();
      } catch (error) {
        const safe = sanitizeError(error, normalizedConfig.secrets ?? []);
        report.cleanup = {
          ...report.cleanup,
          status: 'incomplete',
          errors: [...(report.cleanup.errors ?? []), safe],
        };
      }
    }
  }

  report.cleanup = redactValue(report.cleanup, normalizedConfig.secrets ?? []);
  report.cases = report.cases.map((item) => ({
    ...item,
    evidence: redactValue(item.evidence, normalizedConfig.secrets ?? []),
    requests: redactValue(item.requests, normalizedConfig.secrets ?? []),
  }));
  report.summary = {
    ...report.summary,
    selected: scenarios.length,
    passed: report.cases.filter((item) => item.status === 'PASS').length,
    failed: report.cases.filter((item) => item.status === 'FAIL').length,
    blocked: report.cases.filter((item) => item.status === 'BLOCKED').length,
    skipped: report.cases.filter((item) => item.status === 'SKIPPED').length,
    incomplete: report.cleanup.status !== 'complete' || report.cases.some((item) => item.status === 'BLOCKED' || item.status === 'SKIPPED'),
    reason: report.summary.reason ?? null,
    modelTurns,
    maxModelTurns: normalizedConfig.maxTurns,
  };
  report.run.modelTurns = modelTurns;
  report.run.finishedAt = nowIso(clock);
  report.run = redactValue(report.run, normalizedConfig.secrets ?? []);
  return report;
}
