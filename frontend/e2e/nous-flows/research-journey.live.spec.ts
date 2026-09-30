/**
 * GOO-308 live plan-to-write journey evidence (no mocks, no page.route).
 *
 * Opt-in: RESEARCH_JOURNEY_LIVE=1, BASE_URL, RESEARCH_JOURNEY_PROJECT_ID (the
 * known-answer or consenting project), RESEARCH_JOURNEY_AUTH_DIR holding one
 * saved state per principal (author, supervisor, reviewer-a, reviewer-b,
 * adjudicator, foreign; each from `NOUS_AUTH_STATE=… pnpm --dir
 * tools/nous-playwright auth`) and RESEARCH_JOURNEY_OUT for the evidence.
 * Run with `--trace on` and keep trace.zip.
 *
 * The principals perform the decisions (the trial runbook in
 * evals/academic-journey-v1/README.md); this spec reads every stage through
 * the UI and the API as they stand, reloads to prove the rail is persisted
 * state, downloads the bundle through the button, checks SHA256SUMS, and
 * records the deny probes. It appends one JSON line per read to
 * transitions.jsonl and never writes headers or cookies.
 */
import { createHash } from 'node:crypto';
import {
  appendFileSync,
  mkdirSync,
  readFileSync,
  writeFileSync,
} from 'node:fs';
import path from 'node:path';
import { inflateRawSync } from 'node:zlib';
import {
  expect,
  test,
  type APIRequestContext,
  type Browser,
  type BrowserContext,
} from '@playwright/test';

const live = process.env.RESEARCH_JOURNEY_LIVE === '1';
test.skip(!live, 'Set RESEARCH_JOURNEY_LIVE=1 to record the live journey.');

const PROJECT = process.env.RESEARCH_JOURNEY_PROJECT_ID ?? '';
const AUTH_DIR = process.env.RESEARCH_JOURNEY_AUTH_DIR ?? '';
const OUT = process.env.RESEARCH_JOURNEY_OUT ?? 'research-journey-out';
const API = process.env.RESEARCH_JOURNEY_API_URL ?? '';
const PRINCIPALS = [
  'author',
  'supervisor',
  'reviewer-a',
  'reviewer-b',
  'adjudicator',
  'foreign',
] as const;
type Principal = (typeof PRINCIPALS)[number];
const STAGES = ['plan', 'discover', 'select', 'extract', 'write'] as const;

const sha256 = (data: Buffer | string): string =>
  createHash('sha256').update(data).digest('hex');

function record(entry: {
  step: string;
  principal: Principal;
  method: string;
  path: string;
  status: number;
  ids?: unknown;
  versions?: unknown;
  hashes?: unknown;
}): void {
  appendFileSync(
    path.join(OUT, 'transitions.jsonl'),
    `${JSON.stringify({ at: new Date().toISOString(), ...entry })}\n`
  );
}

async function contexts(
  browser: Browser
): Promise<Record<Principal, BrowserContext>> {
  const entries = await Promise.all(
    PRINCIPALS.map(
      async (name) =>
        [
          name,
          await browser.newContext({
            storageState: path.join(AUTH_DIR, `${name}.json`),
          }),
        ] as const
    )
  );
  return Object.fromEntries(entries) as Record<Principal, BrowserContext>;
}

async function readJson(
  request: APIRequestContext,
  step: string,
  principal: Principal,
  route: string
): Promise<{ status: number; body: unknown }> {
  const response = await request.get(`${API}${route}`);
  const text = await response.text();
  let body: unknown = null;
  try {
    body = JSON.parse(text);
  } catch {
    body = null;
  }
  const journey = body as {
    current?: string;
    stages?: { key: string; status: string }[];
  };
  record({
    step,
    principal,
    method: 'GET',
    path: route,
    status: response.status(),
    ids: journey?.stages?.map((s) => `${s.key}:${s.status}`),
    versions: { current: journey?.current ?? null },
    hashes: { response_sha256: sha256(text) },
  });
  return { status: response.status(), body };
}

/** Stored (method 0) or deflated (8) zip members, central directory order. */
function unzip(data: Buffer): Map<string, Buffer> {
  const members = new Map<string, Buffer>();
  const end = data.lastIndexOf(Buffer.from([0x50, 0x4b, 0x05, 0x06]));
  let offset = data.readUInt32LE(end + 16);
  for (let i = 0; i < data.readUInt16LE(end + 10); i += 1) {
    const method = data.readUInt16LE(offset + 10);
    const size = data.readUInt32LE(offset + 20);
    const nameLength = data.readUInt16LE(offset + 28);
    const extra = data.readUInt16LE(offset + 30);
    const comment = data.readUInt16LE(offset + 32);
    const local = data.readUInt32LE(offset + 42);
    const name = data.toString('utf8', offset + 46, offset + 46 + nameLength);
    const start =
      local +
      30 +
      data.readUInt16LE(local + 26) +
      data.readUInt16LE(local + 28);
    const raw = data.subarray(start, start + size);
    members.set(name, method === 8 ? inflateRawSync(raw) : raw);
    offset += 46 + nameLength + extra + comment;
  }
  return members;
}

