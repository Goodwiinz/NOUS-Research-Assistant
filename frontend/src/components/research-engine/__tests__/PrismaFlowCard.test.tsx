import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { PrismaFlowCard } from '../PrismaFlowCard';
import {
  downloadPrismaFlow,
  getPrismaFlow,
  type PrismaFlow,
} from '@/services/researchEngineService';

vi.mock('@/services/researchEngineService', () => ({
  getPrismaFlow: vi.fn(),
  downloadPrismaFlow: vi.fn(),
}));

const flow: PrismaFlow = {
  schema: 'nous.academic.prisma-flow.v1',
  generated_at: '2026-09-29T00:00:00Z',
  body_sha256: 'abcdef0123456789'.repeat(4),
  body: {
    counts: {
      records_identified: 13,
      records_by_source: { openalex: 9, pubmed: 3 },
      records_by_import: { Embase: 1 },
      import_rejected: 1,
      duplicates_removed: 4,
      unique_reports: 9,
      records_screened: 8,
      records_excluded: 2,
      records_awaiting_screening: 1,
      reports_sought: 5,
      reports_not_retrieved: 1,
      reports_awaiting_retrieval: 1,
      reports_assessed: 3,
      reports_excluded_by_reason: { 'wrong design': 1 },
      included_reports: 2,
      included_studies: 1,
      unconfirmed_study_links: 0,
    },
    excluded_from_flow: {},
    amendments: [],
    warnings: [],
    checks: {
      screened_plus_awaiting_equals_unique: true,
      assessed_within_retrieved: true,
      included_plus_excluded_equals_assessed: true,
    },
    versions: { corpus_hash: 'h', protocol_version_ids: [], stream_heads: {} },
  },
};

function renderCard(): void {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  render(
    <QueryClientProvider client={client}>
      <PrismaFlowCard projectId="p1" />
    </QueryClientProvider>
  );
}

describe('PrismaFlowCard', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(getPrismaFlow).mockResolvedValue(flow);
    vi.mocked(downloadPrismaFlow).mockResolvedValue();
  });

  it('renders the server-derived counts', async () => {
    renderCard();

    expect(await screen.findByText('Records identified')).toBeInTheDocument();
    expect(screen.getByText('openalex 9 · pubmed 3')).toBeInTheDocument();
    expect(screen.getByText('Embase 1')).toBeInTheDocument();
    expect(screen.getByText('wrong design 1')).toBeInTheDocument();
    expect(screen.getByText('Reports sought').nextSibling).toHaveTextContent(
      '5'
    );
    expect(screen.getByText('Export hash abcdef012345')).toBeInTheDocument();
    expect(getPrismaFlow).toHaveBeenCalledWith('p1');
  });

  it('downloads the Markdown export', async () => {
    renderCard();

    await userEvent.click(
      await screen.findByRole('button', { name: 'Download Markdown' })
    );

    expect(downloadPrismaFlow).toHaveBeenCalledWith('p1', 'md');
  });

  it('has no editable field', async () => {
    renderCard();

    await screen.findByText('Records identified');
    expect(screen.queryAllByRole('textbox')).toHaveLength(0);
    expect(screen.queryAllByRole('spinbutton')).toHaveLength(0);
    expect(document.querySelectorAll('input, select, textarea')).toHaveLength(
      0
    );
  });
});
