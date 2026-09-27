/**
 * Browser smoke for canonical research-project navigation.
 *
 * Authentication and API responses are intentionally fulfilled in-browser;
 * this verifies routing, UI identity, and request IDs, not live backend data.
 */
import { expect, test, type Page } from '@playwright/test';

const COLLECTION_ID = '11111111-1111-4111-8111-111111111111';
const ENGINE_ID = '22222222-2222-4222-8222-222222222222';
const USER_ID = '33333333-3333-4333-8333-333333333333';
const BLUEPRINT_ID = '55555555-5555-4555-8555-555555555555';
const RUN_ID = '66666666-6666-4666-8666-666666666666';
const MATRIX_ID = '77777777-7777-4777-8777-777777777777';
const DRAFT_ID = '88888888-8888-4888-8888-888888888888';
const seenProjectScopedPaths: string[] = [];

test.skip(
  process.env.MOCK_PROJECT_IDENTITY_E2E !== '1',
  'Requires the documented same-origin mock Supabase/API harness.'
);

async function installMockedApi(page: Page): Promise<void> {
  await page.route(/\/api\/v[12]\//, async (route) => {
    const request = route.request();
    const url = new URL(request.url());
    const path = url.pathname.replace(/^\/api\/v[12]/, '');
    if (path === '/auth/me') {
      await route.fulfill({
        json: {
          user: { id: USER_ID, email: 'browser@example.com', role: 'user' },
        },
      });
      return;
    }
    if (
      !path.includes('/legacy-projects/') &&
      (path.includes('/projects/') || path.includes('/research/projects/'))
    ) {
      seenProjectScopedPaths.push(path);
    }
    const project = {
      id: COLLECTION_ID,
      name: 'Canonical research project',
      workspace_id: '44444444-4444-4444-8444-444444444444',
      research_status: 'active',
      research_engine_project_id: ENGINE_ID,
      can_edit: true,
      can_manage: true,
      workspace_archived: false,
      created_at: '2026-09-27T00:00:00Z',
      updated_at: '2026-09-27T00:00:00Z',
    };
    if (request.method() === 'GET' && path === '/workspaces') {
      await route.fulfill({
        json: [{ id: project.workspace_id, name: 'Research workspace', owner_id: USER_ID, is_archived: false }],
      });
    } else if (request.method() === 'GET' && path === `/workspaces/${project.workspace_id}`) {
      await route.fulfill({
        json: { id: project.workspace_id, name: 'Research workspace', owner_id: USER_ID, is_archived: false },
      });
    } else if (request.method() === 'GET' && path === '/projects') {
      await route.fulfill({ json: { projects: [project], total: 1 } });
    } else if (request.method() === 'GET' && path === `/projects/${COLLECTION_ID}`) {
      await route.fulfill({ json: project });
    } else if (path === `/research-engine/legacy-projects/${ENGINE_ID}`) {
      await route.fulfill({
        json: { research_engine_project_id: ENGINE_ID, name: project.name, status: 'active', collection_id: COLLECTION_ID },
      });
    } else if (path === `/projects/${COLLECTION_ID}/documents`) {
      await route.fulfill({ json: { documents: [{ id: 'link-1', project_id: COLLECTION_ID, document_id: 'doc-1', document: { id: 'doc-1', title: 'Seeded source paper', filename: 'source.pdf', status: 'completed' } }], total: 1 } });
    } else if (path.endsWith('/notes')) {
      await route.fulfill({ json: { notes: [], total: 0 } });
    } else if (path === `/research-engine/projects/${COLLECTION_ID}`) {
      await route.fulfill({ json: { ...project, project_id: COLLECTION_ID, collection_id: COLLECTION_ID, blueprint_id: BLUEPRINT_ID, settings: {} } });
    } else if (path === `/research-engine/blueprints/${BLUEPRINT_ID}`) {
      await route.fulfill({ json: { id: BLUEPRINT_ID, name: 'Seeded evidence workflow', version: 1, steps: [{ type: 'search', name: 'Collect evidence', parameters: {}, mode: 'deterministic' }], parameters: {} } });
    } else if (request.method() === 'POST' && path === `/research-engine/blueprints/${BLUEPRINT_ID}/runs`) {
      await route.fulfill({ json: { id: RUN_ID } });
    } else if (path === `/research-engine/runs/${RUN_ID}`) {
      await route.fulfill({ json: { id: RUN_ID, project_id: COLLECTION_ID, research_engine_project_id: ENGINE_ID, blueprint_id: BLUEPRINT_ID, blueprint_version: 1, status: 'completed', total_tokens: 42, started_at: '2026-09-27T00:00:00Z', completed_at: '2026-09-27T00:01:00Z' } });
    } else if (path === `/research/projects/${COLLECTION_ID}/matrices`) {
      await route.fulfill({ json: { matrices: [{ id: MATRIX_ID, project_id: COLLECTION_ID, name: 'Seeded extraction matrix', columns: [], created_at: '2026-09-27T00:00:00Z' }], total: 1 } });
    } else if (path === `/research/matrices/${MATRIX_ID}`) {
      await route.fulfill({ json: { id: MATRIX_ID, project_id: COLLECTION_ID, name: 'Seeded extraction matrix', columns: [{ name: 'Finding' }], cells: [{ document_id: 'doc-1', column_name: 'Finding', value: 'Seeded matrix cell', citation_snippet: 'Source passage', confidence: 0.9 }], created_at: '2026-09-27T00:00:00Z', updated_at: '2026-09-27T00:00:00Z' } });
    } else if (path.includes('/drafts/reviews')) {
      await route.fulfill({ json: { reviews: [] } });
    } else if (path === `/projects/${COLLECTION_ID}/drafts/${DRAFT_ID}`) {
      await route.fulfill({ json: { id: DRAFT_ID, project_id: COLLECTION_ID, version: 1, title: 'Seeded synthesis draft', content: 'Seeded grounded draft content.', themes: ['evidence'], word_count: 4, citation_count: 1, is_current: true, created_at: '2026-09-27T00:00:00Z' } });
    } else if (path.startsWith(`/projects/${COLLECTION_ID}/drafts`)) {
      await route.fulfill({ json: { drafts: [{ id: DRAFT_ID, project_id: COLLECTION_ID, version: 1, title: 'Seeded synthesis draft', themes: ['evidence'], word_count: 4, citation_count: 1, is_current: true, created_at: '2026-09-27T00:00:00Z' }], total: 1, skip: 0, limit: 20 } });
    } else if (path.includes('/roles')) {
      await route.fulfill({ json: [] });
    } else if (path.includes('/extraction-matrices')) {
      await route.fulfill({ json: { matrices: [], total: 0 } });
    } else {
      await route.fulfill({ json: { items: [], total: 0 } });
    }
  });
}

