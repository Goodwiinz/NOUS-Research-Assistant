import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { DailyResearchBriefSetup } from '../DailyResearchBriefSetup';
import { getCapabilities } from '@/services/researchEngineService';

vi.mock('@/services/researchEngineService', () => ({
  getCapabilities: vi.fn(),
}));

const AVAILABLE_CAPABILITIES = [
  {
    id: 'openalex',
    label: 'OpenAlex',
    daily_brief_eligible: true,
    available: true,
    features: { full_text: false, date_filter: false, cursor: true },
  },
  {
    id: 'crossref',
    label: 'Crossref',
    daily_brief_eligible: true,
    available: true,
    features: { full_text: false, date_filter: false, cursor: false },
  },
  {
    id: 'arxiv',
    label: 'arXiv',
    daily_brief_eligible: true,
    available: true,
    features: { full_text: false, date_filter: false, cursor: false },
  },
  {
    id: 'pubmed',
    label: 'PubMed',
    daily_brief_eligible: true,
    available: true,
    features: { full_text: false, date_filter: false, cursor: false },
  },
  {
    id: 'semantic_scholar',
    label: 'Semantic Scholar',
    daily_brief_eligible: true,
    available: true,
    features: { full_text: false, date_filter: false, cursor: false },
  },
  {
    id: 'rag_store',
    label: 'Workspace documents',
    daily_brief_eligible: false,
    available: true,
    features: { full_text: true, date_filter: false, cursor: false },
  },
  {
    id: 'unavailable',
    label: 'Unavailable provider',
    daily_brief_eligible: true,
    available: false,
    features: { full_text: false, date_filter: false, cursor: false },
  },
  {
    id: 'openalex',
    label: 'Duplicate OpenAlex',
    daily_brief_eligible: true,
    available: true,
    features: { full_text: false, date_filter: false, cursor: true },
  },
];

function renderSetup(
  onChange = vi.fn(),
  initialParameters: Record<string, unknown> = {
    contract_version: 1,
    research_question: '',
    inclusion_criteria: [],
    exclusion_criteria: [],
    providers: ['openalex', 'crossref'],
    limit_per_provider: 25,
    notes: '',
  }
): { onChange: typeof onChange; queryClient: QueryClient } {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  render(
    <QueryClientProvider client={queryClient}>
      <DailyResearchBriefSetup
        initialParameters={initialParameters}
        onChange={onChange}
      />
    </QueryClientProvider>
  );
  return { onChange, queryClient };
}

describe('DailyResearchBriefSetup', () => {
  beforeEach(() => {
    vi.resetAllMocks();
    vi.mocked(getCapabilities).mockResolvedValue(AVAILABLE_CAPABILITIES);
  });

  it('shows the bounded-review warning and requires a complete confirmed scope', async () => {
    const onChange = vi.fn();
    renderSetup(onChange);

    expect(
      screen.getByText(
        'This is a bounded brief from the selected sources. It is not an exhaustive or systematic review.'
      )
    ).toBeInTheDocument();
    const confirm = screen.getByRole('checkbox', { name: 'Confirm scope' });
    expect(confirm).toBeDisabled();

    fireEvent.change(screen.getByLabelText('Research question'), {
      target: { value: 'How do heat pumps affect household emissions?' },
    });
    fireEvent.change(screen.getByLabelText('What to include'), {
      target: { value: 'Peer-reviewed comparative studies' },
    });
    expect(confirm).toBeEnabled();

    fireEvent.click(confirm);
    expect(onChange).toHaveBeenLastCalledWith(
      expect.objectContaining({
        research_question: 'How do heat pumps affect household emissions?',
        inclusion_criteria: ['Peer-reviewed comparative studies'],
        providers: ['openalex', 'crossref'],
        limit_per_provider: 25,
      }),
      expect.objectContaining({
        confirmed: true,
        research_question: 'How do heat pumps affect household emissions?',
        inclusion_criteria: ['Peer-reviewed comparative studies'],
      })
    );

    fireEvent.change(screen.getByLabelText('Research question'), {
      target: { value: 'A changed question' },
    });
    expect(onChange).toHaveBeenLastCalledWith(
      expect.objectContaining({ research_question: 'A changed question' }),
      null
    );
    expect(confirm).not.toBeChecked();
  });

  it('renders only unique eligible available capabilities and enforces one to four sources', async () => {
    const onChange = vi.fn();
    const { queryClient } = renderSetup(onChange, {
      contract_version: 1,
      research_question: 'Question',
      inclusion_criteria: ['Include'],
      exclusion_criteria: [],
      providers: ['openalex', 'crossref', 'arxiv', 'pubmed'],
      limit_per_provider: 25,
      notes: '',
    });

    await waitFor(() => expect(getCapabilities).toHaveBeenCalledTimes(1));
    await expect(
      vi.mocked(getCapabilities).mock.results[0]?.value
    ).resolves.toEqual(AVAILABLE_CAPABILITIES);
    await waitFor(() =>
      expect(
        queryClient.getQueryData(['research-engine', 'capabilities'])
      ).toEqual(AVAILABLE_CAPABILITIES)
    );
    fireEvent.click(await screen.findByRole('button', { name: 'Sources (4)' }));
    expect(
      await screen.findAllByRole('checkbox', { name: 'OpenAlex' })
    ).toHaveLength(1);
    expect(
      screen.queryByRole('checkbox', { name: 'Workspace documents' })
    ).not.toBeInTheDocument();
    expect(
      screen.queryByRole('checkbox', { name: 'Unavailable provider' })
    ).not.toBeInTheDocument();
    expect(
      screen.getByRole('checkbox', { name: 'Semantic Scholar' })
    ).toBeDisabled();

    fireEvent.click(screen.getByRole('checkbox', { name: 'PubMed' }));
    await waitFor(() =>
      expect(onChange).toHaveBeenLastCalledWith(
        expect.objectContaining({
          providers: ['openalex', 'crossref', 'arxiv'],
        }),
        null
      )
    );
  });

  it('clamps results per source to the server contract', () => {
    const onChange = vi.fn();
    renderSetup(onChange);

    const limit = screen.getByRole('spinbutton', {
      name: 'Results per source',
    });
    fireEvent.change(limit, { target: { value: '99' } });
    expect(limit).toHaveValue(50);
    expect(onChange).toHaveBeenLastCalledWith(
      expect.objectContaining({ limit_per_provider: 50 }),
      null
    );

    fireEvent.change(limit, { target: { value: '0' } });
    expect(limit).toHaveValue(1);
    expect(onChange).toHaveBeenLastCalledWith(
      expect.objectContaining({ limit_per_provider: 1 }),
      null
    );
  });
});
