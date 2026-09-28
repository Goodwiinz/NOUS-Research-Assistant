import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import type {
  PendingReviewResponse,
  RunResponse,
  StepResponse,
} from '@/services/researchEngineService';
import {
  getPendingReview,
  getRun,
  listSteps,
  resumeRun,
} from '@/services/researchEngineService';
import { useResearchEngineStore } from '@/store/research-engine-store';
import { RunView } from '../RunView';

vi.mock('next/navigation', () => ({
  useRouter: () => ({ back: vi.fn() }),
}));

vi.mock('@/lib/supabase/client', () => ({
  createClient: () => ({
    auth: {
      getSession: vi.fn().mockResolvedValue({ data: { session: null } }),
    },
  }),
}));

vi.mock('@/services/researchEngineService', async (importOriginal) => {
  const original =
    await importOriginal<typeof import('@/services/researchEngineService')>();
  return {
    ...original,
    getRun: vi.fn(),
    listSteps: vi.fn(),
    getPendingReview: vi.fn(),
    pauseRun: vi.fn(),
    resumeRun: vi.fn(),
    submitReview: vi.fn(),
  };
});

const RUN_ID = '11111111-1111-4111-8111-111111111111';

function run(overrides: Partial<RunResponse> = {}): RunResponse {
  return {
    id: RUN_ID,
    blueprint_id: '33333333-3333-4333-8333-333333333333',
    blueprint_version: 1,
    status: 'running',
    total_tokens: 12,
    created_at: '2026-09-27T10:00:00Z',
    updated_at: '2026-09-27T10:00:01Z',
    ...overrides,
  };
}

function persistedStep(overrides: Partial<StepResponse> = {}): StepResponse {
  return {
    id: 'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa',
    run_id: RUN_ID,
    step_index: 0,
    step_type: 'search',
    mode: 'deterministic',
    temperature: 0,
    token_count: 12,
    started_at: '2026-09-27T10:00:01Z',
    completed_at: '2026-09-27T10:00:02Z',
    output: { source_records: [] },
    quality_marks: [],
    ...overrides,
  };
}

