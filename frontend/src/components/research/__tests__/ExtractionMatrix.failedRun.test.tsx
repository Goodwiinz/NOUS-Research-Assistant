/**
 * A polled extraction task that ends 'failed' must surface its error.
 *
 * The create flow enqueues an auto-extraction and polls its task status.
 * On 'failed' the component used to drop the progress banner and show the
 * (empty) grid with no message at all, so a run whose every document failed
 * (revoked key, quota) looked like a successful run that found nothing.
 *
 * Mutation check (docs/engineering/testing.md): in ExtractionMatrix.tsx,
 * restore the banner condition to `autoExtractionTaskId && autoExtractionStatus`
 * (or drop the `role="alert"` failed branch) and the first test fails with
 * "Unable to find role=alert"; the second must keep passing either way.
 */
import { describe, expect, it, vi } from 'vitest';
import userEvent from '@testing-library/user-event';
import { render, screen, within } from '@/test/test-utils';
import { ExtractionMatrix } from '../ExtractionMatrix';
import {
  createMatrix,
  deleteMatrix,
  getExtractionTaskStatus,
  getMatrix,
  listMatrices,
} from '@/services/scispaceService';
import type {
  ExtractionMatrix as ExtractionMatrixType,
  ExtractionTaskStatus,
} from '@/types/scispace';

vi.mock('@/hooks/useAuth', () => ({ useAuth: () => ({ user: { id: 'me' } }) }));
vi.mock('@/services/researchEngineService', () => ({
  listProjectRoles: vi.fn().mockResolvedValue([]),
}));
vi.mock('@/services/scispaceService', () => ({
  createMatrix: vi.fn(),
  deleteMatrix: vi.fn(),
  getExtractionTaskStatus: vi.fn(),
  getMatrix: vi.fn(),
  listMatrices: vi.fn(),
  triggerExtraction: vi.fn(),
  updateMatrix: vi.fn(),
  getCellObservations: vi.fn(),
  listFormVersions: vi.fn(),
}));

const matrix: ExtractionMatrixType = {
  id: 'm-1',
  project_id: 'p-1',
  name: 'Outcomes',
  columns: [{ name: 'Sample size' }],
  cells: [],
  created_at: null,
  updated_at: null,
};

const taskStatus = (
  overrides: Partial<ExtractionTaskStatus>
): ExtractionTaskStatus => ({
  status: 'running',
  matrix_id: 'm-1',
  total: 2,
  completed: 0,
  failed: 0,
  skipped: 0,
  ...overrides,
});

async function createAndPoll(status: ExtractionTaskStatus): Promise<void> {
  vi.mocked(listMatrices).mockResolvedValue({ matrices: [] });
  vi.mocked(getMatrix).mockResolvedValue(matrix);
  vi.mocked(createMatrix).mockResolvedValue({
    id: 'm-1',
    project_id: 'p-1',
    name: 'Outcomes',
    columns: [{ name: 'Sample size' }],
    created_at: '2026-10-16T00:00:00Z',
    extraction_task_id: 'task-1',
  });
  vi.mocked(getExtractionTaskStatus).mockResolvedValue(status);

  const { user } = render(
    <ExtractionMatrix
      projectId="p-1"
      documents={[
        { id: 'doc-1', title: 'Paper A' },
        { id: 'doc-2', title: 'Paper B' },
      ]}
    />
  );

  await user.type(
    await screen.findByPlaceholderText('e.g. Literature Review 2024'),
    'Outcomes'
  );
  await user.type(screen.getByPlaceholderText('Column name'), 'Sample size');
  await user.click(screen.getByRole('button', { name: 'Create matrix' }));
  expect(await screen.findByText('Outcomes')).toBeInTheDocument();
}

describe('ExtractionMatrix polled task outcome', () => {
  it('shows the error when every document failed', async () => {
    await createAndPoll(
      taskStatus({
        status: 'failed',
        failed: 2,
        error: '2 of 2 documents failed; nothing was recorded',
      })
    );

    const alert = await screen.findByRole('alert');
    expect(alert).toHaveTextContent(
      '2 of 2 documents failed; nothing was recorded'
    );
    expect(screen.queryByText(/^Extracting/)).not.toBeInTheDocument();
    expect(getExtractionTaskStatus).toHaveBeenCalledWith('task-1');
  });

  it('drops a stale failed alert when another matrix is created', async () => {
    // Mutation check: remove the setAutoExtractionStatus(null) reset at the
    // start of handleCreate and the final assertion fails because the first
    // matrix's alert is still in the document under the second matrix.
    await createAndPoll(
      taskStatus({
        status: 'failed',
        failed: 2,
        error: '2 of 2 documents failed; nothing was recorded',
      })
    );
    expect(await screen.findByRole('alert')).toBeInTheDocument();

    // Delete the failed matrix; the create form comes back with its fields.
    vi.mocked(deleteMatrix).mockResolvedValue(undefined);
    const user = userEvent.setup();
    await user.click(screen.getByRole('button', { name: 'Delete' }));
    const dialog = await screen.findByRole('dialog');
    await user.click(within(dialog).getByRole('button', { name: 'Delete' }));
    expect(
      await screen.findByRole('button', { name: 'Create matrix' })
    ).toBeInTheDocument();

    // The second matrix enqueues no extraction, so nothing would ever poll
    // and replace the stale status.
    vi.mocked(createMatrix).mockResolvedValue({
      id: 'm-2',
      project_id: 'p-1',
      name: 'Second',
      columns: [{ name: 'Sample size' }],
      created_at: '2026-10-16T00:00:00Z',
      extraction_task_id: null,
    });
    vi.mocked(getMatrix).mockResolvedValue({
      ...matrix,
      id: 'm-2',
      name: 'Second',
    });
    await user.click(screen.getByRole('button', { name: 'Create matrix' }));

    expect(await screen.findByText('Second')).toBeInTheDocument();
    expect(screen.queryByRole('alert')).not.toBeInTheDocument();
  });

  it('clears the banner after a completed run', async () => {
    await createAndPoll(taskStatus({ status: 'completed', completed: 2 }));

    // The completed poll refetches the matrix and drops the progress banner.
    await vi.waitFor(() => expect(getMatrix).toHaveBeenCalledTimes(2));
    expect(screen.queryByText(/^Extracting/)).not.toBeInTheDocument();
    expect(screen.queryByRole('alert')).not.toBeInTheDocument();
  });
});
