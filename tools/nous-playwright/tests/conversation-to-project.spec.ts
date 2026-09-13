import { randomUUID } from 'node:crypto';
import { test, expect, type Page, type Locator, type TestInfo } from 'playwright/test';

const PAPER = 'Attention Is All You Need';
const PAPER_ID = process.env.NOUS_PAPER_ID ?? '74c232ea-e007-4a73-b8ed-df0f88bae654';
const AGENT_TIMEOUT = 180_000;
const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

type ProjectDocument = {
  project_id: string;
  document_id: string;
  document?: { id: string; title: string; status: string };
};

async function capture(page: Page, info: TestInfo, name: string) {
  await info.attach(name, {
    body: await page.screenshot({ fullPage: false }),
    contentType: 'image/png',
  });
}

async function send(page: Page, message: string) {
  const composer = page.getByRole('textbox', {
    name: 'Ask anything, or paste a passage to discuss…', exact: true,
  });
  await expect(composer).toBeEnabled();
  await composer.fill(message);
  await page.getByRole('button', { name: 'Send', exact: true }).click();
  // Scope to user rows; matching the input's value would not prove submission.
  await expect(page.locator('[data-role="user"]').filter({ hasText: message }))
    .toHaveCount(1);
}

async function turnFinished(page: Page) {
  await expect(page.getByRole('button', { name: 'Stop agent', exact: true }))
    .toBeHidden({ timeout: AGENT_TIMEOUT });
  await expect(page.getByRole('switch', { name: 'Use my sources', exact: true }))
    .toBeEnabled({ timeout: AGENT_TIMEOUT });
  // An empty composer makes Send disabled even after a successful turn.
  await expect(page.getByRole('button', { name: 'Send', exact: true })).toBeVisible();
}

async function actionArguments(gate: Locator, action: string) {
  await expect(gate).toHaveCount(1, { timeout: AGENT_TIMEOUT });
  const args = gate.getByLabel(`Arguments for ${action}`, { exact: true });
  await expect(args).toBeVisible({ timeout: AGENT_TIMEOUT });
  // Never approve a batch containing an extra, unexpected action.
  await expect(gate.locator('section')).toHaveCount(1);
  return args.evaluate((dl) => Object.fromEntries(
    Array.from(dl.querySelectorAll('dt')).map((dt) => [
      dt.textContent?.trim().replace(/\s+/g, '_') ?? '',
      dt.nextElementSibling?.textContent?.trim() ?? '',
    ]),
  ));
}

async function projectDocuments(
  page: Page,
  projectId: string,
  navigate: () => Promise<unknown>,
): Promise<ProjectDocument[]> {
  // Observe the real UI's GET response; do not mock it or extract auth tokens.
  const responsePromise = page.waitForResponse((response) =>
    new URL(response.url()).pathname === `/api/v1/projects/${projectId}/documents`
      && response.request().method() === 'GET',
    { timeout: 60_000 },
  );
  const [, response] = await Promise.all([navigate(), responsePromise]);
  expect(response.ok(), `Project documents GET returned ${response.status()}`).toBeTruthy();
  const envelope = await response.json();
  const payload = envelope.data ?? envelope;
  expect(Array.isArray(payload.documents), 'Expected the project documents response').toBe(true);
  return payload.documents as ProjectDocument[];
}

