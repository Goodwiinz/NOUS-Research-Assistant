import {
  fireEvent,
  render,
  screen,
  waitFor,
  within,
} from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import type { PendingReviewResponse } from '@/services/researchEngineService';
import { resumeRun, submitReview } from '@/services/researchEngineService';
import { APIErrorClass } from '@/types/api';
import { ReviewPanel } from '../ReviewPanel';

vi.mock('@/services/researchEngineService', async (importOriginal) => {
  const original =
    await importOriginal<typeof import('@/services/researchEngineService')>();
  return {
    ...original,
    submitReview: vi.fn(),
    resumeRun: vi.fn(),
  };
});

const RUN_ID = '11111111-1111-4111-8111-111111111111';

function screeningReview(): PendingReviewResponse {
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
          reason: 'Machine recommendation: population matches.',
        },
      ],
    },
    validation: {
      item_decisions: ['include', 'exclude', 'unresolved'],
      reason_required_for: ['exclude'],
    },
  };
}

describe('ReviewPanel', () => {
  beforeEach(() => {
    vi.resetAllMocks();
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
            decision: 'exclude',
            reason: 'Wrong population',
          },
        ],
      },
      reviewer_id: '88888888-8888-4888-8888-888888888888',
      created_at: '2026-09-27T11:00:00Z',
      replay: false,
    });
  });

  it('shows original screening evidence and keeps local drafts explicitly ephemeral', () => {
    render(
      <ReviewPanel
        runId={RUN_ID}
        review={screeningReview()}
        sourceRecords={[
          {
            source_id: 'source-a',
            title: 'A Test Paper',
            authors: ['Ada Lovelace', 'Grace Hopper'],
            abstract: 'Original persisted abstract.',
            evidence_level: 'abstract',
            venue: 'Journal of Test Evidence',
            published_at: '2025-04-03',
            doi: '10.1000/test-paper',
            url: 'https://example.test/paper',
          },
        ]}
        onRefresh={vi.fn()}
      />
    );

    expect(screen.getByText('A Test Paper')).toBeInTheDocument();
    expect(
      screen.getByText('Original persisted abstract.')
    ).toBeInTheDocument();
    expect(screen.getByText(/evidence level: abstract/i)).toBeInTheDocument();
    expect(screen.getByText(/Ada Lovelace, Grace Hopper/)).toBeInTheDocument();
    expect(screen.getByText(/Journal of Test Evidence/)).toBeInTheDocument();
    expect(screen.getByText(/2025/)).toBeInTheDocument();
    expect(screen.getByText(/10\.1000\/test-paper/)).toBeInTheDocument();
    expect(
      screen.getByText(/choices on this page are drafts until you submit/i)
    ).toBeInTheDocument();
    expect(
      screen.queryByText(/draft choices (were|are) saved/i)
    ).not.toBeInTheDocument();
  });

  it('blocks unresolved approval and requires a reason for exclusions', async () => {
    const onRefresh = vi.fn();
    render(
      <ReviewPanel
        runId={RUN_ID}
        review={screeningReview()}
        sourceRecords={[
          {
            source_id: 'source-a',
            title: 'A Test Paper',
            evidence_level: 'abstract',
          },
        ]}
        onRefresh={onRefresh}
      />
    );

    const group = screen.getByRole('group', { name: /a test paper/i });
    const approve = screen.getByRole('button', {
      name: /approve screening review/i,
    });
    expect(approve).toBeDisabled();

    fireEvent.click(within(group).getByRole('radio', { name: /exclude/i }));
    expect(approve).toBeDisabled();
    fireEvent.change(
      within(group).getByRole('textbox', { name: /reason for exclusion/i }),
      { target: { value: 'Wrong population' } }
    );
    expect(approve).toBeEnabled();

    fireEvent.click(approve);
    await waitFor(() =>
      expect(submitReview).toHaveBeenCalledWith(RUN_ID, 1, {
        review_kind: 'screening',
        output_hash: 'a'.repeat(64),
        decision: 'approve',
        decision_payload: {
          items: [
            {
              source_id: 'source-a',
              part_id: 'p0001',
              decision: 'exclude',
              reason: 'Wrong population',
            },
          ],
        },
      })
    );
    expect(onRefresh).toHaveBeenCalled();
  });

  it('renders original extraction data and quoted evidence', () => {
    const review: PendingReviewResponse = {
      pending: true,
      descriptor: {
        run_id: RUN_ID,
        step_index: 2,
        stage_type: 'extract',
        review_kind: 'extraction',
        contract_version: 1,
        output_hash: 'b'.repeat(64),
        status: 'pending',
      },
      stage_output: {
        extractions: [
          {
            source_id: 'source-a',
            part_id: 'p0001',
            data: { effect: 'positive' },
            evidence: [
              {
                pointer: '/effect',
                quote: 'The effect was positive.',
                page_reference: '4',
              },
            ],
          },
        ],
      },
    };

    render(
      <ReviewPanel
        runId={RUN_ID}
        review={review}
        sourceRecords={[
          {
            source_id: 'source-a',
            title: 'A Test Paper',
            evidence_level: 'full_text',
          },
        ]}
        onRefresh={vi.fn()}
      />
    );

    expect(screen.getByText(/"effect": "positive"/i)).toBeInTheDocument();
    expect(screen.getByText('The effect was positive.')).toBeInTheDocument();
    expect(screen.getByText(/page 4/i)).toBeInTheDocument();
    expect(screen.getByText(/evidence level: full text/i)).toBeInTheDocument();

    const group = screen.getByRole('group', {
      name: /extraction from a test paper, part p0001/i,
    });
    const approve = screen.getByRole('button', {
      name: /approve extraction review/i,
    });
    fireEvent.click(within(group).getByRole('radio', { name: /reject/i }));
    expect(approve).toBeDisabled();
    fireEvent.change(
      within(group).getByRole('textbox', { name: /reason for rejection/i }),
      { target: { value: 'Evidence does not support the extracted value' } }
    );
    expect(approve).toBeEnabled();
  });

  it('refreshes authoritative review state after a stale hash conflict', async () => {
    const onRefresh = vi.fn().mockResolvedValue(undefined);
    vi.mocked(submitReview).mockRejectedValue(
      new APIErrorClass({
        message: 'stale output',
        status_code: 409,
        type: 'http_error',
      })
    );
    render(
      <ReviewPanel
        runId={RUN_ID}
        review={screeningReview()}
        sourceRecords={[
          {
            source_id: 'source-a',
            title: 'A Test Paper',
            evidence_level: 'abstract',
          },
        ]}
        onRefresh={onRefresh}
      />
    );

    const group = screen.getByRole('group', { name: /a test paper/i });
    fireEvent.click(within(group).getByRole('radio', { name: /include/i }));
    fireEvent.click(
      screen.getByRole('button', { name: /approve screening review/i })
    );

    expect(
      await screen.findByRole('alert', { name: /review changed/i })
    ).toHaveTextContent(/refreshed/i);
    expect(onRefresh).toHaveBeenCalled();
  });

  it('records a decline without resuming the paused run', async () => {
    vi.mocked(submitReview).mockResolvedValue({
      id: '99999999-9999-4999-8999-999999999999',
      run_id: RUN_ID,
      step_index: 1,
      stage_type: 'screen',
      review_kind: 'screening',
      output_hash: 'a'.repeat(64),
      decision: 'decline',
      decision_payload: {
        items: [
          {
            source_id: 'source-a',
            part_id: 'p0001',
            decision: 'unresolved',
          },
        ],
      },
      reviewer_id: '88888888-8888-4888-8888-888888888888',
      created_at: '2026-09-27T11:00:00Z',
      replay: false,
    });
    render(
      <ReviewPanel
        runId={RUN_ID}
        review={screeningReview()}
        sourceRecords={[
          {
            source_id: 'source-a',
            title: 'A Test Paper',
            evidence_level: 'abstract',
          },
        ]}
        onRefresh={vi.fn()}
      />
    );

    fireEvent.click(screen.getByRole('button', { name: /decline review/i }));
    await waitFor(() =>
      expect(submitReview).toHaveBeenCalledWith(
        RUN_ID,
        1,
        expect.objectContaining({ decision: 'decline' })
      )
    );
    expect(resumeRun).not.toHaveBeenCalled();
  });

  it.each(['unverified', 'failed'])(
    'does not offer final approval for a %s artifact',
    (finalStatus) => {
      const review: PendingReviewResponse = {
        pending: true,
        descriptor: {
          run_id: RUN_ID,
          step_index: 5,
          stage_type: 'export',
          review_kind: 'final',
          contract_version: 1,
          output_hash: 'c'.repeat(64),
          status: 'pending',
        },
        stage_output: {
          exported: { final_status: finalStatus },
          markdown: '# Draft',
        },
      };

      render(
        <ReviewPanel
          runId={RUN_ID}
          review={review}
          sourceRecords={[]}
          onRefresh={vi.fn()}
        />
      );

      expect(screen.getByRole('alert')).toHaveTextContent(
        /cannot receive final approval/i
      );
      expect(
        screen.queryByRole('button', { name: /approve final review/i })
      ).not.toBeInTheDocument();
    }
  );
});
