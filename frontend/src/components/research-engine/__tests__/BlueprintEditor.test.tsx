import { beforeEach, describe, expect, it, vi } from 'vitest';
import {
  act,
  fireEvent,
  render,
  screen,
  waitFor,
  within,
} from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { BlueprintEditor } from '../BlueprintEditor';
import { StepCard } from '../StepCard';
import {
  createBlueprint,
  getBlueprint,
  getCapabilities,
  getProject,
  getTemplateDetail,
  listTemplates,
  startRun,
} from '@/services/researchEngineService';

vi.mock('next/navigation', () => ({ useRouter: () => ({ push: vi.fn() }) }));
vi.mock('@/services/researchEngineService', () => ({
  getProject: vi.fn(),
  getBlueprint: vi.fn(),
  listTemplates: vi.fn(),
  getTemplateDetail: vi.fn(),
  getCapabilities: vi.fn(),
  createBlueprint: vi.fn(),
  startRun: vi.fn(),
}));

const TEMPLATE_STEPS = [
  'search',
  'screen',
  'extract',
  'synthesize',
  'verify',
  'export',
].map((type) => ({
  type,
  name: `${type} step`,
  parameters: {},
  mode: 'deterministic' as const,
  temperature: 0,
}));

const TEMPLATE_DETAIL = {
  slug: 'daily_research_brief',
  name: 'Daily Research Brief',
  description: 'A bounded brief',
  template_source: 'daily_research_brief',
  contract_version: 1,
  parameters: {
    contract_version: 1,
    research_question: '',
    inclusion_criteria: [],
    exclusion_criteria: [],
    providers: ['openalex', 'crossref'],
    limit_per_provider: 25,
    notes: '',
  },
  constraints: {
    providers: { min: 1, max: 4 },
    limit_per_provider: { min: 1, max: 50 },
  },
  coverage: { exhaustive: false },
  steps: TEMPLATE_STEPS,
};

const CAPABILITIES = [
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
];

function renderEditor(projectId = 'project-1'): ReturnType<typeof render> {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  return render(
    <QueryClientProvider client={queryClient}>
      <BlueprintEditor projectId={projectId} />
    </QueryClientProvider>
  );
}

