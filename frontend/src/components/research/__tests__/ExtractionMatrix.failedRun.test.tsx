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
import { act, render, screen, waitFor, within } from '@/test/test-utils';
import { ExtractionMatrix } from '../ExtractionMatrix';
import {
  createMatrix,
  deleteMatrix,
  getExtractionTaskStatus,
  getMatrix,
  listMatrices,
  triggerExtraction,
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

const FAILED = taskStatus({
  status: 'failed',
  failed: 2,
  error: '2 of 2 documents failed; nothing was recorded',
});

async function createAndPoll(
  status: ExtractionTaskStatus | (() => Promise<ExtractionTaskStatus>)
): Promise<void> {
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
  if (typeof status === 'function') {
    vi.mocked(getExtractionTaskStatus).mockImplementation(status);
  } else {
    vi.mocked(getExtractionTaskStatus).mockResolvedValue(status);
  }

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

  it('keeps the failed alert when a stale in-flight poll resolves running', async () => {
    // Out-of-order responses: poll 1 outlasts the 3s interval, poll 2 returns
    // 'failed' (alert shown, task id cleared), then poll 1 finally resolves
    // 'running'. That late response must be dropped, not written over the
    // terminal status (which would hide both progress and alert).
    // Mutation check: remove the `stale()` guard in the poll effect and this
    // fails at the final getByRole('alert').
    vi.useFakeTimers({ shouldAdvanceTime: true });
    try {
      let resolveFirst!: (status: ExtractionTaskStatus) => void;
      const first = new Promise<ExtractionTaskStatus>((resolve) => {
        resolveFirst = resolve;
      });
      let calls = 0;
      await createAndPoll(() => {
        calls += 1;
        return calls === 1 ? first : Promise.resolve(FAILED);
      });

      await vi.advanceTimersByTimeAsync(3000); // poll 2 -> failed
      expect(await screen.findByRole('alert')).toBeInTheDocument();

      await act(async () => {
        resolveFirst(taskStatus({ status: 'running', completed: 1 }));
        await vi.advanceTimersByTimeAsync(10);
      });

      expect(screen.getByRole('alert')).toHaveTextContent(
        '2 of 2 documents failed; nothing was recorded'
      );
      expect(screen.queryByText(/Extracting 1\/2/)).not.toBeInTheDocument();
    } finally {
      vi.useRealTimers();
    }
  });

  it('a retry clears the failed alert and polls the new task', async () => {
    // Mutation check: in startExtraction, remove setAutoExtractionStatus(null)
    // -> the alert assertion fails; remove setAutoExtractionTaskId(task_id)
    // -> "Extracting 1/2" is never rendered.
    vi.useFakeTimers({ shouldAdvanceTime: true });
    try {
      await createAndPoll(FAILED);
      expect(await screen.findByRole('alert')).toBeInTheDocument();

      vi.mocked(triggerExtraction).mockResolvedValue({
        task_id: 'task-2',
        matrix_id: 'm-1',
        document_ids: ['doc-1', 'doc-2'],
        status: 'accepted',
        message: 'Extraction started for 2 document(s)',
      });
      // The new task's first poll is held pending: the old alert must already
      // be gone at retry start, not only once a fresh status lands.
      let resolveStatus!: (status: ExtractionTaskStatus) => void;
      vi.mocked(getExtractionTaskStatus).mockImplementationOnce(
        () =>
          new Promise<ExtractionTaskStatus>((resolve) => {
            resolveStatus = resolve;
          })
      );
      const user = userEvent.setup();
      await user.click(screen.getByRole('button', { name: 'Extract' }));

      await waitFor(() =>
        expect(getExtractionTaskStatus).toHaveBeenLastCalledWith('task-2')
      );
      expect(screen.queryByRole('alert')).not.toBeInTheDocument();

      vi.mocked(getExtractionTaskStatus).mockResolvedValue(
        taskStatus({ status: 'running', completed: 1 })
      );
      // The first status is the route's seed, written before the worker's
      // first update; older seeds carry no `completed`. It must render as
      // 0/2, never "/2". Mutation: drop `?? 0` from the banner and the 0/2
      // assertion fails.
      await act(async () => {
        resolveStatus({
          status: 'running',
          matrix_id: 'm-1',
          total: 2,
          failed: 0,
          skipped: 0,
        } as ExtractionTaskStatus);
        await vi.advanceTimersByTimeAsync(10);
      });
      expect(await screen.findByText(/Extracting 0\/2/)).toBeInTheDocument();
      expect(screen.queryByRole('alert')).not.toBeInTheDocument();

      await vi.advanceTimersByTimeAsync(3000);
      expect(await screen.findByText(/Extracting 1\/2/)).toBeInTheDocument();

      vi.mocked(getExtractionTaskStatus).mockResolvedValue(
        taskStatus({ status: 'completed', completed: 2 })
      );
      await vi.advanceTimersByTimeAsync(3000);
      await waitFor(() =>
        expect(screen.queryByText(/Extracting 1\/2/)).not.toBeInTheDocument()
      );
      expect(screen.queryByRole('alert')).not.toBeInTheDocument();
      // create, after the trigger, after the completed poll
      expect(getMatrix).toHaveBeenCalledTimes(3);
    } finally {
      vi.useRealTimers();
    }
  });

  it('clears the banner after a completed run', async () => {
    await createAndPoll(taskStatus({ status: 'completed', completed: 2 }));

    // The completed poll refetches the matrix and drops the progress banner.
    await vi.waitFor(() => expect(getMatrix).toHaveBeenCalledTimes(2));
    expect(screen.queryByText(/^Extracting/)).not.toBeInTheDocument();
    expect(screen.queryByRole('alert')).not.toBeInTheDocument();
  });
});
