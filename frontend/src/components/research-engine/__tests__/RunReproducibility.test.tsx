import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { render, screen } from '@testing-library/react';
import type { ReactElement } from 'react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { FigureLineage } from '../FigureLineage';
import { RunReproducibility, missingLabel } from '../RunReproducibility';
import {
  getFigureLineage,
  getManifestV2,
  type FigureLineage as FigureLineageData,
  type RunManifestV2,
} from '@/services/researchEngineService';

vi.mock('@/services/researchEngineService', () => ({
  getManifestV2: vi.fn(),
  downloadManifestV2: vi.fn(),
  listFigures: vi.fn(),
  registerFigure: vi.fn(),
  getFigureLineage: vi.fn(),
}));

const H = (c: string): string => c.repeat(64);

function wrap(node: ReactElement): ReturnType<typeof render> {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  return render(
    <QueryClientProvider client={client}>{node}</QueryClientProvider>
  );
}

const manifest = (overrides: Partial<RunManifestV2> = {}): RunManifestV2 => ({
  schema: 'nous.run-manifest/2',
  manifest: {
    code: { sha256: H('c') },
    environment: {
      template_id: 'code-interpreter-v1',
      python: 'Python 3.11.9',
      lock_sha256: H('e'),
    },
    inputs: [{ name: 'data.csv', kind: 'document', sha256: H('a') }],
    outputs: [
      { name: 'fig.svg', role: 'figure', sha256: H('b'), artifact_id: 'art-1' },
    ],
  },
  manifest_hash: H('f'),
  completeness: 'complete',
  missing: [],
  legacy: null,
  ...overrides,
});

describe('RunReproducibility', () => {
  beforeEach(() => vi.clearAllMocks());

  it('incomplete manifest never shows complete', async () => {
    vi.mocked(getManifestV2).mockResolvedValue(
      manifest({
        completeness: 'incomplete',
        missing: [
          'environment.lock_sha256',
          'outputs[0].artifact_id',
          'status',
        ],
      })
    );
    wrap(<RunReproducibility runId="run-1" projectId="project-1" />);
    expect(
      await screen.findByText(
        'Reproducibility: incomplete (missing: environment lock, output 1 retained file, run did not complete)'
      )
    ).toBeInTheDocument();
    expect(screen.queryByText('Reproducibility: complete')).toBeNull();
  });

  it('a "complete" label with a missing entry still reads incomplete', async () => {
    vi.mocked(getManifestV2).mockResolvedValue(
      manifest({ completeness: 'complete', missing: ['seed'] })
    );
    wrap(<RunReproducibility runId="run-1" />);
    expect(
      await screen.findByText('Reproducibility: incomplete (missing: seed)')
    ).toBeInTheDocument();
  });

  it('complete manifest shows hashes and the register action', async () => {
    vi.mocked(getManifestV2).mockResolvedValue(manifest());
    wrap(<RunReproducibility runId="run-1" projectId="project-1" />);
    expect(
      await screen.findByText('Reproducibility: complete')
    ).toBeInTheDocument();
    expect(screen.getByText('aaaaaaaaaaaa')).toBeInTheDocument();
    expect(screen.getByText('bbbbbbbbbbbb')).toBeInTheDocument();
    expect(screen.getByText('code-interpreter-v1')).toBeInTheDocument();
    expect(
      screen.getByRole('button', { name: 'Register as figure' })
    ).toBeInTheDocument();
  });

  it('legacy run shows incomplete without hashes', async () => {
    vi.mocked(getManifestV2).mockResolvedValue({
      schema: 'nous.run-manifest/1',
      manifest: null,
      manifest_hash: null,
      completeness: 'incomplete',
      missing: ['schema_version<2'],
      legacy: { run_status: 'completed', total_tokens: 4 },
    });
    wrap(<RunReproducibility runId="run-1" projectId="project-1" />);
    expect(
      await screen.findByText(
        'Reproducibility: incomplete (missing: recorded before run manifests (legacy run))'
      )
    ).toBeInTheDocument();
    expect(screen.queryByRole('table')).toBeNull();
    expect(screen.queryByText('Environment')).toBeNull();
    expect(
      screen.queryByRole('button', { name: 'Download manifest' })
    ).toBeNull();
  });

  it('labels every missing path', () => {
    expect(missingLabel('inputs[1].sha256')).toBe('input 2 hash');
    expect(missingLabel('question_version_id')).toBe(
      'hypothesis (question version)'
    );
    expect(missingLabel('something.new')).toBe('something.new');
  });
});

describe('FigureLineage', () => {
  it('figure lineage renders run, code, environment, data, protocol', async () => {
    const lineage: FigureLineageData = {
      figure: {
        id: 'fig-id',
        collection_id: 'project-1',
        figure_key: 'fig-1',
        kind: 'figure',
        caption: 'Mean y',
        output_artifact_id: 'art-1',
        run_id: '12345678-run',
        manifest_id: 'manifest-1',
        supersedes_figure_id: null,
        created_by_id: 'user-1',
        created_at: '2026-10-01T00:00:00Z',
        superseded: false,
        stale: false,
      },
      output: {
        id: 'art-1',
        run_id: '12345678-run',
        role: 'output',
        name: 'fig.svg',
        media_type: 'image/svg+xml',
        sha256: H('b'),
        byte_size: 171,
      },
      run_id: '12345678-run',
      run_status: 'completed',
      manifest_id: 'manifest-1',
      manifest_hash: H('f'),
      completeness: 'complete',
      missing: [],
      code: { sha256: H('c') },
      environment: { template_id: 'code-interpreter-v1', lock_sha256: H('e') },
      inputs: [{ name: 'data.csv', kind: 'document', sha256: H('a') }],
      parameters: {},
      seed: 7,
      question_version_id: H('1'),
      hypothesis_sha256: H('2'),
      protocol_version_id: H('3'),
      protocol_content_hash: H('4'),
      effective_plan_hash: H('5'),
    };
    vi.mocked(getFigureLineage).mockResolvedValue(lineage);
    wrap(<FigureLineage projectId="project-1" figureId="fig-id" />);
    expect(
      await screen.findByText('Reproducibility: complete')
    ).toBeInTheDocument();
    expect(screen.getByText('fig.svg bbbbbbbbbbbb')).toBeInTheDocument();
    expect(screen.getByText('12345678 (completed)')).toBeInTheDocument();
    expect(screen.getByText('cccccccccccc')).toBeInTheDocument();
    expect(
      screen.getByText('code-interpreter-v1 lock eeeeeeeeeeee')
    ).toBeInTheDocument();
    expect(screen.getByText('data.csv aaaaaaaaaaaa')).toBeInTheDocument();
    expect(screen.getByText('222222222222')).toBeInTheDocument();
    expect(screen.getByText('333333333333')).toBeInTheDocument();
  });
});
