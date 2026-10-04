import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import type { ReactElement } from 'react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { RunRerun, rerunReasonLabel } from '../RunRerun';
import {
  admitRerun,
  getRerunEligibility,
  listReruns,
  type Rerun,
  type RerunEligibility,
} from '@/services/researchEngineService';

vi.mock('@/services/researchEngineService', () => ({
  getRerunEligibility: vi.fn(),
  listReruns: vi.fn(),
  admitRerun: vi.fn(),
  cancelRerun: vi.fn(),
  retryRerun: vi.fn(),
  downloadRerunComparison: vi.fn(),
}));

const H = (c: string): string => c.repeat(64);
const OUTPUTS = ['fig.svg', 'metrics.json', 'table.csv'];
const DEFAULT_RULE = {
  schema: 'nous.rerun-rule/1',
  outputs: OUTPUTS.map((name) => ({ name, mode: 'bytes' })),
};

function wrap(node: ReactElement): ReturnType<typeof render> {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  return render(
    <QueryClientProvider client={client}>{node}</QueryClientProvider>
  );
}

const eligibility = (
  overrides: Partial<RerunEligibility> = {}
): RerunEligibility => ({
  eligible: true,
  reasons: [],
  default_rule: DEFAULT_RULE,
  ...overrides,
});

const rerun = (overrides: Partial<Rerun> = {}): Rerun => ({
  id: 'rerun-1',
  collection_id: 'project-1',
  run_id: 'run-1',
  manifest_id: 'manifest-1',
  manifest_hash: H('m'),
  rule: DEFAULT_RULE,
  rule_hash: H('r'),
  requested_by_id: 'user-1',
  created_at: '2026-10-01T00:00:00Z',
  attempts: [],
  ...overrides,
});

describe('RunRerun', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(listReruns).mockResolvedValue({ reruns: [] });
  });

  it('ineligible reasons listed and admit disabled', async () => {
    vi.mocked(getRerunEligibility).mockResolvedValue(
      eligibility({
        eligible: false,
        default_rule: null,
        reasons: [
          'no_manifest',
          'manifest_incomplete:environment.lock_sha256',
          'artifact_corrupt:data.csv',
        ],
      })
    );
    wrap(<RunRerun runId="run-1" />);
    expect(
      await screen.findByText('Not eligible for rerun')
    ).toBeInTheDocument();
    expect(
      screen.getByText('no run manifest (legacy run)')
    ).toBeInTheDocument();
    expect(
      screen.getByText('manifest missing: environment lock')
    ).toBeInTheDocument();
    expect(
      screen.getByText('archived file corrupt: data.csv')
    ).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Rerun' })).toBeDisabled();
    expect(rerunReasonLabel('input_mismatch:data.csv')).toBe(
      'restored input hash differs: data.csv'
    );
  });

  it('executed but not reproduced shows both badges', async () => {
    vi.mocked(getRerunEligibility).mockResolvedValue(eligibility());
    vi.mocked(listReruns).mockResolvedValue({
      reruns: [
        rerun({
          attempts: [
            {
              attempt: 1,
              status: 'restoration_failed',
              reproduction: null,
              reasons: ['environment_lock_mismatch'],
              environment_validation: {},
              input_validation: {},
              comparison: null,
              comparison_hash: null,
              outputs: [],
              finished_at: '2026-10-01T00:01:00Z',
            },
            {
              attempt: 2,
              status: 'executed',
              reproduction: 'not_reproduced',
              reasons: [],
              environment_validation: {},
              input_validation: {},
              comparison: [
                {
                  name: 'metrics.json',
                  mode: 'json_numeric',
                  expected_sha256: H('a'),
                  actual_sha256: H('b'),
                  equal: false,
                  numeric: [
                    {
                      pointer: '/mean_y',
                      expected: 5,
                      actual: 5.000001,
                      abs_diff: 0.000001,
                      within: false,
                    },
                  ],
                  reason: null,
                },
              ],
              comparison_hash: H('c'),
              outputs: [],
              finished_at: '2026-10-01T00:02:00Z',
            },
          ],
        }),
      ],
    });
    wrap(<RunRerun runId="run-1" />);
    expect(await screen.findByText('Attempt 2')).toBeInTheDocument();
    // Attempt 2 executed but did not reproduce: two separate results.
    expect(screen.getByText('Executed')).toBeInTheDocument();
    expect(screen.getByText('Not reproduced')).toBeInTheDocument();
    // Attempt 1 never executed, so reproduction does not apply.
    expect(screen.getByText('Not executed')).toBeInTheDocument();
    expect(screen.getByText('Reproduced: n/a')).toBeInTheDocument();
    expect(
      screen.getByText('environment lock differs after install')
    ).toBeInTheDocument();
    expect(
      screen.getByText('/mean_y: outside (Δ 0.000001)')
    ).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Retry' })).toBeNull();
  });

  it('rule table requires every output', async () => {
    vi.mocked(getRerunEligibility).mockResolvedValue(eligibility());
    vi.mocked(admitRerun).mockResolvedValue(rerun());
    wrap(<RunRerun runId="run-1" />);
    const mode = await screen.findByLabelText('Mode for metrics.json');
    const admit = screen.getByRole('button', { name: 'Rerun' });
    expect(admit).toBeEnabled();
    fireEvent.change(mode, { target: { value: 'json_numeric' } });
    // A numeric rule without a pointer cannot be declared.
    expect(admit).toBeDisabled();
    fireEvent.change(screen.getByLabelText('pointers for metrics.json'), {
      target: { value: '/mean_y' },
    });
    fireEvent.change(screen.getByLabelText('abs for metrics.json'), {
      target: { value: '1e-12' },
    });
    expect(admit).toBeEnabled();
    fireEvent.click(admit);
    await waitFor(() => expect(admitRerun).toHaveBeenCalledTimes(1));
    const [, body] = vi.mocked(admitRerun).mock.calls[0];
    expect(body.rule).toEqual({
      schema: 'nous.rerun-rule/1',
      outputs: [
        { name: 'fig.svg', mode: 'bytes' },
        {
          name: 'metrics.json',
          mode: 'json_numeric',
          pointers: ['/mean_y'],
          abs: 1e-12,
          rel: 0,
        },
        { name: 'table.csv', mode: 'bytes' },
      ],
    });
  });
});