describe('BlueprintEditor loading', () => {
  beforeEach(() => {
    vi.resetAllMocks();
    vi.mocked(getProject).mockResolvedValue({
      id: 'project-1',
      name: 'Research',
      blueprint_id: 'blueprint-1',
    });
    vi.mocked(getBlueprint).mockResolvedValue({
      id: 'blueprint-1',
      name: 'Paper discovery',
      steps: [],
      parameters: {},
    });
    vi.mocked(listTemplates).mockResolvedValue([]);
    vi.mocked(getTemplateDetail).mockResolvedValue(TEMPLATE_DETAIL);
    vi.mocked(getCapabilities).mockResolvedValue(CAPABILITIES);
  });

  it('loads the saved blueprint after the request completes', async () => {
    renderEditor();
    expect(screen.getByText('Loading project')).toBeInTheDocument();
    expect(
      await screen.findByRole('textbox', { name: 'Blueprint name' })
    ).toHaveValue('Paper discovery');
    expect(screen.queryByText('Loading project')).not.toBeInTheDocument();
  });

  it('recovers from a failed load through Retry', async () => {
    vi.mocked(getProject).mockRejectedValueOnce(
      new Error('Network unavailable')
    );
    renderEditor();
    expect(await screen.findByRole('alert')).toHaveTextContent(
      'Failed to load project.'
    );
    fireEvent.click(screen.getByRole('button', { name: 'Retry' }));
    expect(screen.getByText('Loading project')).toBeInTheDocument();
    expect(
      await screen.findByRole('textbox', { name: 'Blueprint name' })
    ).toHaveValue('Paper discovery');
    expect(screen.queryByRole('alert')).not.toBeInTheDocument();
  });

  it('fetches full template detail before applying and saves its template source', async () => {
    vi.mocked(getProject).mockResolvedValue({
      id: 'project-1',
      name: 'Research',
    });
    vi.mocked(listTemplates).mockResolvedValue([
      {
        slug: 'daily_research_brief',
        name: 'Daily Research Brief',
        description: 'A bounded brief',
        step_count: 6,
      },
    ]);
    let resolveDetail: (value: typeof TEMPLATE_DETAIL) => void = () =>
      undefined;
    vi.mocked(getTemplateDetail).mockReturnValue(
      new Promise((resolve) => {
        resolveDetail = resolve;
      })
    );
    vi.mocked(createBlueprint).mockResolvedValue({
      id: 'blueprint-1',
      ...TEMPLATE_DETAIL,
      project_id: 'project-1',
      version: 1,
      is_immutable: false,
      created_at: '2026-09-28T00:00:00Z',
      updated_at: '2026-09-28T00:00:00Z',
    });

    renderEditor();
    fireEvent.click(
      await screen.findByRole('button', { name: /Daily Research Brief/i })
    );
    expect(getTemplateDetail).toHaveBeenCalledWith('daily_research_brief');
    expect(
      screen.queryByRole('textbox', { name: 'Blueprint name' })
    ).not.toBeInTheDocument();

    resolveDetail(TEMPLATE_DETAIL);
    expect(
      await screen.findByRole('textbox', { name: 'Blueprint name' })
    ).toHaveValue('Daily Research Brief');
    expect(screen.getByText('Steps (6)')).toBeInTheDocument();

    fireEvent.click(screen.getByRole('button', { name: 'Save' }));
    expect(createBlueprint).toHaveBeenCalledWith(
      'project-1',
      expect.objectContaining({
        template_source: 'daily_research_brief',
        steps: TEMPLATE_STEPS,
      })
    );
  });

  it('clears template_source when the user creates a custom topology', async () => {
    vi.mocked(getBlueprint).mockResolvedValue({
      id: 'blueprint-1',
      name: 'Daily Research Brief',
      template_source: 'daily_research_brief',
      steps: TEMPLATE_STEPS,
      parameters: TEMPLATE_DETAIL.parameters,
    });
    vi.mocked(createBlueprint).mockResolvedValue({
      id: 'blueprint-custom',
      name: 'Daily Research Brief',
      steps: [...TEMPLATE_STEPS, TEMPLATE_STEPS[0]],
      parameters: TEMPLATE_DETAIL.parameters,
    });

    renderEditor();
    expect(await screen.findByText('Steps (6)')).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: 'Add step' }));
    fireEvent.click(screen.getByRole('button', { name: 'Save' }));

    const request = vi.mocked(createBlueprint).mock.calls[0][1];
    expect(request).not.toHaveProperty('template_source');
    expect(request.steps).toHaveLength(7);
  });

  it('requires an exact scope confirmation before starting a Daily Brief', async () => {
    const parameters = {
      ...TEMPLATE_DETAIL.parameters,
      research_question: 'What changed?',
      inclusion_criteria: ['Peer reviewed'],
    };
    vi.mocked(getBlueprint).mockResolvedValue({
      id: 'blueprint-1',
      name: 'Daily Research Brief',
      template_source: 'daily_research_brief',
      steps: TEMPLATE_STEPS,
      parameters,
    });
    vi.mocked(startRun).mockResolvedValue({ id: 'run-1' });

    renderEditor();
    const start = await screen.findByRole('button', { name: 'Start run' });
    expect(start).toBeDisabled();
    fireEvent.click(screen.getByRole('checkbox', { name: 'Confirm scope' }));
    expect(start).toBeEnabled();
    fireEvent.click(start);

    expect(startRun).toHaveBeenCalledWith('blueprint-1', {
      parameters_override: parameters,
      scope_confirmation: {
        confirmed: true,
        research_question: 'What changed?',
        inclusion_criteria: ['Peer reviewed'],
        exclusion_criteria: [],
        providers: ['openalex', 'crossref'],
        limit_per_provider: 25,
        notes: '',
      },
    });
  });

  it('keeps an edited Daily Brief draft from starting the old blueprint until save succeeds', async () => {
    const parameters = {
      ...TEMPLATE_DETAIL.parameters,
      research_question: 'What changed?',
      inclusion_criteria: ['Peer reviewed'],
    };
    vi.mocked(getBlueprint).mockResolvedValue({
      id: 'blueprint-1',
      name: 'Daily Research Brief',
      template_source: 'daily_research_brief',
      steps: TEMPLATE_STEPS,
      parameters,
    });
    vi.mocked(createBlueprint)
      .mockRejectedValueOnce(new Error('Save unavailable'))
      .mockResolvedValueOnce({
        id: 'blueprint-custom',
        name: 'Daily Research Brief',
        steps: [
          ...TEMPLATE_STEPS,
          {
            type: 'search',
            name: '',
            description: '',
            parameters: {},
            mode: 'deterministic',
            temperature: 0,
          },
        ],
        parameters,
      });
    vi.mocked(startRun).mockResolvedValue({ id: 'run-custom' });

    renderEditor();
    const start = await screen.findByRole('button', { name: 'Start run' });
    fireEvent.click(screen.getByRole('checkbox', { name: 'Confirm scope' }));
    expect(start).toBeEnabled();

    fireEvent.click(screen.getByRole('button', { name: 'Add step' }));
    expect(start).toBeDisabled();
    fireEvent.click(start);
    expect(startRun).not.toHaveBeenCalled();

    fireEvent.click(screen.getByRole('button', { name: 'Save' }));
    expect(await screen.findByRole('alert')).toHaveTextContent(
      'Failed to save blueprint.'
    );
    expect(start).toBeDisabled();
    expect(startRun).not.toHaveBeenCalled();

    fireEvent.click(screen.getByRole('button', { name: 'Save' }));
    await waitFor(() => expect(start).toBeEnabled());
    expect(createBlueprint).toHaveBeenLastCalledWith(
      'project-1',
      expect.objectContaining({
        steps: expect.arrayContaining([
          expect.objectContaining({ type: 'search', name: '' }),
        ]),
      })
    );
    expect(
      vi.mocked(createBlueprint).mock.calls.at(-1)?.[1]
    ).not.toHaveProperty('template_source');

    fireEvent.click(start);
    expect(startRun).toHaveBeenCalledWith('blueprint-custom', {
      parameters_override: parameters,
    });
  });

  it('keeps a newer topology dirty when an older save resolves', async () => {
    vi.mocked(getBlueprint).mockResolvedValue({
      id: 'blueprint-1',
      name: 'Custom brief',
      steps: TEMPLATE_STEPS,
      parameters: {},
    });
    const staleResponse = {
      id: 'blueprint-stale',
      name: 'Custom brief',
      steps: [
        ...TEMPLATE_STEPS,
        {
          type: 'search',
          name: '',
          description: '',
          parameters: {},
          mode: 'deterministic' as const,
          temperature: 0,
        },
      ],
      parameters: {},
    };
    let resolveSave: (value: typeof staleResponse) => void = () => undefined;
    vi.mocked(createBlueprint).mockReturnValue(
      new Promise((resolve) => {
        resolveSave = resolve;
      })
    );

    renderEditor();
    const start = await screen.findByRole('button', { name: 'Start run' });
    expect(start).toBeEnabled();

    fireEvent.click(screen.getByRole('button', { name: 'Add step' }));
    expect(screen.getByText('Steps (7)')).toBeInTheDocument();
    expect(start).toBeDisabled();

    fireEvent.click(screen.getByRole('button', { name: 'Save' }));
    expect(
      await screen.findByRole('button', { name: 'Saving' })
    ).toBeDisabled();
    expect(vi.mocked(createBlueprint).mock.calls[0][1].steps).toHaveLength(7);

    fireEvent.click(screen.getByRole('button', { name: 'Add step' }));
    expect(screen.getByText('Steps (8)')).toBeInTheDocument();

    await act(async () => resolveSave(staleResponse));
    await screen.findByRole('button', { name: 'Save' });
    expect(start).toBeDisabled();
    expect(screen.queryByText('blueprint-stale')).not.toBeInTheDocument();
    fireEvent.click(start);
    expect(startRun).not.toHaveBeenCalled();
  });
});

describe('StepCard contract', () => {
  it('offers exactly the six backend-supported step types', () => {
    render(
      <StepCard
        step={TEMPLATE_STEPS[0]}
        index={0}
        totalSteps={1}
        onChange={vi.fn()}
        onMoveUp={vi.fn()}
        onMoveDown={vi.fn()}
        onRemove={vi.fn()}
      />
    );
    fireEvent.click(screen.getByRole('button', { name: /search step/i }));
    const typeSelect = screen.getByRole('combobox', { name: 'Step type' });
    expect(
      within(typeSelect)
        .getAllByRole('option')
        .map((option) => option.getAttribute('value'))
    ).toEqual([
      'search',
      'screen',
      'extract',
      'synthesize',
      'verify',
      'export',
    ]);
  });
});
