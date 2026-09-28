import {
  act,
  fireEvent,
  render,
  screen,
  waitFor,
  within,
} from '@testing-library/react';
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
  submitReview,
} from '@/services/researchEngineService';
import { useResearchEngineStore } from '@/store/research-engine-store';
import { APIErrorClass } from '@/types/api';
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
const OTHER_RUN_ID = '22222222-2222-4222-8222-222222222222';

type NonFinalReviewKind = 'screening' | 'extraction';
type ProjectionMismatch = 'missing' | 'run' | 'index' | 'stage' | 'hash';

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

function pendingScreeningReview(): PendingReviewResponse {
  return {
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
}

function acceptedScreeningReview(): PendingReviewResponse {
  const review = pendingScreeningReview();
  review.descriptor = {
    ...review.descriptor!,
    status: 'approved',
  };
  review.accepted_review = {
    id: '77777777-7777-4777-8777-777777777777',
    run_id: RUN_ID,
    step_index: 1,
    stage_type: 'screen',
    review_kind: 'screening',
    reviewer_id: '88888888-8888-4888-8888-888888888888',
    output_hash: 'a'.repeat(64),
    decision: 'approve',
    decision_payload: {
      items: [
        {
          source_id: 'source-a',
          part_id: 'p0001',
          decision: 'include',
        },
      ],
    },
    note: null,
    created_at: '2026-09-27T11:00:00Z',
    replay: false,
  };
  return review;
}

function projectedReview(
  reviewKind: NonFinalReviewKind,
  outputHash: string
): PendingReviewResponse {
  const screening = reviewKind === 'screening';
  return {
    pending: true,
    descriptor: {
      run_id: RUN_ID,
      step_index: screening ? 1 : 2,
      stage_type: screening ? 'screen' : 'extract',
      review_kind: reviewKind,
      contract_version: 1,
      output_hash: outputHash,
      status: 'pending',
    },
    stage_output: {
      contract_version: 1,
      stage_type: screening ? 'screen' : 'extract',
      review_projection: {
        projected: true,
        truncated: true,
        identity_complete: true,
      },
      ...(screening
        ? {
            screening: [
              {
                source_id: 'source-a',
                part_id: 'p0001',
                included: true,
              },
            ],
          }
        : {
            extractions: [{ source_id: 'source-a', part_id: 'p0001' }],
          }),
    },
  };
}

function projectedReviewStep(
  reviewKind: NonFinalReviewKind,
  outputHash: string,
  mismatch: ProjectionMismatch
): StepResponse | null {
  if (mismatch === 'missing') return null;
  const screening = reviewKind === 'screening';
  const expectedIndex = screening ? 1 : 2;
  const expectedStage = screening ? 'screen' : 'extract';
  return persistedStep({
    id: 'dddddddd-dddd-4ddd-8ddd-dddddddddddd',
    run_id: mismatch === 'run' ? OTHER_RUN_ID : RUN_ID,
    step_index: mismatch === 'index' ? expectedIndex + 1 : expectedIndex,
    step_type:
      mismatch === 'stage' ? (screening ? 'extract' : 'screen') : expectedStage,
    outputs_hash: mismatch === 'hash' ? 'e'.repeat(64) : outputHash,
    output: screening
      ? {
          contract_version: 1,
          stage_type: 'screen',
          screening: [
            {
              source_id: 'source-a',
              part_id: 'p0001',
              included: true,
              reason: 'This private persisted reason must remain hidden.',
            },
          ],
        }
      : {
          contract_version: 1,
          stage_type: 'extract',
          extractions: [
            {
              source_id: 'source-a',
              part_id: 'p0001',
              data: { private_result: 'must remain hidden' },
              evidence: [{ quote: 'Private mismatched evidence.' }],
            },
          ],
        },
  });
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

  it('refreshes after a clean stream end and stops when the durable run is paused', async () => {
    vi.mocked(getRun)
      .mockResolvedValueOnce(run())
      .mockResolvedValueOnce(
        run({ status: 'paused', pause_reason: 'user_paused' })
      );
    vi.mocked(listSteps).mockResolvedValue([persistedStep()]);
    const closedStream = new ReadableStream({
      start(controller) {
        controller.close();
      },
    });
    vi.mocked(fetch).mockResolvedValue(
      new Response(closedStream, {
        status: 200,
        headers: { 'Content-Type': 'text/event-stream' },
      })
    );

    render(<RunView runId={RUN_ID} />);

    expect(
      await screen.findByRole('button', { name: /search/i })
    ).toBeInTheDocument();
    expect(
      await screen.findByRole('button', { name: /^resume$/i })
    ).toBeInTheDocument();
    expect(getRun).toHaveBeenCalledTimes(2);
    expect(listSteps).toHaveBeenCalledTimes(2);
    expect(fetch).toHaveBeenCalledTimes(1);
    expect(screen.getAllByRole('button', { name: /search/i })).toHaveLength(1);
  });

  it('refreshes non-OK streams and stops with a safe error after bounded retries', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    try {
      vi.mocked(getRun).mockResolvedValue(run());
      vi.mocked(listSteps).mockResolvedValue([persistedStep()]);
      vi.mocked(fetch).mockResolvedValue(
        new Response(null, { status: 409, statusText: 'Conflict' })
      );

      render(<RunView runId={RUN_ID} />);

      expect(
        await screen.findByText(
          /live updates stopped after repeated connection failures/i,
          {},
          { timeout: 5_000 }
        )
      ).toBeInTheDocument();
      expect(fetch).toHaveBeenCalledTimes(3);
      expect(getRun).toHaveBeenCalledTimes(4);
      expect(listSteps).toHaveBeenCalledTimes(4);
    } finally {
      vi.useRealTimers();
    }
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

  it('fills a projected extraction review from its matching persisted step and keeps the pending hash', async () => {
    const outputHash = 'b'.repeat(64);
    vi.mocked(getRun).mockResolvedValue(
      run({
        status: 'paused',
        pause_reason: 'review_required',
        review_kind: 'extraction',
        step_index: 2,
        output_hash: outputHash,
      })
    );
    vi.mocked(listSteps).mockResolvedValue([
      persistedStep({
        output: {
          source_records: [
            {
              source_id: 'source-a',
              title: 'Persisted source title',
              evidence_level: 'full_text',
            },
          ],
        },
      }),
      persistedStep({
        id: 'bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb',
        step_index: 2,
        step_type: 'extract',
        outputs_hash: outputHash,
        output: {
          contract_version: 1,
          stage_type: 'extract',
          extractions: [
            {
              source_id: 'source-a',
              part_id: 'p0001',
              data: { effect: 'positive' },
              evidence: [
                {
                  quote: 'The persisted effect was positive.',
                  page_reference: '7',
                },
              ],
            },
          ],
        },
      }),
    ]);
    vi.mocked(getPendingReview).mockResolvedValue({
      pending: true,
      descriptor: {
        run_id: RUN_ID,
        step_index: 2,
        stage_type: 'extract',
        review_kind: 'extraction',
        contract_version: 1,
        output_hash: outputHash,
        status: 'pending',
      },
      stage_output: {
        contract_version: 1,
        stage_type: 'extract',
        review_projection: {
          projected: true,
          truncated: true,
          identity_complete: true,
        },
        extractions: [{ source_id: 'source-a', part_id: 'p0001' }],
      },
    });
    vi.mocked(submitReview).mockResolvedValue({
      id: '99999999-9999-4999-8999-999999999999',
      run_id: RUN_ID,
      step_index: 2,
      stage_type: 'extract',
      review_kind: 'extraction',
      output_hash: outputHash,
      decision: 'approve',
      decision_payload: {
        items: [
          {
            source_id: 'source-a',
            part_id: 'p0001',
            decision: 'accept',
          },
        ],
      },
      reviewer_id: '88888888-8888-4888-8888-888888888888',
      created_at: '2026-09-27T11:00:00Z',
      replay: false,
    });

    render(<RunView runId={RUN_ID} />);

    expect(
      await screen.findByText('The persisted effect was positive.')
    ).toBeInTheDocument();
    expect(screen.getByText(/"effect": "positive"/i)).toBeInTheDocument();
    const group = screen.getByRole('group', {
      name: /extraction from persisted source title, part p0001/i,
    });
    fireEvent.click(within(group).getByRole('radio', { name: /accept/i }));
    fireEvent.click(
      screen.getByRole('button', { name: /approve extraction review/i })
    );
    await waitFor(() =>
      expect(submitReview).toHaveBeenCalledWith(
        RUN_ID,
        2,
        expect.objectContaining({ output_hash: outputHash })
      )
    );
  });

  it('uses the matching full export step for a projected final review', async () => {
    const outputHash = 'c'.repeat(64);
    vi.mocked(getRun).mockResolvedValue(
      run({
        status: 'paused',
        pause_reason: 'review_required',
        review_kind: 'final',
        step_index: 5,
        output_hash: outputHash,
      })
    );
    vi.mocked(listSteps).mockResolvedValue([
      persistedStep({
        id: 'cccccccc-cccc-4ccc-8ccc-cccccccccccc',
        step_index: 5,
        step_type: 'export',
        outputs_hash: outputHash,
        output: {
          contract_version: 1,
          stage_type: 'export',
          exported: {
            final_status: 'verified',
            verification: {
              claims: [{ claim_id: 'c0001', status: 'supported' }],
            },
          },
          markdown: '# Full persisted brief',
        },
      }),
    ]);
    vi.mocked(getPendingReview).mockResolvedValue({
      pending: true,
      descriptor: {
        run_id: RUN_ID,
        step_index: 5,
        stage_type: 'export',
        review_kind: 'final',
        contract_version: 1,
        output_hash: outputHash,
        status: 'pending',
      },
      stage_output: {
        contract_version: 1,
        stage_type: 'export',
        review_projection: {
          projected: true,
          truncated: true,
          identity_complete: true,
        },
        format: 'markdown',
      },
    });

    render(<RunView runId={RUN_ID} />);

    expect(
      await screen.findByText('# Full persisted brief')
    ).toBeInTheDocument();
    expect(
      screen.getByRole('button', { name: /approve final review/i })
    ).toBeEnabled();
    expect(
      screen.queryByText(/cannot receive final approval/i)
    ).not.toBeInTheDocument();
  });

  it.each(
    (['screening', 'extraction'] as const).flatMap((reviewKind) =>
      (['missing', 'run', 'index', 'stage', 'hash'] as const).map(
        (mismatch) => [reviewKind, mismatch] as const
      )
    )
  )(
    'blocks an unresolved projected %s review with a %s persisted-step match',
    async (reviewKind, mismatch) => {
      const outputHash = 'd'.repeat(64);
      const screening = reviewKind === 'screening';
      const candidate = projectedReviewStep(reviewKind, outputHash, mismatch);
      vi.mocked(getRun).mockResolvedValue(
        run({
          status: 'paused',
          pause_reason: 'review_required',
          review_kind: reviewKind,
          step_index: screening ? 1 : 2,
          output_hash: outputHash,
        })
      );
      vi.mocked(listSteps).mockResolvedValue(candidate ? [candidate] : []);
      vi.mocked(getPendingReview).mockResolvedValue(
        projectedReview(reviewKind, outputHash)
      );

      render(<RunView runId={RUN_ID} />);

      const group = await screen.findByRole('group', {
        name: screening ? /source-a/i : /extraction from source-a/i,
      });
      fireEvent.click(
        within(group).getByRole('radio', {
          name: screening ? /include/i : /accept/i,
        })
      );
      const approve = screen.getByRole('button', {
        name: new RegExp(`approve ${reviewKind} review`, 'i'),
      });
      expect(approve).toBeDisabled();
      fireEvent.click(approve);
      expect(submitReview).not.toHaveBeenCalled();
      expect(
        screen.getByText(
          /complete persisted review output could not be verified/i
        )
      ).toBeInTheDocument();
      expect(
        screen.queryByText(
          /private persisted reason|private mismatched evidence/i
        )
      ).not.toBeInTheDocument();
    }
  );

  it('does not claim a stale review was refreshed when durable reload fails', async () => {
    vi.mocked(getRun)
      .mockResolvedValueOnce(
        run({
          status: 'paused',
          pause_reason: 'review_required',
          review_kind: 'screening',
          step_index: 1,
          output_hash: 'a'.repeat(64),
        })
      )
      .mockRejectedValueOnce(new Error('internal reload failure'));
    vi.mocked(listSteps).mockResolvedValue([
      persistedStep({
        output: {
          source_records: [
            {
              source_id: 'source-a',
              title: 'Persisted source title',
              evidence_level: 'abstract',
            },
          ],
        },
      }),
    ]);
    vi.mocked(getPendingReview).mockResolvedValue(pendingScreeningReview());
    vi.mocked(submitReview).mockRejectedValue(
      new APIErrorClass({
        message: 'stale output',
        status_code: 409,
        type: 'http_error',
      })
    );

    render(<RunView runId={RUN_ID} />);

    const group = await screen.findByRole('group', {
      name: /persisted source title/i,
    });
    fireEvent.click(within(group).getByRole('radio', { name: /include/i }));
    fireEvent.click(
      screen.getByRole('button', { name: /approve screening review/i })
    );

    const alert = await screen.findByRole('alert', {
      name: /review changed/i,
    });
    expect(alert).toHaveTextContent(/reload the page/i);
    expect(alert).not.toHaveTextContent(/has been refreshed/i);
  });

  it('keeps the stale-review notice visible while replacing the durable review', async () => {
    const changedReview = pendingScreeningReview();
    changedReview.descriptor = {
      ...changedReview.descriptor!,
      output_hash: 'b'.repeat(64),
    };
    changedReview.stage_output = {
      screening: [
        {
          source_id: 'source-a',
          part_id: 'p0001',
          included: true,
          reason: 'Server-side revised reason',
        },
      ],
    };
    vi.mocked(getRun)
      .mockResolvedValueOnce(
        run({
          status: 'paused',
          pause_reason: 'review_required',
          review_kind: 'screening',
          step_index: 1,
          output_hash: 'a'.repeat(64),
        })
      )
      .mockResolvedValueOnce(
        run({
          status: 'paused',
          pause_reason: 'review_required',
          review_kind: 'screening',
          step_index: 1,
          output_hash: 'b'.repeat(64),
        })
      );
    vi.mocked(listSteps).mockResolvedValue([
      persistedStep({
        output: {
          source_records: [
            {
              source_id: 'source-a',
              title: 'Persisted source title',
              evidence_level: 'abstract',
            },
          ],
        },
      }),
    ]);
    vi.mocked(getPendingReview)
      .mockResolvedValueOnce(pendingScreeningReview())
      .mockResolvedValueOnce(changedReview);
    vi.mocked(submitReview).mockRejectedValue(
      new APIErrorClass({
        message: 'stale output',
        status_code: 409,
        type: 'http_error',
      })
    );

    render(<RunView runId={RUN_ID} />);

    const group = await screen.findByRole('group', {
      name: /persisted source title/i,
    });
    fireEvent.click(within(group).getByRole('radio', { name: /include/i }));
    fireEvent.click(
      screen.getByRole('button', { name: /approve screening review/i })
    );

    expect(
      await screen.findByRole('alert', { name: /review changed/i })
    ).toHaveTextContent(/current review has been refreshed/i);
    expect(screen.getByText(/server-side revised reason/i)).toBeInTheDocument();
    expect(
      within(
        screen.getByRole('group', { name: /persisted source title/i })
      ).getByRole('radio', { name: /unresolved/i })
    ).toBeChecked();
  });

  it('reports an accepted review whose durable reload failed', async () => {
    vi.mocked(getRun)
      .mockResolvedValueOnce(
        run({
          status: 'paused',
          pause_reason: 'review_required',
          review_kind: 'screening',
          step_index: 1,
          output_hash: 'a'.repeat(64),
        })
      )
      .mockRejectedValueOnce(new Error('internal reload failure'));
    vi.mocked(listSteps).mockResolvedValue([
      persistedStep({
        output: {
          source_records: [
            {
              source_id: 'source-a',
              title: 'Persisted source title',
              evidence_level: 'abstract',
            },
          ],
        },
      }),
    ]);
    vi.mocked(getPendingReview).mockResolvedValue(pendingScreeningReview());
    vi.mocked(submitReview).mockResolvedValue({
      id: '99999999-9999-4999-8999-999999999999',
      run_id: RUN_ID,
      step_index: 1,
      stage_type: 'screen',
      review_kind: 'screening',
      output_hash: 'a'.repeat(64),
      decision: 'approve',
      decision_payload: {
        items: [
          {
            source_id: 'source-a',
            part_id: 'p0001',
            decision: 'include',
          },
        ],
      },
      reviewer_id: '88888888-8888-4888-8888-888888888888',
      created_at: '2026-09-27T11:00:00Z',
      replay: false,
    });

    render(<RunView runId={RUN_ID} />);

    const group = await screen.findByRole('group', {
      name: /persisted source title/i,
    });
    fireEvent.click(within(group).getByRole('radio', { name: /include/i }));
    fireEvent.click(
      screen.getByRole('button', { name: /approve screening review/i })
    );

    expect(
      await screen.findByRole('alert', { name: /review accepted/i })
    ).toHaveTextContent(/latest run state could not be loaded/i);
  });

  it('claims and completes a manual resume after the refresh still reports user_paused', async () => {
    const userPaused = run({ status: 'paused', pause_reason: 'user_paused' });
    vi.mocked(getRun)
      .mockResolvedValueOnce(userPaused)
      // POST /resume only mints the one-use authorization. Until the SSE GET
      // claims it, durable state still carries the original manual pause.
      .mockResolvedValueOnce(userPaused)
      .mockResolvedValue(run({ status: 'completed' }));
    vi.mocked(listSteps).mockResolvedValue([persistedStep()]);
    vi.mocked(resumeRun).mockResolvedValue(userPaused);
    const encoder = new TextEncoder();
    vi.mocked(fetch).mockResolvedValue(
      new Response(
        new ReadableStream({
          start(controller) {
            controller.enqueue(
              encoder.encode(
                `event: run_started\ndata: ${JSON.stringify({ run_id: RUN_ID })}\n\n` +
                  `event: run_complete\ndata: ${JSON.stringify({
                    run_id: RUN_ID,
                    final_status: 'verified',
                  })}\n\n`
              )
            );
            controller.close();
          },
        }),
        {
          status: 200,
          headers: { 'Content-Type': 'text/event-stream' },
        }
      )
    );

    render(<RunView runId={RUN_ID} />);

    const resume = await screen.findByRole('button', { name: /^resume$/i });
    expect(
      screen.queryByRole('button', {
        name: /continue with an unverified draft/i,
      })
    ).not.toBeInTheDocument();
    fireEvent.click(resume);
    await waitFor(() => expect(resumeRun).toHaveBeenCalledWith(RUN_ID));
    await waitFor(() =>
      expect(fetch).toHaveBeenCalledWith(
        `/api/v1/research-engine/runs/${RUN_ID}/stream`,
        expect.any(Object)
      )
    );
    await waitFor(() =>
      expect(useResearchEngineStore.getState().activeRun?.status).toBe(
        'completed'
      )
    );
    expect(useResearchEngineStore.getState().finalStatus).toBe('verified');
  });

  it('opens the stream after an approved-review resume stays durably paused until claimed', async () => {
    const approvedPause = run({
      status: 'paused',
      pause_reason: 'review_required',
      review_kind: 'screening',
      step_index: 1,
      output_hash: 'a'.repeat(64),
    });
    const authorizedPause = run({
      status: 'paused',
      pause_reason: null,
      review_kind: null,
      step_index: null,
      output_hash: null,
    });
    vi.mocked(getRun)
      .mockResolvedValueOnce(approvedPause)
      .mockResolvedValueOnce(authorizedPause)
      .mockResolvedValue(
        run({
          status: 'paused',
          pause_reason: 'review_required',
          review_kind: 'extraction',
          step_index: 2,
          output_hash: 'b'.repeat(64),
        })
      );
    vi.mocked(listSteps).mockResolvedValue([persistedStep()]);
    vi.mocked(getPendingReview)
      .mockResolvedValueOnce(acceptedScreeningReview())
      .mockResolvedValueOnce(projectedReview('extraction', 'b'.repeat(64)));
    vi.mocked(resumeRun).mockResolvedValue(authorizedPause);
    const encoder = new TextEncoder();
    let streamController!: ReadableStreamDefaultController<Uint8Array>;
    let streamSignal!: AbortSignal;
    vi.mocked(fetch).mockImplementation(async (_input, init) => {
      streamSignal = init?.signal as AbortSignal;
      return new Response(
        new ReadableStream({
          start(controller) {
            streamController = controller;
          },
        }),
        {
          status: 200,
          headers: { 'Content-Type': 'text/event-stream' },
        }
      );
    });

    render(<RunView runId={RUN_ID} />);

    fireEvent.click(
      await screen.findByRole('button', { name: /resume approved run/i })
    );
    await waitFor(() => expect(resumeRun).toHaveBeenCalledWith(RUN_ID));
    await waitFor(() =>
      expect(fetch).toHaveBeenCalledWith(
        `/api/v1/research-engine/runs/${RUN_ID}/stream`,
        expect.any(Object)
      )
    );

    await act(async () => {
      streamController.enqueue(
        encoder.encode(
          `event: run_started\ndata: ${JSON.stringify({ run_id: RUN_ID })}\n\n`
        )
      );
    });
    await waitFor(() =>
      expect(useResearchEngineStore.getState().activeRun?.status).toBe(
        'running'
      )
    );

    // Clearing the one-use resume authorization on run_started must not tear
    // down the stream that claimed it; durable running state now owns it.
    expect(streamSignal.aborted).toBe(false);
    expect(fetch).toHaveBeenCalledTimes(1);

    await act(async () => {
      streamController.enqueue(
        encoder.encode(
          `event: run_paused\ndata: ${JSON.stringify({
            run_id: RUN_ID,
            pause_reason: 'review_required',
            review_kind: 'extraction',
            step_index: 2,
            output_hash: 'b'.repeat(64),
          })}\n\n`
        )
      );
      streamController.close();
    });
    await waitFor(() =>
      expect(vi.mocked(getRun).mock.calls.length).toBeGreaterThanOrEqual(3)
    );
    expect(fetch).toHaveBeenCalledTimes(1);
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
