import { render, screen } from '@testing-library/react';
import { describe, expect, it } from 'vitest';
import type {
  RunResponse,
  StepResponse,
} from '@/services/researchEngineService';
import { RunResults } from '../RunResults';

const RUN_ID = '11111111-1111-4111-8111-111111111111';

function run(): RunResponse {
  return {
    id: RUN_ID,
    blueprint_id: '33333333-3333-4333-8333-333333333333',
    blueprint_version: 1,
    status: 'completed',
    total_tokens: 12,
    created_at: '2026-09-27T10:00:00Z',
    updated_at: '2026-09-27T10:00:01Z',
    completed_at: '2026-09-27T10:00:02Z',
  };
}

function exportStep(finalStatus: 'verified' | 'unverified'): StepResponse {
  return {
    id: 'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa',
    run_id: RUN_ID,
    step_index: 5,
    step_type: 'export',
    mode: 'deterministic',
    temperature: 0,
    token_count: 0,
    started_at: '2026-09-27T10:00:01Z',
    completed_at: '2026-09-27T10:00:02Z',
    output: {
      exported: {
        final_status: finalStatus,
        coverage: {
          partial: true,
          exhaustive: false,
          provider_results: [
            { provider: 'openalex', returned_count: 3 },
            {
              provider: 'crossref',
              status: 'failed',
              error_type: 'TimeoutError',
              returned_count: 0,
            },
          ],
        },
        included_source_ids: ['source-a'],
        screening: [
          { source_id: 'source-a', part_id: 'p0001' },
          { source_id: 'source-b', part_id: 'p0001' },
        ],
        reviews: [
          {
            review_kind: 'extraction',
            decision: 'approve',
            decision_payload: {
              items: [
                {
                  source_id: 'source-a',
                  part_id: 'p0001',
                  decision: 'accept',
                },
                {
                  source_id: 'source-b',
                  part_id: 'p0001',
                  decision: 'reject',
                  reason: 'No usable outcome',
                },
              ],
            },
          },
        ],
        verification: {
          claims: [
            { claim_id: 'c0001', status: 'supported' },
            { claim_id: 'c0002', status: 'failed' },
          ],
        },
        limitations: ['One provider was unavailable.'],
      },
      markdown: '# Daily brief',
    },
    quality_marks: [],
  };
}

describe('RunResults', () => {
  it('labels a verified brief and exposes accessible API download links', () => {
    render(<RunResults run={run()} steps={[exportStep('verified')]} />);

    expect(
      screen.getByRole('heading', { name: /verified brief/i })
    ).toBeInTheDocument();
    expect(screen.getByText('1 of 2 providers succeeded')).toBeInTheDocument();
    expect(screen.getByText('1 included source')).toBeInTheDocument();
    expect(screen.getByText('1 excluded source')).toBeInTheDocument();
    expect(screen.getByText('1 accepted extraction')).toBeInTheDocument();
    expect(screen.getByText('1 rejected extraction')).toBeInTheDocument();
    expect(screen.getByText('1 of 2 claims verified')).toBeInTheDocument();
    for (const [label, format] of [
      ['Markdown', 'markdown'],
      ['JSON', 'json'],
      ['CSV', 'csv'],
    ] as const) {
      expect(
        screen.getByRole('link', { name: `Download ${label}` })
      ).toHaveAttribute(
        'href',
        `/api/v1/research-engine/runs/${RUN_ID}/export?format=${format}`
      );
    }
  });

  it('keeps an overridden result visibly unverified in the result and downloads', () => {
    render(<RunResults run={run()} steps={[exportStep('unverified')]} />);

    expect(
      screen.getByRole('heading', { name: /unverified draft/i })
    ).toBeInTheDocument();
    expect(screen.getByRole('alert')).toHaveTextContent(
      /verification did not pass/i
    );
    expect(
      screen.getByRole('link', { name: /download unverified markdown/i })
    ).toHaveAttribute(
      'href',
      `/api/v1/research-engine/runs/${RUN_ID}/export?format=markdown`
    );
  });

  it('shows a no-evidence outcome with audit downloads but no research brief', () => {
    const screenStep: StepResponse = {
      id: 'bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb',
      run_id: RUN_ID,
      step_index: 1,
      step_type: 'screen',
      mode: 'deterministic',
      temperature: 0,
      token_count: 0,
      started_at: '2026-09-27T10:00:01Z',
      completed_at: '2026-09-27T10:00:02Z',
      output: {
        included_source_ids: [],
        screening: [],
        no_evidence_reason: 'No sources met the inclusion criteria.',
      },
      quality_marks: [],
    };
    render(<RunResults run={run()} steps={[screenStep]} />);

    expect(
      screen.getByRole('heading', { name: /no evidence brief/i })
    ).toBeInTheDocument();
    expect(
      screen.getByText(/no sources met the inclusion criteria/i)
    ).toBeInTheDocument();
    expect(
      screen.queryByRole('link', { name: /markdown/i })
    ).not.toBeInTheDocument();
    expect(
      screen.getByRole('link', { name: /download audit json/i })
    ).toHaveAttribute(
      'href',
      `/api/v1/research-engine/runs/${RUN_ID}/export?format=json`
    );
    expect(
      screen.getByRole('link', { name: /download extraction csv/i })
    ).toHaveAttribute(
      'href',
      `/api/v1/research-engine/runs/${RUN_ID}/export?format=csv`
    );
  });
});
