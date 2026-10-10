// Committed evidence README for nous-verify (docs/engineering/verification.md,
// "Evidence record"). Input is the already-redacted campaign report; binaries
// stay in the gitignored evidence directory and are listed by name only.
import { mkdir, writeFile } from 'node:fs/promises';
import { basename, dirname, isAbsolute, join, relative, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

const REPO_ROOT = resolve(dirname(fileURLToPath(import.meta.url)), '../../..');

const STATUS_LABEL = { PASS: 'PASS', FAIL: 'FAILED', BLOCKED: 'BLOCKED (NOT RUN)', SKIPPED: 'SKIPPED (NOT RUN)' };

function overall(report) {
  const summary = report.summary ?? {};
  if (summary.failed > 0) return 'FAILED';
  if (summary.blocked > 0 || summary.skipped > 0 || summary.incomplete || !summary.selected) return 'BLOCKED';
  if (report.cleanup?.status !== 'complete') return 'BLOCKED';
  return 'PASS (assertions); visual confirmation pending';
}

function cell(value) {
  return String(value ?? '').replace(/\|/g, '\\|').replace(/\r?\n/g, ' ');
}

function code(value) {
  return `\`${String(value ?? 'unknown').replace(/`/g, "'").replace(/\r?\n/g, ' ')}\``;
}

/** Repo-relative when the directory is under the repository; never a home path otherwise. */
function displayDir(dir) {
  if (!dir) return 'none';
  const rel = relative(REPO_ROOT, dir);
  if (rel && !rel.startsWith('..') && !isAbsolute(rel)) return rel;
  return `<outside the worktree>/${basename(dir)}`;
}

export function renderEvidenceReadme(report) {
  const run = report.run ?? {};
  const cases = report.cases ?? [];
  const features = run.configuration?.features?.length ? run.configuration.features.join(', ') : 'selected scenarios';
  const date = String(run.startedAt ?? '').slice(0, 10);
  const identity = run.observedIdentity?.backendSha
    ? `${code(run.observedIdentity.backendSha)} (${run.observedIdentity.provenance ?? 'unknown provenance'})`
    : 'not exposed';
  const videos = (run.artifacts?.videos ?? []).map((path) => basename(String(path)));
  const checkpointRows = cases.flatMap((item) => (item.checkpoints ?? []).map((checkpoint) =>
    `| ${cell(item.id)} | ${cell(checkpoint.name)} | ${cell(checkpoint.file)} |`));
  const lines = [
    `# Verification evidence: ${features} (${date})`,
    '',
    `Source: ${code(run.localSource?.sha)} (${run.localSource?.dirty ?? 'unknown'}; ${run.localSource?.provenance ?? 'unknown'}).`,
    `Target: frontend ${code(run.target)}, API ${code(run.configuration?.apiUrl)}.`,
    `Backend identity: ${identity}.`,
    `Command: ${code(run.command)}. Run id ${code(run.id)}, ${run.startedAt} to ${run.finishedAt}.`,
    `Binaries (not committed): ${code(displayDir(run.evidenceDir))} with ${run.artifacts?.checkpointCount ?? 0} checkpoint PNG(s), `
      + `${videos.length} video(s)${videos.length ? ` (${videos.map(code).join(', ')})` : ''}, `
      + `trace(s) ${(run.artifacts?.traces ?? []).length ? run.artifacts.traces.map(code).join(', ') : code('none')} `
      + '(secret-bearing: session cookies and tokens; never attach, commit or upload).',
    '',
    `Overall: ${overall(report)}. A PASS here is assertion-level only until the checkpoints below were viewed and the feature pass criteria confirmed by the operator (record that in the PR).`,
    '',
    '| Scenario | Result | Reason |',
    '| --- | --- | --- |',
    ...cases.map((item) => `| ${cell(item.id)} | ${STATUS_LABEL[item.status] ?? cell(item.status)} | ${cell(item.reason ?? '')} |`),
    '',
    '## Checkpoints',
    '',
    ...(checkpointRows.length
      ? ['| Scenario | Checkpoint | File |', '| --- | --- | --- |', ...checkpointRows]
      : ['No checkpoint was taken.']),
    '',
    `Cleanup: ${report.cleanup?.status ?? 'unknown'}${report.cleanup?.retained?.length ? ` (retained ${report.cleanup.retained.length})` : ''}.`,
    '',
  ];
  return lines.join('\n');
}

export async function writeEvidenceRecord(report, dir) {
  await mkdir(dir, { recursive: true });
  const path = join(dir, 'README.md');
  await writeFile(path, renderEvidenceReadme(report), 'utf8');
  return path;
}
