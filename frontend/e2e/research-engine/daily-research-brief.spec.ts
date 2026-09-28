import { expect, test, type Browser, type Page } from '@playwright/test';
import { spawn, type ChildProcessWithoutNullStreams } from 'node:child_process';
import path from 'node:path';

const fixturePort = Number(process.env.DAILY_BRIEF_E2E_PORT ?? '8765');
const fixtureOrigin = `http://127.0.0.1:${fixturePort}`;
// Captured before freezing on 2026-09-28: 11 cold reloads, p50 656.100 ms,
// p95 832.700 ms. The fixed gate is the measured p95 plus 20%.
const REVIEW_PAGE_P95_THRESHOLD_MS = 999.24;
let fixture: ChildProcessWithoutNullStreams;
let fixtureLog = '';

type AuditStep = {
  step_index: number;
  step_type: string;
  outputs_hash: string;
  canonical_hash: string;
  output: Record<string, unknown>;
};

type RunAudit = {
  status: string;
  manifest: Record<string, unknown>;
  steps: AuditStep[];
  reviews: Array<{
    id: string;
    step_index: number;
    review_kind: string;
    output_hash: string;
    decision: string;
    decision_payload: Record<string, unknown>;
  }>;
  reconnect_failures: number;
};

type AcceptedReview = {
  id: string;
  replay: boolean;
};

async function waitForFixture(): Promise<void> {
  const deadline = Date.now() + 30_000;
  while (Date.now() < deadline) {
    if (fixture.exitCode !== null) {
      throw new Error(`Daily Brief fixture exited early:\n${fixtureLog}`);
    }
    try {
      const response = await fetch(`${fixtureOrigin}/health`);
      if (response.ok) return;
    } catch {
      // Uvicorn has not bound the port yet.
    }
    await new Promise((resolve) => setTimeout(resolve, 100));
  }
  throw new Error(
    `Daily Brief fixture server did not become ready:\n${fixtureLog}`
  );
}

async function login(page: Page, email = 'owner@example.test'): Promise<void> {
  await page.goto('/login');
  await page.getByTestId('email-input').fill(email);
  await page.getByTestId('password-input').fill('controlled-password');
  await page.getByTestId('login-button').click();
  await expect(page).toHaveURL(/\/dashboard/);
}