describe('RunView durable run state', () => {
  beforeEach(() => {
    vi.resetAllMocks();
    useResearchEngineStore.setState(
      useResearchEngineStore.getInitialState(),
      true
    );
    vi.stubGlobal(
      'fetch',
      vi.fn().mockResolvedValue({ ok: false, body: null })
    );
    vi.mocked(getPendingReview).mockResolvedValue({ pending: false });
    vi.mocked(resumeRun).mockResolvedValue(run());
  });

  it('hydrates the run and persisted steps before opening the event stream', async () => {
    let resolveRun!: (value: RunResponse) => void;
    let resolveSteps!: (value: StepResponse[]) => void;
    vi.mocked(getRun).mockReturnValue(
      new Promise((resolve) => {
        resolveRun = resolve;
      })
    );
    vi.mocked(listSteps).mockReturnValue(
      new Promise((resolve) => {
        resolveSteps = resolve;
      })
    );

    render(<RunView runId={RUN_ID} />);
    await waitFor(() => {
      expect(getRun).toHaveBeenCalledWith(RUN_ID);
      expect(listSteps).toHaveBeenCalledWith(RUN_ID);
    });
    expect(fetch).not.toHaveBeenCalled();

    resolveRun(run());
    resolveSteps([persistedStep()]);

    expect(
      await screen.findByRole('button', { name: /search/i })
    ).toBeInTheDocument();
    await waitFor(() =>
      expect(fetch).toHaveBeenCalledWith(
        `/api/v1/research-engine/runs/${RUN_ID}/stream`,
        expect.any(Object)
      )
    );
  });

  it('shows a stable load error without exposing internal exception details', async () => {
    vi.mocked(getRun).mockRejectedValue(
      new Error('database connection password=internal-secret')
    );
    vi.mocked(listSteps).mockResolvedValue([]);

    render(<RunView runId={RUN_ID} />);

    expect(
      await screen.findByText('The run could not be loaded. Try again.')
    ).toBeInTheDocument();
    expect(screen.queryByText(/internal-secret/i)).not.toBeInTheDocument();
  });

  it('reconnects a running stream after a clean end without duplicating persisted state', async () => {
    vi.mocked(getRun).mockResolvedValue(run());
    vi.mocked(listSteps).mockResolvedValue([persistedStep()]);
    const closedStream = new ReadableStream({
      start(controller) {
        controller.close();
      },
    });
    vi.mocked(fetch)
      .mockResolvedValueOnce(
        new Response(closedStream, {
          status: 200,
          headers: { 'Content-Type': 'text/event-stream' },
        })
      )
      .mockResolvedValue({ ok: false, body: null } as Response);

    render(<RunView runId={RUN_ID} />);

    expect(
      await screen.findByRole('button', { name: /search/i })
    ).toBeInTheDocument();
    await waitFor(() => expect(fetch).toHaveBeenCalledTimes(2), {
      timeout: 2_000,
    });
    expect(screen.getAllByRole('button', { name: /search/i })).toHaveLength(1);
  });

  it('clears the previous run while a new run is being hydrated', async () => {
    vi.mocked(getRun).mockResolvedValue(run({ status: 'completed' }));
    vi.mocked(listSteps).mockResolvedValue([persistedStep()]);
    const view = render(<RunView runId={RUN_ID} />);
    expect(
      await screen.findByRole('button', { name: /search/i })
    ).toBeInTheDocument();

    const nextRunId = '22222222-2222-4222-8222-222222222222';
    let resolveNext!: (value: RunResponse) => void;
    vi.mocked(getRun).mockReturnValue(
      new Promise((resolve) => {
        resolveNext = resolve;
      })
    );
    vi.mocked(listSteps).mockResolvedValue([]);
    view.rerender(<RunView runId={nextRunId} />);

    await waitFor(() =>
      expect(screen.queryByRole('button', { name: /search/i })).toBeNull()
    );
    resolveNext(run({ id: nextRunId, status: 'completed' }));
    expect(
      await screen.findByText(/no steps received yet/i)
    ).toBeInTheDocument();
  });

  it('reconstructs a missed screening-review pause and fetches its owned review', async () => {
    const review: PendingReviewResponse = {
      pending: true,
      descriptor: {
        run_id: RUN_ID,
        step_index: 1,
        stage_type: 'screen',
        review_kind: 'screening',
        contract_version: 1,
        output_hash: 'a'.repeat(64),
        status: 'pending',
      },
      stage_output: {
        screening: [
          {
            source_id: 'source-a',
            part_id: 'p0001',
            included: true,
            reason: 'Relevant population',
          },
        ],
      },
    };
    vi.mocked(getRun).mockResolvedValue(
      run({
        status: 'paused',
        pause_reason: 'review_required',
        review_kind: 'screening',
        step_index: 1,
        output_hash: 'a'.repeat(64),
      })
    );
    vi.mocked(listSteps).mockResolvedValue([
      persistedStep({
        output: {
          source_records: [
            {
              source_id: 'source-a',
              title: 'Persisted source title',
              abstract: 'Persisted source abstract.',
              evidence_level: 'abstract',
            },
          ],
        },
      }),
    ]);
    vi.mocked(getPendingReview).mockResolvedValue(review);

    render(<RunView runId={RUN_ID} />);

    expect(
      await screen.findByRole('heading', { name: /screening review/i })
    ).toBeInTheDocument();
    expect(screen.getByText('Persisted source title')).toBeInTheDocument();
    expect(getPendingReview).toHaveBeenCalledWith(RUN_ID);
    expect(
      screen.queryByRole('button', { name: /^resume$/i })
    ).not.toBeInTheDocument();
  });

  it('reconstructs a manual pause with only the ordinary resume action', async () => {
    vi.mocked(getRun).mockResolvedValue(
      run({ status: 'paused', pause_reason: 'user_paused' })
    );
    vi.mocked(listSteps).mockResolvedValue([persistedStep()]);

    render(<RunView runId={RUN_ID} />);

    const resume = await screen.findByRole('button', { name: /^resume$/i });
    expect(
      screen.queryByRole('button', {
        name: /continue with an unverified draft/i,
      })
    ).not.toBeInTheDocument();
    fireEvent.click(resume);
    await waitFor(() => expect(resumeRun).toHaveBeenCalledWith(RUN_ID));
  });

  it('keeps ordinary resume separate from exact-hash unverified continuation', async () => {
    vi.mocked(getRun).mockResolvedValue(
      run({
        status: 'paused',
        pause_reason: 'verification_failed',
        step_index: 4,
        output_hash: 'f'.repeat(64),
      })
    );
    vi.mocked(listSteps).mockResolvedValue([persistedStep()]);

    render(<RunView runId={RUN_ID} />);

    const continueButton = await screen.findByRole('button', {
      name: /continue with an unverified draft/i,
    });
    expect(
      screen.queryByRole('button', { name: /^resume$/i })
    ).not.toBeInTheDocument();
    fireEvent.click(continueButton);
    await waitFor(() =>
      expect(resumeRun).toHaveBeenCalledWith(RUN_ID, {
        continue_unverified: true,
        output_hash: 'f'.repeat(64),
      })
    );
  });
});
