import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { CorpusPanel } from '../CorpusPanel';
import {
  chaseCitations,
  downloadCorpus,
  getCorpusCoverage,
  getImport,
  importSearchResults,
  listImports,
  listReports,
  type CorpusCoverage,
  type ImportReceipt,
  type ImportReceiptDetail,
  type ResearchReport,
} from '@/services/researchEngineService';

vi.mock('@/services/researchEngineService', () => ({
  chaseCitations: vi.fn(),
  downloadCorpus: vi.fn(),
  getCorpusCoverage: vi.fn(),
  getImport: vi.fn(),
  importSearchResults: vi.fn(),
  listImports: vi.fn(),
  listReports: vi.fn(),
}));

const receipt = (overrides: Partial<ImportReceipt> = {}): ImportReceipt => ({
  id: 'receipt-1',
  kind: 'file_import',
  version: 1,
  previous_receipt_id: null,
  declared: {
    database: 'Embase (Ovid)',
    query_text: 'aspirin',
    search_date: '2026-09-01',
    redistribution: 'restricted',
  },
  observed: { imported_at: '2026-09-29T10:00:00+00:00' },
  parsed_count: 5,
  accepted_count: 3,
  rejected_count: 2,
  replayed: false,
  created_at: '2026-09-29T10:00:00Z',
  ...overrides,
});

const detail: ImportReceiptDetail = {
  ...receipt(),
  records: [
    {
      id: 'rec-4',
      record_index: 3,
      status: 'rejected',
      rejection_reason: 'missing_title',
      parsed: {},
      report_id: null,
      raw: null,
    },
    {
      id: 'rec-5',
      record_index: 4,
      status: 'rejected',
      rejection_reason: 'unterminated_record',
      parsed: {},
      report_id: null,
      raw: null,
    },
  ],
};

const coverage: CorpusCoverage = {
  found: [],
  missing: [],
  recall: null,
  searched: [{ type: 'import' }],
  not_searched: ['arxiv', 'crossref'],
  citation_chasing: {
    required: true,
    directions: ['backward', 'forward'],
    performed: ['backward'],
    missing_directions: ['forward'],
  },
  exhaustive: false,
  statement: 'Coverage is not exhaustive.',
};

const seed: ResearchReport = {
  id: 'report-1',
  title_snapshot: 'Aspirin trial',
  identifiers: { openalex: ['W1'] },
  observations: [],
};

function renderPanel(readOnly = false): void {
  render(
    <QueryClientProvider
      client={
        new QueryClient({ defaultOptions: { queries: { retry: false } } })
      }
    >
      <CorpusPanel projectId="collection-1" readOnly={readOnly} />
    </QueryClientProvider>
  );
}