test('chat retrieves the paper, approves a new project, and persists its document', async ({
  page, context,
}, info) => {
  // Regressions caught: absent answer, user text mistaken for an answer,
  // duplicate reconciled messages, missing/wrong approvals, wrong document or
  // project IDs, and success prose without durable project membership.
  expect(UUID.test(PAPER_ID), 'NOUS_PAPER_ID must identify the indexed paper').toBe(true);
  const projectName = `NOUS E2E - Transformer Research - ${randomUUID().slice(0, 12)}`;
  const run: Record<string, string> = { projectName, paperId: PAPER_ID };
  let verificationPage: Page | undefined;

  try {
    await test.step('Verify the actual indexed source', async () => {
      await page.goto(`/documents/${PAPER_ID}`);
      await expect(page.getByRole('button', { name: 'Sign out', exact: true })).toBeVisible();
      await expect(page.getByRole('heading', { name: PAPER, exact: true })).toBeVisible();
      await expect(page.getByText('ready for retrieval', { exact: true })).toBeVisible();
      await expect(page.getByRole('tabpanel', { name: 'Overview', exact: true }))
        .toContainText(/attention mechanisms/i);
      await capture(page, info, '01-indexed-source');
    });

    await test.step('Start a fresh conversation and receive a source-named answer', async () => {
      await page.getByRole('link', { name: 'Chat', exact: true }).click();
      // Wait for existing selection to hydrate before resetting it.
      await expect(page.getByRole('textbox', {
        name: 'Ask anything, or paste a passage to discuss…', exact: true,
      })).toBeEnabled({ timeout: 60_000 });
      await page.getByRole('button', { name: /^New chat/ }).click();
      await expect(page.getByRole('heading', {
        name: 'What would you like to find out?', exact: true,
      })).toBeVisible();
      await expect(page.getByRole('switch', { name: 'Use my sources', exact: true })).toBeChecked();
      await send(page,
        `Find ${PAPER} in my documents. In two sentences, explain what the ` +
        'Transformer uses instead of recurrence and convolutions. Cite the supporting document.',
      );
      const answer = page.locator('[data-role="assistant"]');
      await expect(answer.filter({ hasText: /self[\s-]?attention/i })).toHaveCount(1, {
        timeout: AGENT_TIMEOUT,
      });
      await turnFinished(page);
      await expect(answer).toHaveCount(1); // no duplicate final transcript rows
      await expect(answer).toContainText(/self[\s-]?attention/i);
      // Catch missing sources and citations wired to the wrong document, without
      // requiring the old direct-link presentation of the sources footer.
      const source = answer.getByRole('region', { name: 'Sources', exact: true })
        .getByRole('button')
        .filter({ has: page.getByText(/^Attention Is All You Need(?: · p\. \d+)?$/) });
      await expect(source).toHaveCount(1);
      await source.click();
      const viewer = page.getByRole('region', { name: 'Artifact viewer', exact: true });
      await viewer.getByRole('button', { name: 'Open document', exact: true }).click();
      await expect(viewer.getByRole('heading', { name: PAPER, exact: true })).toBeVisible();
      await expect(viewer.getByRole('link', { name: 'Open full page', exact: true }))
        .toHaveAttribute('href', `/documents/${PAPER_ID}`);
      await viewer.getByRole('button', { name: 'Close artifact panel', exact: true }).click();
      // This remains a source-identity smoke check, not a semantic-faithfulness score.
      await page.waitForURL((url) => Boolean(url.searchParams.get('thread')), { timeout: 30_000 });
      run.threadUrl = page.url();
      await capture(page, info, '02-answer-and-tools');
    });

    const gate = page.getByRole('alertdialog', { name: 'Approval needed', exact: true });
    await test.step('Ask from the conversation and approve only the requested project', async () => {
      await send(page,
        `Create a research project named "${projectName}" and add the indexed paper ` +
        `"${PAPER}" (document ID ${PAPER_ID}) to it. Use the project tools and ` +
        'show their approval dialogs. Do not substitute a prose-only confirmation.',
      );
      const args = await actionArguments(gate, 'Create project');
      expect(args.name).toBe(projectName);
      await capture(page, info, '03-create-project-approval');
      await gate.getByRole('button', { name: 'Approve', exact: true }).click();
    });

    await test.step('Verify the attachment target and approve the paper', async () => {
      const args = await actionArguments(gate, 'Add document to project');
      expect(args.document_id).toBe(PAPER_ID);
      expect(args.project_id).toMatch(UUID);
      run.projectId = args.project_id;
      run.projectUrl = new URL(`/projects/${run.projectId}`, page.url()).href;

      // A separate page checks persisted state while the attachment is pending.
      // This fails if the paper was attached before the user approved that action.
      verificationPage = await context.newPage();
      const before = await projectDocuments(verificationPage, run.projectId,
        () => verificationPage!.goto(run.projectUrl));
      await expect(verificationPage.getByRole('heading', { name: projectName, exact: true }))
        .toBeVisible();
      expect(before, 'A newly created project should still be empty before attachment approval')
        .toHaveLength(0);
      await capture(page, info, '04-add-document-approval');
      await page.bringToFront();
      await gate.getByRole('button', { name: 'Approve', exact: true }).click();
      // Reconciliation can briefly overlap optimistic and persisted tool rows.
      // Require every approval dialog to disappear, rather than a single-element
      // assertion that throws on the overlap before the wait can finish.
      await expect(gate).toHaveCount(0, { timeout: AGENT_TIMEOUT });
      const completed = page.locator('[data-role="assistant"]').filter({ hasText: projectName });
      // The UI can briefly overlap optimistic and persisted rows. Wait for
      // content without a strict single-element lookup, then check final counts.
      await expect.poll(async () => (await completed.allTextContents())
        .some((text) => text.includes(PAPER)), { timeout: AGENT_TIMEOUT }).toBe(true);
      await turnFinished(page);
      await expect(page.locator('[data-role="assistant"]')).toHaveCount(2);
      await expect(completed).toHaveCount(1);
      await expect(completed).toContainText(PAPER);
      await capture(page, info, '05-agent-completed');
    });

    await test.step('Verify exact membership, then reload and verify persistence', async () => {
      const verifyMembership = (documents: ProjectDocument[]) => {
        expect(documents, 'Exactly one paper should be attached').toHaveLength(1);
        expect(documents[0]).toMatchObject({
          project_id: run.projectId,
          document_id: PAPER_ID,
          document: { id: PAPER_ID, title: PAPER },
        });
        expect(documents[0].document?.status).toMatch(/^(indexed|completed)$/i);
      };
      verifyMembership(await projectDocuments(page, run.projectId, () => page.goto(run.projectUrl)));
      await expect(page.getByRole('heading', { name: projectName, exact: true })).toBeVisible();
      await expect(page.getByRole('tab', { name: 'Documents 1', exact: true })).toBeVisible();
      await expect(page.getByRole('checkbox', { name: `Select ${PAPER}`, exact: true })).toBeVisible();
      await capture(page, info, '06-project-with-paper');

      verifyMembership(await projectDocuments(page, run.projectId, () => page.reload()));
      await expect(page.getByRole('heading', { name: projectName, exact: true })).toBeVisible();
      await expect(page.getByRole('tab', { name: 'Documents 1', exact: true })).toBeVisible();
      await expect(page.getByRole('checkbox', { name: `Select ${PAPER}`, exact: true })).toBeVisible();
      await capture(page, info, '07-persisted-after-reload');
    });
  } finally {
    // Keep the test-created project for inspection; never delete the shared paper.
    await info.attach('created-resources.json', {
      body: Buffer.from(JSON.stringify(run, null, 2)), contentType: 'application/json',
    });
    await verificationPage?.close();
  }
});