async function startDailyBrief(
  page: Page,
  question: string,
  projectSuffix: string,
  options: { providerLabels?: string[]; resultsPerSource?: number } = {}
): Promise<string> {
  const projectName = `Daily Brief ${projectSuffix}`;
  await page.goto('/research-engine');
  await expect(
    page.getByRole('heading', { name: 'Research engine' })
  ).toBeVisible();
  await page.getByRole('button', { name: 'New project' }).first().click();
  await page
    .getByPlaceholder('e.g., Transformer Architecture Survey')
    .fill(projectName);
  await page
    .getByPlaceholder('Brief description of the research project')
    .fill('Controlled PostgreSQL browser lifecycle');
  await page.getByRole('button', { name: 'Create Project' }).click();
  await page.getByRole('heading', { name: projectName }).click();

  await expect(
    page.getByRole('heading', { name: 'Choose a blueprint template' })
  ).toBeVisible();
  await page
    .getByRole('button')
    .filter({ hasText: 'Daily Research Brief' })
    .click();
  await expect(page.getByLabel('Research question')).toBeVisible();
  await page.getByLabel('Research question').fill(question);
  await page
    .getByLabel('What to include')
    .fill('Controlled treatment outcomes\nPeer-reviewed evidence');
  await page
    .getByLabel('What to exclude')
    .fill('Unrelated outcomes\nNon-research commentary');
  if (options.providerLabels) {
    await page.getByRole('button', { name: /^Sources \(/ }).click();
    for (const label of options.providerLabels) {
      const checkbox = page.getByRole('checkbox', { name: label });
      if (!(await checkbox.isChecked())) await checkbox.click();
    }
    await page.keyboard.press('Escape');
    await expect(
      page.getByRole('button', {
        name: `Sources (${options.providerLabels.length})`,
      })
    ).toBeVisible();
  }
  await page
    .getByLabel('Results per source')
    .fill(String(options.resultsPerSource ?? 2));
  await page.getByLabel('Notes').fill('Playwright lifecycle certification');
  await page.getByRole('checkbox', { name: 'Confirm scope' }).click();

  await page.getByRole('button', { name: 'Save' }).click();
  const start = page.getByRole('button', { name: 'Start run' });
  await expect(start).toBeEnabled();
  await start.click();
  await expect(page).toHaveURL(/\/research-engine\/runs\/[0-9a-f-]+$/);
  return page.url().split('/').at(-1) as string;
}

async function selectEveryDecision(
  page: Page,
  decision: string
): Promise<void> {
  const choices = page.getByRole('radio', { name: decision, exact: true });
  const count = await choices.count();
  expect(count).toBeGreaterThan(0);
  for (let index = 0; index < count; index += 1) {
    await choices.nth(index).click();
  }
}

async function approveAndResume(
  page: Page,
  reviewKind: 'screening' | 'extraction' | 'final'
): Promise<void> {
  const reviewResponse = page.waitForResponse(
    (response) =>
      response.url().includes('/reviews/') &&
      response.request().method() === 'POST'
  );
  await page
    .getByRole('button', { name: `Approve ${reviewKind} review` })
    .click();
  expect((await reviewResponse).status()).toBe(200);
  await expect(
    page.getByRole('button', { name: 'Resume approved run' })
  ).toBeVisible();

  // A hard reload proves that the accepted review comes from PostgreSQL.
  await page.reload();
  await expect(
    page.getByRole('button', { name: 'Resume approved run' })
  ).toBeVisible();
  await page.getByRole('button', { name: 'Resume approved run' }).click();
}

async function advanceThroughExtraction(page: Page): Promise<void> {
  await expect(
    page.getByRole('heading', { name: 'Screening review' })
  ).toBeVisible();
  await selectEveryDecision(page, 'include');
  await approveAndResume(page, 'screening');

  await expect(
    page.getByRole('heading', { name: 'Extraction review' })
  ).toBeVisible();
  await selectEveryDecision(page, 'accept');
  await approveAndResume(page, 'extraction');
}

async function assertDeniedRun(
  browser: Browser,
  runId: string,
  email: string
): Promise<void> {
  const context = await browser.newContext({
    baseURL: 'http://localhost:3000',
  });
  const page = await context.newPage();
  try {
    await login(page, email);
    const denied = page.waitForResponse(
      (response) =>
        response.url().includes(`/api/v1/research-engine/runs/${runId}`) &&
        !response.url().includes('/stream') &&
        response.request().method() === 'GET'
    );
    await page.goto(`/research-engine/runs/${runId}`);
    expect((await denied).status()).toBe(404);
    await expect(
      page.getByRole('alert').filter({ hasText: 'The run could not be loaded' })
    ).toBeVisible();
  } finally {
    await context.close();
  }
}

async function auditRun(page: Page, runId: string): Promise<RunAudit> {
  return page.evaluate(async (id) => {
    const response = await fetch(`/api/v1/e2e/runs/${id}/audit`);
    if (!response.ok) throw new Error(`audit failed: ${response.status}`);
    return response.json() as Promise<RunAudit>;
  }, runId);
}

test.describe.serial('Daily Research Brief real lifecycle', () => {
  test.describe.configure({ timeout: 120_000 });

  test.beforeAll(async () => {
    const backend = path.resolve(process.cwd(), '..', 'backend');
    fixture = spawn(
      path.resolve(process.cwd(), '..', '.venv', 'bin', 'python'),
      [
        '-m',
        'uvicorn',
        'tests.integration.test_daily_research_brief_postgres:create_e2e_app',
        '--factory',
        '--host',
        '127.0.0.1',
        '--port',
        String(fixturePort),
      ],
      {
        cwd: backend,
        env: {
          ...process.env,
          PYTHONPATH: '.',
          ORCHESTRATION_TEST_DATABASE_URL:
            process.env.ORCHESTRATION_TEST_DATABASE_URL ?? '',
        },
        stdio: 'pipe',
      }
    );
    fixture.stdout.on('data', (chunk: Buffer) => {
      fixtureLog += chunk.toString();
    });
    fixture.stderr.on('data', (chunk: Buffer) => {
      fixtureLog += chunk.toString();
    });
    await waitForFixture();
  });

  test.afterAll(async () => {
    if (!fixture || fixture.killed) return;
    fixture.kill('SIGTERM');
    await new Promise<void>((resolve) => {
      fixture.once('exit', () => resolve());
      setTimeout(resolve, 5_000);
    });
  });

  test.afterEach(async ({}, testInfo) => {
    if (testInfo.status === testInfo.expectedStatus) return;
    await testInfo.attach('daily-brief-fixture.log', {
      body: Buffer.from(fixtureLog),
      contentType: 'text/plain',
    });
  });

  test('reloads all three gates and persists exact artifacts and downloads', async ({
    page,
  }) => {
    await login(page);
    const runId = await startDailyBrief(
      page,
      'What are the controlled treatment outcomes?',
      'verified lifecycle'
    );

    await expect(
      page.getByRole('heading', { name: 'Screening review' })
    ).toBeVisible();
    await page.reload();
    await expect(
      page.getByRole('heading', { name: 'Screening review' })
    ).toBeVisible();
    await selectEveryDecision(page, 'include');

    await approveAndResume(page, 'screening');

    await expect(
      page.getByRole('heading', { name: 'Extraction review' })
    ).toBeVisible();
    await page.reload();
    await expect(
      page.getByRole('heading', { name: 'Extraction review' })
    ).toBeVisible();
    const extractionRows = page.locator('fieldset');
    expect(await extractionRows.count()).toBe(2);
    await extractionRows
      .nth(0)
      .getByRole('radio', { name: 'accept', exact: true })
      .click();
    await extractionRows
      .nth(1)
      .getByRole('radio', { name: 'reject', exact: true })
      .click();
    await extractionRows
      .nth(1)
      .getByLabel('Reason for rejection')
      .fill('Controlled mixed extraction decision');
    await approveAndResume(page, 'extraction');

    await expect(
      page.getByRole('heading', { name: 'Final review' })
    ).toBeVisible();
    await page.reload();
    await expect(
      page.getByRole('heading', { name: 'Final review' })
    ).toBeVisible();
    await approveAndResume(page, 'final');

    await expect(
      page.getByRole('heading', { name: 'Verified brief' })
    ).toBeVisible();
    const audit = await auditRun(page, runId);
    expect(audit.status).toBe('completed');
    expect(audit.steps.map((step) => step.step_type)).toEqual([
      'search',
      'screen',
      'extract',
      'synthesize',
      'verify',
      'export',
    ]);
    expect(new Set(audit.steps.map((step) => step.step_index)).size).toBe(6);
    for (const step of audit.steps) {
      expect(step.outputs_hash).toBe(step.canonical_hash);
    }
    expect(audit.reviews.map((review) => review.review_kind)).toEqual([
      'screening',
      'extraction',
      'final',
    ]);
    expect(new Set(audit.reviews.map((review) => review.id)).size).toBe(3);
    expect(audit.reviews.map((review) => review.output_hash)).toEqual([
      audit.steps[1].outputs_hash,
      audit.steps[2].outputs_hash,
      audit.steps[5].outputs_hash,
    ]);
    expect(audit.reconnect_failures).toBe(0);

    const extraction = audit.steps[2].output.extractions as Array<{
      evidence: Array<{ evidence_id: string; quote: string }>;
    }>;
    const evidenceId = extraction[0].evidence[0].evidence_id;
    expect(extraction[0].evidence[0].quote).toBe(
      'Treatment reduced the measured score'
    );
    const synthesis = audit.steps[3].output.synthesis as {
      claims: Array<{ evidence: Array<{ evidence_id: string }> }>;
    };
    expect(synthesis.claims[0].evidence[0].evidence_id).toBe(evidenceId);
    const verification = audit.steps[4].output.verification as {
      claims: Array<{ evidence_ids: string[] }>;
    };
    expect(verification.claims[0].evidence_ids).toContain(evidenceId);
    const exported = audit.steps[5].output;
    expect(exported.verification_output_hash).toBe(audit.steps[4].outputs_hash);
    expect(exported.report_hash).toMatch(/^[0-9a-f]{64}$/);
    expect(exported.markdown).toContain('Treatment reduced the measured score');

    // The real authenticated download must cross the same browser/API boundary.
    for (const format of ['markdown', 'json', 'csv'] as const) {
      const downloadResponse = page.waitForResponse(
        (response) =>
          response.url().includes(`/runs/${runId}/export?format=${format}`) &&
          response.request().method() === 'GET'
      );
      await page
        .getByRole('button', {
          name: `Download ${format === 'markdown' ? 'Markdown' : format.toUpperCase()}`,
        })
        .click();
      expect((await downloadResponse).status()).toBe(200);
    }
  });

  test('retains successful evidence and coverage after a provider partial failure', async ({
    page,
  }) => {
    await login(page);
    const runId = await startDailyBrief(
      page,
      'What are the controlled treatment outcomes? [partial]',
      'partial provider'
    );

    await advanceThroughExtraction(page);
    await expect(
      page.getByRole('heading', { name: 'Verification failed' })
    ).toBeVisible();
    await page
      .getByRole('button', { name: 'Continue with an unverified draft' })
      .click();
    await expect(
      page.getByRole('heading', { name: 'Unverified draft' })
    ).toBeVisible();
    await expect(page.getByText('1 of 2 providers succeeded')).toBeVisible();

    const audit = await auditRun(page, runId);
    const search = audit.steps[0].output;
    const coverage = search.coverage as {
      partial: boolean;
      providers: Record<string, { status: string; error_type?: string }>;
      deduplication: { before: number; after: number };
    };
    expect(coverage.partial).toBe(true);
    expect(coverage.providers.crossref).toEqual({
      status: 'failed',
      error_type: 'TimeoutError',
    });
    expect(coverage.deduplication).toEqual({ before: 2, after: 2 });
    expect(search.source_records).toHaveLength(2);
  });

  test('renders a durable no-evidence result and keeps audit downloads available', async ({
    page,
  }) => {
    await login(page);
    const runId = await startDailyBrief(
      page,
      'What are the controlled treatment outcomes? [no-evidence]',
      'no evidence'
    );

    await expect(
      page.getByRole('heading', { name: 'No evidence brief' })
    ).toBeVisible();
    const audit = await auditRun(page, runId);
    expect(audit.status).toBe('completed');
    expect(audit.manifest.final_status).toBe('no_evidence');
    expect(audit.steps.map((step) => step.step_type)).toEqual([
      'search',
      'screen',
    ]);
    expect(audit.reviews).toEqual([]);

    for (const [name, format] of [
      ['Download audit JSON', 'json'],
      ['Download extraction CSV', 'csv'],
    ] as const) {
      const response = page.waitForResponse(
        (candidate) =>
          candidate.url().includes(`/runs/${runId}/export?format=${format}`) &&
          candidate.request().method() === 'GET'
      );
      await page.getByRole('button', { name }).click();
      expect((await response).status()).toBe(200);
    }
  });

  test('stops at a persisted verification failure without offering final approval', async ({
    page,
  }) => {
    await login(page);
    const runId = await startDailyBrief(
      page,
      'What are the controlled treatment outcomes? [unverified]',
      'verification failure'
    );
    await advanceThroughExtraction(page);

    await expect(
      page.getByRole('heading', { name: 'Verification failed' })
    ).toBeVisible();
    await expect(
      page.getByRole('button', { name: 'Continue with an unverified draft' })
    ).toBeVisible();
    await expect(
      page.getByRole('heading', { name: 'Final review' })
    ).toHaveCount(0);

    const audit = await auditRun(page, runId);
    expect(audit.status).toBe('paused');
    expect(audit.steps.map((step) => step.step_type)).toEqual([
      'search',
      'screen',
      'extract',
      'synthesize',
      'verify',
    ]);
    expect(audit.reviews.map((review) => review.review_kind)).toEqual([
      'screening',
      'extraction',
    ]);
  });

  test('binds an explicit override to the failed verification hash and labels every result unverified', async ({
    page,
  }) => {
    await login(page);
    const runId = await startDailyBrief(
      page,
      'What are the controlled treatment outcomes? [unverified]',
      'unverified override'
    );
    await advanceThroughExtraction(page);
    await expect(
      page.getByRole('heading', { name: 'Verification failed' })
    ).toBeVisible();

    // The failed hash and action must survive a cold browser reconstruction.
    await page.reload();
    const override = page.getByRole('button', {
      name: 'Continue with an unverified draft',
    });
    await expect(override).toBeEnabled();
    const resumeResponse = page.waitForResponse(
      (response) =>
        response.url().endsWith(`/research-engine/runs/${runId}/resume`) &&
        response.request().method() === 'POST'
    );
    await override.click();
    expect((await resumeResponse).status()).toBe(200);

    await expect(
      page.getByRole('heading', { name: 'Unverified draft' })
    ).toBeVisible();
    await expect(
      page.getByRole('button', { name: 'Download unverified Markdown' })
    ).toBeVisible();
    const audit = await auditRun(page, runId);
    expect(audit.status).toBe('completed');
    expect(audit.manifest.final_status).toBe('unverified');
    const verificationOverride = audit.manifest.verification_override as {
      actor_id: string;
      output_hash: string;
      step_index: number;
    };
    expect(verificationOverride.actor_id).toMatch(/^[0-9a-f-]{36}$/);
    expect(verificationOverride.output_hash).toBe(audit.steps[4].outputs_hash);
    expect(verificationOverride.step_index).toBe(4);
    expect(audit.reviews.map((review) => review.review_kind)).toEqual([
      'screening',
      'extraction',
    ]);
    expect(audit.steps[5].output.report_hash).toMatch(/^[0-9a-f]{64}$/);
  });

  test('reconnects once to the same PostgreSQL run without duplicating its screening gate', async ({
    page,
  }) => {
    await login(page);
    const runId = await startDailyBrief(
      page,
      'What are the controlled treatment outcomes? [reconnect]',
      'reconnect'
    );

    await expect(
      page.getByRole('heading', { name: 'Screening review' })
    ).toBeVisible();
    const audit = await auditRun(page, runId);
    expect(audit.reconnect_failures).toBe(1);
    expect(audit.steps.map((step) => step.step_type)).toEqual([
      'search',
      'screen',
    ]);
    expect(new Set(audit.steps.map((step) => step.step_index)).size).toBe(2);
  });

  test('renders the 200-candidate review page within its measured p95 threshold', async ({
    page,
  }) => {
    await login(page);
    const runId = await startDailyBrief(
      page,
      'What are the controlled treatment outcomes? [max-bound]',
      'maximum review render',
      {
        providerLabels: ['OpenAlex', 'Crossref', 'arXiv', 'PubMed'],
        resultsPerSource: 50,
      }
    );

    await expect(
      page.getByRole('heading', { name: 'Screening review' })
    ).toBeVisible();
    await expect(page.locator('fieldset')).toHaveCount(200);

    const renderSamples: number[] = [];
    for (let sample = 0; sample < 11; sample += 1) {
      await page.reload();
      await expect(
        page.getByRole('heading', { name: 'Screening review' })
      ).toBeVisible();
      await expect(page.locator('fieldset')).toHaveCount(200);
      renderSamples.push(
        await page.evaluate(async () => {
          await new Promise<void>((resolve) => {
            requestAnimationFrame(() => requestAnimationFrame(() => resolve()));
          });
          const entries = performance
            .getEntriesByType('resource')
            .filter((entry) => entry.name.includes('/reviews/pending'));
          const pendingResponse = entries.at(-1);
          if (!pendingResponse) {
            throw new Error('pending review resource timing was not recorded');
          }
          return performance.now() - pendingResponse.responseEnd;
        })
      );
    }
    const ordered = [...renderSamples].sort((left, right) => left - right);
    const p50 = ordered[Math.floor(ordered.length * 0.5)];
    const p95 = ordered[Math.ceil(ordered.length * 0.95) - 1];
    console.log(
      `DAILY_BRIEF_REVIEW_PAGE=${JSON.stringify({
        candidateRows: 200,
        samples: renderSamples.length,
        p50Ms: Number(p50.toFixed(3)),
        p95Ms: Number(p95.toFixed(3)),
      })}`
    );
    expect(p95).toBeLessThanOrEqual(REVIEW_PAGE_P95_THRESHOLD_MS);

    const audit = await auditRun(page, runId);
    expect(audit.steps).toHaveLength(2);
    expect(audit.reviews).toEqual([]);
  });

  test('rejects a stale screening review and refreshes the current content-free descriptor', async ({
    page,
  }) => {
    await login(page);
    const runId = await startDailyBrief(
      page,
      'What are the controlled treatment outcomes?',
      'stale review'
    );
    await expect(
      page.getByRole('heading', { name: 'Screening review' })
    ).toBeVisible();
    await selectEveryDecision(page, 'include');

    const rotated = await page.evaluate(async (id) => {
      const response = await fetch(`/api/v1/e2e/runs/${id}/rotate-review`, {
        method: 'POST',
      });
      if (!response.ok) throw new Error(`rotation failed: ${response.status}`);
      return response.json() as Promise<{ old_hash: string; new_hash: string }>;
    }, runId);
    expect(rotated.new_hash).not.toBe(rotated.old_hash);

    const staleResponse = page.waitForResponse(
      (response) =>
        response.url().includes(`/runs/${runId}/reviews/1`) &&
        response.request().method() === 'POST'
    );
    await page
      .getByRole('button', { name: 'Approve screening review' })
      .click();
    expect((await staleResponse).status()).toBe(409);
    await expect(
      page.getByRole('alert', { name: 'Review changed' })
    ).toContainText('current review has been refreshed');
    expect((await auditRun(page, runId)).reviews).toEqual([]);
  });

  test('returns the original immutable row for a duplicate review submission', async ({
    page,
  }) => {
    await login(page);
    const runId = await startDailyBrief(
      page,
      'What are the controlled treatment outcomes?',
      'duplicate review'
    );
    await expect(
      page.getByRole('heading', { name: 'Screening review' })
    ).toBeVisible();
    await selectEveryDecision(page, 'include');

    const acceptedResponsePromise = page.waitForResponse(
      (response) =>
        response.url().includes(`/runs/${runId}/reviews/1`) &&
        response.request().method() === 'POST'
    );
    await page
      .getByRole('button', { name: 'Approve screening review' })
      .click();
    const acceptedResponse = await acceptedResponsePromise;
    expect(acceptedResponse.status()).toBe(200);
    const accepted = (await acceptedResponse.json()) as AcceptedReview;
    expect(accepted.replay).toBe(false);
    const originalRequest = acceptedResponse.request();
    const requestHeaders = await originalRequest.allHeaders();
    const requestBody = originalRequest.postDataJSON();

    const replayed = await page.evaluate(
      async ({ run, authorization, body }) => {
        const response = await fetch(
          `/api/v1/research-engine/runs/${run}/reviews/1`,
          {
            method: 'POST',
            headers: {
              Authorization: authorization,
              'Content-Type': 'application/json',
            },
            body: JSON.stringify(body),
          }
        );
        return {
          status: response.status,
          body: (await response.json()) as AcceptedReview,
        };
      },
      {
        run: runId,
        authorization: requestHeaders.authorization,
        body: requestBody,
      }
    );
    expect(replayed.status).toBe(200);
    expect(replayed.body.id).toBe(accepted.id);
    expect(replayed.body.replay).toBe(true);
    expect((await auditRun(page, runId)).reviews).toHaveLength(1);
  });

  test('returns the same non-enumerating denial to same-organization and cross-tenant non-owners', async ({
    page,
    browser,
  }) => {
    await login(page);
    const runId = await startDailyBrief(
      page,
      'What are the controlled treatment outcomes?',
      'owner isolation'
    );
    await expect(
      page.getByRole('heading', { name: 'Screening review' })
    ).toBeVisible();

    await assertDeniedRun(browser, runId, 'same-org-intruder@example.test');
    await assertDeniedRun(browser, runId, 'intruder@example.test');
  });
});