async function login(page: Page): Promise<void> {
  await page.goto('/login');
  await page.getByTestId('email-input').fill('browser@example.com');
  await page.getByTestId('password-input').fill('mock-password');
  await page.getByTestId('login-button').click();
  await page.waitForURL(/\/dashboard/);
}

test('one canonical card retains collection identity across research surfaces', async ({ page }) => {
  seenProjectScopedPaths.length = 0;
  await installMockedApi(page);
  await login(page);
  await page.goto('/projects');
  await expect(page.getByText('Canonical research project')).toHaveCount(1);
  await page.getByText('Canonical research project').click();
  await expect(page).toHaveURL(new RegExp(`/projects/${COLLECTION_ID}`));

  await page.getByRole('tab', { name: 'Documents' }).click();
  await expect(page.getByText('Seeded source paper')).toBeVisible();
  await page.getByRole('button', { name: 'More tabs' }).click();
  await page.getByRole('menuitem', { name: 'Workflow' }).click();
  await expect(page.getByRole('textbox', { name: 'Blueprint name' })).toHaveValue('Seeded evidence workflow');
  await page.getByRole('button', { name: /Start run/i }).click();
  await expect(page).toHaveURL(`/research-engine/runs/${RUN_ID}`);
  await expect(page.getByRole('status')).toContainText(/completed/i);
  await page.getByRole('button', { name: 'Go back' }).click();
  await expect(page).toHaveURL(`/projects/${COLLECTION_ID}?tab=workflow`);
  await page.getByRole('button', { name: 'More tabs' }).click();
  await page.getByRole('menuitem', { name: 'Matrix' }).click();
  await expect(page.getByText('Seeded extraction matrix')).toBeVisible();
  await expect(page.getByText('Seeded matrix cell')).toBeVisible();
  await page.getByRole('tab', { name: 'Drafts' }).click();
  await expect(page.getByText('Seeded synthesis draft')).toBeVisible();
  await expect(page.getByText('Seeded grounded draft content.')).toBeVisible();
  expect(seenProjectScopedPaths).toContain(
    `/research-engine/projects/${COLLECTION_ID}`
  );
  expect(seenProjectScopedPaths).toContain(
    `/research/projects/${COLLECTION_ID}/matrices`
  );
  expect(seenProjectScopedPaths.some((path) => path.startsWith(`/projects/${COLLECTION_ID}/documents`))).toBe(true);
  expect(seenProjectScopedPaths.some((path) => path.startsWith(`/projects/${COLLECTION_ID}/drafts`))).toBe(true);
  expect(seenProjectScopedPaths.every((path) => !path.includes(ENGINE_ID))).toBe(true);

  await page.goto(`/research-engine/projects/${ENGINE_ID}/blueprint`);
  await expect(page).toHaveURL(`/projects/${COLLECTION_ID}?tab=workflow`);
  await expect(page.getByRole('textbox', { name: 'Blueprint name' })).toHaveValue('Seeded evidence workflow');
});
