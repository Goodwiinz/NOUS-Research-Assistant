/**
 * Report construction, redaction, and rendering for the NOUS QA campaign.
 *
 * Reports intentionally contain observations and bounded metadata only. They
 * never contain cookies, request bodies, response bodies, or auth material.
 */

import { chmod, mkdir, writeFile } from 'node:fs/promises';
import { join } from 'node:path';

export const REPORT_SCHEMA_VERSION = 1;
export const CASE_STATUSES = Object.freeze([
  'PASS',
  'FAIL',
  'BLOCKED',
  'SKIPPED',
]);

const SECRET_KEY = /(?:authorization|cookie|set-cookie|password|passwd|secret|token|api[-_]?key|access[-_]?token|refresh[-_]?token|session)/i;
const JWT = /\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b/g;
const BEARER = /\bBearer\s+[A-Za-z0-9._~+/=-]+/gi;
const URL_SECRET = /([?&](?:token|password|secret|api[_-]?key|access[_-]?token|refresh[_-]?token)=)[^&#\s]*/gi;
const CONTROL = /[\u0000-\u0008\u000b\u000c\u000e-\u001f\u007f]/g;
const BROWSER_ACTIONS = new Set(['click', 'fill', 'press', 'setInputFiles', 'waitFor', 'waitForURL', 'goto', 'reload', 'inputValue', 'allInnerTexts', 'unknown']);

function browserErrorSummary(error) {
  const raw = String(error?.message ?? error ?? '');
  const lower = raw.toLowerCase();
  const looksLikeBrowser = error?.name === 'TimeoutError'
    || lower.includes('strict mode violation')
    || lower.includes('call log:')
    || /(?:locator|page)\.[a-z]/i.test(raw);
  if (!looksLikeBrowser) return null;
  const actionMatch = raw.match(/(?:locator|page)\.([A-Za-z]+)/i);
  const action = BROWSER_ACTIONS.has(actionMatch?.[1]) ? actionMatch[1] : 'unknown';
  const category = lower.includes('strict mode violation')
    ? 'strict-match'
    : lower.includes('timeout')
      ? 'timeout'
      : lower.includes('target page, context or browser has been closed')
        ? 'closed'
        : 'browser';
  const timeoutMatch = raw.match(/(\d{1,6})\s*ms/);
  const timeoutMs = timeoutMatch ? Math.min(300_000, Number(timeoutMatch[1])) : null;
  return { category, action, timeoutMs };
}

/** Escape text for insertion into an HTML text node or attribute. */
export function escapeHtml(value) {
  return String(value ?? '')
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;');
}

/**
 * Remove secrets and control characters from a string destined for a report.
 * The supplied secret list is deliberately accepted by value so callers can
 * provide environment-derived credentials without placing them in a report.
 */
export function redactText(value, secrets = []) {
  let text = String(value ?? '').replace(CONTROL, '');
  for (const secret of secrets) {
    if (typeof secret === 'string' && secret.length > 0) {
      text = text.split(secret).join('[REDACTED]');
    }
  }
  return text
    .replace(BEARER, 'Bearer [REDACTED]')
    .replace(JWT, '[REDACTED_TOKEN]')
    .replace(URL_SECRET, '$1[REDACTED]');
}

/**
 * Recursively redact arbitrary evidence while retaining useful scalar shape.
 * Unknown objects are bounded to avoid dumping raw response payloads.
 */
export function redactValue(value, secrets = [], depth = 0) {
  if (depth > 5) return '[TRUNCATED]';
  if (value === null || value === undefined) return value;
  if (typeof value === 'string') return redactText(value, secrets).slice(0, 4000);
  if (typeof value === 'number' || typeof value === 'boolean') return value;
  if (value instanceof Error) {
    return {
      name: redactText(value.name, secrets),
      message: redactText(value.message, secrets).slice(0, 1000),
    };
  }
  if (Array.isArray(value)) {
    return value.slice(0, 50).map((item) => redactValue(item, secrets, depth + 1));
  }
  if (typeof value === 'object') {
    const result = {};
    for (const [key, item] of Object.entries(value).slice(0, 100)) {
      if (SECRET_KEY.test(key)) {
        result[key] = '[REDACTED]';
      } else {
        result[key] = redactValue(item, secrets, depth + 1);
      }
    }
    return result;
  }
  return redactText(String(value), secrets);
}

export function sanitizeError(error, secrets = []) {
  const message = error instanceof Error ? error.message : String(error);
  const name = error instanceof Error ? error.name : 'Error';
  const browser = browserErrorSummary(error);
  if (browser) {
    return {
      name: 'BrowserError',
      message: `Browser ${browser.category} during ${browser.action}${browser.timeoutMs === null ? '' : ` (${browser.timeoutMs}ms timeout)`}`,
      category: browser.category,
      action: browser.action,
      timeoutMs: browser.timeoutMs,
    };
  }
  return {
    name: redactText(name, secrets).slice(0, 160),
    message: redactText(message, secrets).slice(0, 1000),
  };
}

/** Keep request evidence to method, path and status; never retain headers/body. */
export function sanitizeRequestObservation(observation, secrets = []) {
  const rawPath = observation?.path ?? observation?.url ?? '';
  let path = redactText(rawPath, secrets);
  try {
    const parsed = new URL(path, 'http://nous-qa.invalid');
    const query = new URLSearchParams();
    for (const [key, value] of parsed.searchParams.entries()) {
      query.set(key, SECRET_KEY.test(key) ? '[REDACTED]' : redactText(value, secrets));
    }
    path = `${parsed.pathname}${query.toString() ? `?${query}` : ''}`;
  } catch {
    path = redactText(path, secrets).slice(0, 500);
  }
  return {
    method: String(observation?.method ?? 'GET').toUpperCase(),
    path: path.slice(0, 500),
    status: Number.isFinite(observation?.status) ? observation.status : null,
    durationMs: Number.isFinite(observation?.durationMs)
      ? Math.max(0, Math.round(observation.durationMs))
      : null,
  };
}

export function configForReport(config = {}) {
  const output = {
    suite: config.suite,
    selectedIds: Array.isArray(config.selectedIds) ? [...config.selectedIds] : [],
    baseUrl: config.baseUrl,
    apiUrl: config.apiUrl,
    allowWrites: Boolean(config.allowWrites),
    timeoutMs: config.timeoutMs,
    maxTurns: config.maxTurns,
    expectedBackendSha: config.expectedBackendSha ?? null,
    deploymentEvidence: config.deploymentEvidence
      ? {
          targetUrl: config.deploymentEvidence.targetUrl,
          backendSha: config.deploymentEvidence.backendSha,
          frontendSha: config.deploymentEvidence.frontendSha ?? null,
          observedAt: config.deploymentEvidence.observedAt,
          provenance: config.deploymentEvidence.provenance,
        }
      : null,
    storageState: config.storageState ? '[provided]' : null,
    credentialsConfigured: Boolean(config.credentials?.email && config.credentials?.password),
  };
  return output;
}

export function renderHtml(report) {
  const safeReport = redactValue(report, report?.run?.secrets ?? []);
  const serialized = JSON.stringify(safeReport, null, 2);
  const cases = Array.isArray(safeReport?.cases) ? safeReport.cases : [];
  const rows = cases
    .map(
      (item) =>
        `<tr><td>${escapeHtml(item.id)}</td><td>${escapeHtml(item.title)}</td><td class="${escapeHtml(String(item.status).toLowerCase())}">${escapeHtml(item.status)}</td><td>${escapeHtml(item.reason ?? '')}</td><td>${escapeHtml(item.durationMs ?? '')}</td></tr>`
    )
    .join('');
  return `<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>NOUS QA report</title>
<style>body{font:14px system-ui,sans-serif;margin:2rem;color:#202020}table{border-collapse:collapse;width:100%}th,td{border:1px solid #ccc;padding:.5rem;text-align:left;vertical-align:top}.pass{color:#075e2a}.fail{color:#a40000}.blocked,.skipped{color:#805500}pre{white-space:pre-wrap;overflow:auto;background:#f6f6f6;padding:1rem}</style></head>
<body><h1>NOUS QA report</h1><table><thead><tr><th>Id</th><th>Scenario</th><th>Status</th><th>Reason</th><th>Duration (ms)</th></tr></thead><tbody>${rows}</tbody></table><h2>Machine-readable report</h2><pre>${escapeHtml(serialized)}</pre></body></html>`;
}

export async function writeReports(report, outputDir) {
  await mkdir(outputDir, { recursive: true, mode: 0o700 });
  // chmod after mkdir also handles an existing directory whose mode was more
  // permissive; report files contain campaign metadata and retained IDs.
  await chmod(outputDir, 0o700);
  const jsonPath = join(outputDir, 'nous-qa-report.json');
  const htmlPath = join(outputDir, 'nous-qa-report.html');
  const json = `${JSON.stringify(report, null, 2)}\n`;
  await writeFile(jsonPath, json, { encoding: 'utf8', mode: 0o600 });
  await writeFile(htmlPath, renderHtml(report), { encoding: 'utf8', mode: 0o600 });
  await chmod(jsonPath, 0o600);
  await chmod(htmlPath, 0o600);
  return { jsonPath, htmlPath };
}