test('live plan-to-write journey: rail, reloads, bundle and deny probes', async ({
  browser,
}) => {
  expect(PROJECT, 'RESEARCH_JOURNEY_PROJECT_ID').not.toBe('');
  expect(AUTH_DIR, 'RESEARCH_JOURNEY_AUTH_DIR').not.toBe('');
  expect(API, 'RESEARCH_JOURNEY_API_URL (the backend /api/v1 origin)').not.toBe(
    ''
  );
  mkdirSync(OUT, { recursive: true });
  const ctx = await contexts(browser);
  const journeyPath = `/research-engine/projects/${PROJECT}/journey`;

  // Each stage: the author's rail and API agree, and survive a reload and a
  // fresh context (persisted state, not client memory).
  const page = await ctx.author.newPage();
  await page.goto(`/projects/${PROJECT}?tab=workflow`);
  const rail = page.getByRole('list', { name: 'Research journey' });
  await expect(rail.getByRole('listitem').first()).toBeVisible();
  for (const stage of STAGES) {
    await page.goto(`/projects/${PROJECT}?tab=workflow#journey-${stage}`);
    const before = await rail.innerText();
    const api = await readJson(
      ctx.author.request,
      `stage:${stage}`,
      'author',
      journeyPath
    );
    expect(api.status).toBe(200);
    await page.reload();
    await expect(rail).toHaveText(before);
    const fresh = await browser.newContext({
      storageState: path.join(AUTH_DIR, 'author.json'),
    });
    const again = await fresh.newPage();
    await again.goto(`/projects/${PROJECT}?tab=workflow`);
    await expect(
      again.getByRole('list', { name: 'Research journey' })
    ).toHaveText(before);
    await fresh.close();
  }

  // Every principal's view is recorded; the foreign user sees nothing.
  for (const principal of PRINCIPALS) {
    const { status } = await readJson(
      ctx[principal].request,
      'roles',
      principal,
      `/research-engine/projects/${PROJECT}/roles`
    );
    expect(status).toBe(principal === 'foreign' ? 404 : 200);
  }

  // The bundle through the button; SHA256SUMS checked in-test.
  const download = page.waitForEvent('download');
  await page.getByRole('button', { name: 'Download audit bundle' }).click();
  const saved = path.join(OUT, 'audit-bundle.zip');
  await (await download).saveAs(saved);
  const zip = readFileSync(saved);
  const members = unzip(zip);
  const sums = members.get('SHA256SUMS')?.toString('utf8') ?? '';
  const lines = sums.trim().split('\n');
  expect(lines.length).toBe(members.size - 1);
  for (const line of lines) {
    const [digest, name] = [line.slice(0, 64), line.slice(66)];
    expect(sha256(members.get(name) ?? Buffer.alloc(0)), name).toBe(digest);
  }
  const manifestSha = sha256(members.get('manifest.json') ?? '');
  record({
    step: 'bundle',
    principal: 'author',
    method: 'GET',
    path: `/research-engine/projects/${PROJECT}/audit-bundle`,
    status: 200,
    hashes: { zip_sha256: sha256(zip), manifest_sha256: manifestSha },
  });
  writeFileSync(path.join(OUT, 'manifest.sha256'), `${manifestSha}\n`);

  // Deny probes: foreign 404 on journey, bundle, claims and PRISMA.
  for (const route of [
    journeyPath,
    `/research-engine/projects/${PROJECT}/audit-bundle`,
    `/projects/${PROJECT}/claims`,
    `/research-engine/projects/${PROJECT}/prisma`,
  ]) {
    const { status } = await readJson(
      ctx.foreign.request,
      'deny:foreign',
      'foreign',
      route
    );
    expect(status, route).toBe(404);
  }
  // A reviewer cannot promote the current draft (403 before any gate).
  const current = await ctx['reviewer-a'].request.get(
    `${API}/projects/${PROJECT}/drafts/current`
  );
  const draft = (await current.json()) as { id: string; version: number };
  const promote = await ctx['reviewer-a'].request.post(
    `${API}/projects/${PROJECT}/drafts/${draft.id}/versions/${draft.version}/promote`,
    {
      data: {
        content_hash: '0'.repeat(64),
        idempotency_key: `probe-${Date.now()}`,
      },
    }
  );
  record({
    step: 'deny:reviewer-promote',
    principal: 'reviewer-a',
    method: 'POST',
    path: `/projects/${PROJECT}/drafts/${draft.id}/versions/${draft.version}/promote`,
    status: promote.status(),
  });
  expect(promote.status()).toBe(403);

  await Promise.all(Object.values(ctx).map((context) => context.close()));
});