describe('CorpusPanel', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(listImports).mockResolvedValue([receipt()]);
    vi.mocked(getImport).mockResolvedValue(detail);
    vi.mocked(getCorpusCoverage).mockResolvedValue(coverage);
    vi.mocked(listReports).mockResolvedValue([seed]);
    vi.mocked(downloadCorpus).mockResolvedValue(undefined);
  });

  it('renders receipts with declared and observed dates and lists rejections', async () => {
    renderPanel();

    expect(await screen.findByText('Embase (Ovid) v1')).toBeInTheDocument();
    expect(screen.getByText('2026-09-01')).toBeInTheDocument();
    expect(screen.getByText('2026-09-29')).toBeInTheDocument();
    fireEvent.click(
      screen.getByText('Rejected records for Embase (Ovid) v1 (2)')
    );
    expect(
      await screen.findByText('Record 4: missing_title')
    ).toBeInTheDocument();
    expect(
      screen.getByText('Record 5: unterminated_record')
    ).toBeInTheDocument();
    expect(getImport).toHaveBeenCalledWith('collection-1', 'receipt-1');
  });

  it('requires a file and a declared database before importing', async () => {
    vi.mocked(importSearchResults).mockResolvedValue(receipt());
    renderPanel();
    const submit = await screen.findByRole('button', {
      name: 'Import results',
    });
    expect(submit).toBeDisabled();

    const file = new File(['TY  - JOUR'], 'embase.ris');
    fireEvent.change(screen.getByLabelText('Result file'), {
      target: { files: [file] },
    });
    expect(submit).toBeDisabled();
    fireEvent.change(
      screen.getByLabelText('Database searched (declared by you, required)'),
      { target: { value: ' Embase (Ovid) ' } }
    );
    fireEvent.change(screen.getByLabelText('Search date (declared by you)'), {
      target: { value: '2026-09-01' },
    });
    expect(submit).toBeEnabled();
    fireEvent.click(submit);

    await waitFor(() =>
      expect(importSearchResults).toHaveBeenCalledWith(
        'collection-1',
        file,
        'ris',
        {
          database: 'Embase (Ovid)',
          query_text: null,
          search_date: '2026-09-01',
          redistribution: 'restricted',
        }
      )
    );
    expect(
      await screen.findByText(/Imported Embase \(Ovid\) v1: 3 accepted/)
    ).toBeInTheDocument();
  });

  it('says so when the same file was already imported', async () => {
    vi.mocked(importSearchResults).mockResolvedValue(
      receipt({ replayed: true })
    );
    renderPanel();
    fireEvent.change(await screen.findByLabelText('Result file'), {
      target: { files: [new File(['x'], 'x.ris')] },
    });
    fireEvent.change(
      screen.getByLabelText('Database searched (declared by you, required)'),
      { target: { value: 'Embase (Ovid)' } }
    );
    fireEvent.click(screen.getByRole('button', { name: 'Import results' }));

    expect(
      await screen.findByText(/already imported; showing the existing receipt/)
    ).toBeInTheDocument();
  });

  it('downloads the corpus and states coverage is not exhaustive', async () => {
    renderPanel();

    expect(screen.getByText('Coverage is not exhaustive.')).toBeInTheDocument();
    expect(
      await screen.findByText(/Not searched: arxiv, crossref\./)
    ).toBeInTheDocument();
    expect(screen.getByText(/missing: forward\./)).toBeInTheDocument();
    fireEvent.click(
      screen.getByRole('button', { name: 'Download corpus (ZIP)' })
    );
    await waitFor(() =>
      expect(downloadCorpus).toHaveBeenCalledWith('collection-1', 'zip')
    );
  });

  it('chases citations from a chosen seed report', async () => {
    vi.mocked(chaseCitations).mockResolvedValue(
      receipt({
        kind: 'citation_chase',
        declared: {
          seed_report_id: 'report-1',
          direction: 'forward',
          requested_limit: 50,
          redistribution: 'allowed',
        },
        observed: { completion: 'exhausted' },
      })
    );
    renderPanel();
    const chase = await screen.findByRole('button', {
      name: 'Chase citations (OpenAlex)',
    });
    expect(chase).toBeDisabled();
    await screen.findByRole('option', { name: 'Aspirin trial' });
    fireEvent.change(screen.getByLabelText('Seed report for citation chase'), {
      target: { value: 'report-1' },
    });
    fireEvent.change(screen.getByLabelText('Direction'), {
      target: { value: 'forward' },
    });
    fireEvent.click(chase);

    await waitFor(() => expect(chaseCitations).toHaveBeenCalledTimes(1));
    expect(vi.mocked(chaseCitations).mock.calls[0]?.[1]).toMatchObject({
      seed_report_id: 'report-1',
      direction: 'forward',
    });
    expect(
      await screen.findByText(/Citation chase recorded \(exhausted\)/)
    ).toBeInTheDocument();
  });

  it('is read-only when archived but still downloads', async () => {
    renderPanel(true);

    expect(await screen.findByText('Embase (Ovid) v1')).toBeInTheDocument();
    expect(
      screen.queryByRole('button', { name: 'Import results' })
    ).not.toBeInTheDocument();
    expect(
      screen.queryByRole('button', { name: 'Chase citations (OpenAlex)' })
    ).not.toBeInTheDocument();
    expect(listReports).not.toHaveBeenCalled();
    expect(
      screen.getByRole('button', { name: 'Download corpus (JSON)' })
    ).toBeEnabled();
  });

  it('shows import failures as an alert', async () => {
    vi.mocked(importSearchResults).mockRejectedValue(
      new Error('The file must be UTF-8 encoded.')
    );
    renderPanel();
    fireEvent.change(await screen.findByLabelText('Result file'), {
      target: { files: [new File(['x'], 'x.ris')] },
    });
    fireEvent.change(
      screen.getByLabelText('Database searched (declared by you, required)'),
      { target: { value: 'Embase' } }
    );
    fireEvent.click(screen.getByRole('button', { name: 'Import results' }));

    expect(await screen.findByRole('alert')).toHaveTextContent(
      'The file must be UTF-8 encoded.'
    );
  });
});
