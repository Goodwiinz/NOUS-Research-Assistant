import { act, screen, waitFor } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import type { ReactNode } from 'react';
import { beforeEach, describe, expect, it, vi } from 'vitest';

vi.mock('@/services/integrationContextService', () => ({
  integrationContextService: { options: vi.fn(), save: vi.fn() },
}));
const auth = { user: { id: 'u1' } };
vi.mock('@/hooks/useAuth', () => ({
  useAuth: (): { user: { id: string } } => auth,
}));

import { integrationContextService } from '@/services/integrationContextService';
import { render } from '@/test/test-utils';
import type { ApiContextOptions } from '@/types/api/integration-context-contract';

import { ContextSelection, MAX_SHARED_MEMORIES } from '../ContextSelection';

const REQUEST = '55555555-5555-4555-8555-555555555555';
const memory = (
  id: string,
  content: string
): ApiContextOptions['memories'][number] => ({
  id,
  content,
  source: 'manual',
  created_at: '2026-09-30T00:00:00Z',
});
const options = (
  selected: string[] = [],
  memories = [memory('m1', 'Cite in APA'), memory('m2', 'Client is a hospital')]
): ApiContextOptions => ({
  request_id: REQUEST,
  project_id: 'p1',
  project_label: 'Thesis',
  memories,
  selected_memory_ids: selected,
  skill_snapshot_status: 'none',
});

describe('ContextSelection', () => {
  beforeEach(() => {
    auth.user = { id: 'u1' };
  });

  it('shares nothing by default and saves exactly the checked memories', async () => {
    vi.mocked(integrationContextService.options).mockResolvedValue(options());
    vi.mocked(integrationContextService.save).mockResolvedValue(
      options(['m2'])
    );
    const { user } = render(<ContextSelection requestId={REQUEST} />);
    const hospital = await screen.findByRole('checkbox', {
      name: 'Client is a hospital',
    });
    expect(
      screen.getByRole('checkbox', { name: 'Cite in APA' })
    ).not.toBeChecked();
    expect(hospital).not.toBeChecked();
    await user.click(hospital);
    await user.click(screen.getByRole('button', { name: 'Save selection' }));
    expect(integrationContextService.save).toHaveBeenCalledWith(REQUEST, [
      'm2',
    ]);
    expect(await screen.findByRole('status')).toHaveTextContent(
      'can read 1 selected memories'
    );
    expect(
      screen.getByRole('checkbox', { name: 'Client is a hospital' })
    ).toBeChecked();
  });

  it('starts from the saved selection and can clear it', async () => {
    vi.mocked(integrationContextService.options).mockResolvedValue(
      options(['m1'])
    );
    vi.mocked(integrationContextService.save).mockResolvedValue(options([]));
    const { user } = render(<ContextSelection requestId={REQUEST} />);
    const apa = await screen.findByRole('checkbox', { name: 'Cite in APA' });
    expect(apa).toBeChecked();
    await user.click(apa);
    await user.click(screen.getByRole('button', { name: 'Save selection' }));
    expect(integrationContextService.save).toHaveBeenCalledWith(REQUEST, []);
  });

  it(`stops at ${MAX_SHARED_MEMORIES} memories`, async () => {
    const many = Array.from({ length: MAX_SHARED_MEMORIES + 1 }, (_, i) =>
      memory(`m${i}`, `memory ${i}`)
    );
    vi.mocked(integrationContextService.options).mockResolvedValue(
      options(
        many.slice(0, MAX_SHARED_MEMORIES).map((m) => m.id),
        many
      )
    );
    render(<ContextSelection requestId={REQUEST} />);
    const last = await screen.findByRole('checkbox', {
      name: `memory ${MAX_SHARED_MEMORIES}`,
    });
    expect(last).toBeDisabled();
    expect(screen.getByRole('checkbox', { name: 'memory 0' })).toBeEnabled();
  });

  it('reports options it cannot load and a save that failed', async () => {
    vi.mocked(integrationContextService.options).mockRejectedValueOnce(
      new Error('404')
    );
    const first = render(<ContextSelection requestId={REQUEST} />);
    expect(await screen.findByRole('alert')).toHaveTextContent(
      'could not be loaded'
    );
    first.unmount();
    vi.mocked(integrationContextService.options).mockResolvedValue(options());
    vi.mocked(integrationContextService.save).mockRejectedValueOnce(
      new Error('422')
    );
    const { user } = render(<ContextSelection requestId={REQUEST} />);
    await user.click(
      await screen.findByRole('button', { name: 'Save selection' })
    );
    await waitFor(() =>
      expect(screen.getByRole('alert')).toHaveTextContent('was not saved')
    );
  });

  it('refreshes the checks when the saved selection changed elsewhere', async () => {
    vi.mocked(integrationContextService.options)
      .mockResolvedValueOnce(options([]))
      .mockResolvedValue(options(['m1']));
    const { rerender } = render(<ContextSelection requestId={REQUEST} />);
    expect(
      await screen.findByRole('checkbox', { name: 'Cite in APA' })
    ).not.toBeChecked();
    // Another tab saved m1; a refetch delivers it.
    auth.user = { id: 'u1b' };
    rerender(<ContextSelection requestId={REQUEST} />);
    await waitFor(() =>
      expect(
        screen.getByRole('checkbox', { name: 'Cite in APA' })
      ).toBeChecked()
    );
  });

  it('keys the cache by account so a switch never shows the previous selection', async () => {
    vi.mocked(integrationContextService.options).mockResolvedValue(options());
    const { rerender } = render(<ContextSelection requestId={REQUEST} />);
    await screen.findByText(/Thesis/);
    auth.user = { id: 'u2' };
    rerender(<ContextSelection requestId={REQUEST} />);
    await waitFor(() =>
      expect(integrationContextService.options).toHaveBeenCalledTimes(2)
    );
  });
});

it('shares only checked skill versions and preserves the memory selection', async () => {
  const data = {
    ...options(['m1']),
    skills: [
      {
        version_id: 'v1',
        name: 'review',
        version: 1,
        description: 'Review rubric',
        content_hash: 'a'.repeat(64),
      },
    ],
    selected_skill_version_ids: [],
    skill_snapshot_status: 'none' as const,
  };
  vi.mocked(integrationContextService.options).mockResolvedValue(data);
  vi.mocked(integrationContextService.save).mockResolvedValue({
    ...data,
    selected_skill_version_ids: ['v1'],
    skill_snapshot_status: 'ready',
  });
  const { user } = render(<ContextSelection requestId={REQUEST} />);
  const skill = await screen.findByRole('checkbox', { name: /review.*v1/ });
  expect(skill).not.toBeChecked();
  await user.click(skill);
  await user.click(screen.getByRole('button', { name: 'Save selection' }));
  expect(integrationContextService.save).toHaveBeenCalledWith(
    REQUEST,
    ['m1'],
    ['v1'],
    false
  );
});

it('offers an explicit refresh when the frozen skills expired', async () => {
  const data = {
    ...options(['m1']),
    skills: [
      {
        version_id: 'v1',
        name: 'review',
        version: 1,
        description: 'Review rubric',
        content_hash: 'a'.repeat(64),
      },
    ],
    selected_skill_version_ids: ['v1'],
    skill_snapshot_status: 'unavailable' as const,
  };
  vi.mocked(integrationContextService.options).mockResolvedValue(data);
  vi.mocked(integrationContextService.save).mockResolvedValue({
    ...data,
    skill_snapshot_status: 'ready',
  });
  const { user } = render(<ContextSelection requestId={REQUEST} />);
  expect(
    await screen.findByText(/Frozen skills are unavailable/)
  ).toBeInTheDocument();
  await user.click(
    screen.getByRole('button', { name: 'Refresh selected skills' })
  );
  expect(integrationContextService.save).toHaveBeenCalledWith(
    REQUEST,
    ['m1'],
    ['v1'],
    true
  );
});

it('replaces an older selected version when choosing the same skill name', async () => {
  const data = {
    ...options([]),
    skills: [1, 2].map((version) => ({
      version_id: `v${version}`,
      name: 'review',
      version,
      description: 'Review rubric',
      content_hash: 'a'.repeat(64),
    })),
    selected_skill_version_ids: ['v1'],
    skill_snapshot_status: 'ready' as const,
  };
  vi.mocked(integrationContextService.options).mockResolvedValue(data);
  vi.mocked(integrationContextService.save).mockResolvedValue(data);
  const { user } = render(<ContextSelection requestId={REQUEST} />);
  const old = await screen.findByRole('checkbox', { name: /review.*v1/ });
  expect(old).toBeChecked();
  await user.click(screen.getByRole('checkbox', { name: /review.*v2/ }));
  expect(old).not.toBeChecked();
  await user.click(screen.getByRole('button', { name: 'Save selection' }));
  expect(integrationContextService.save).toHaveBeenCalledWith(
    REQUEST,
    [],
    ['v2'],
    false
  );
});

// Mutation: ContextSelection.tsx:55 must use the initiating owner/request.
// On 2026-10-09 replacing its key with the current queryKey leaks Alice's data
// into the current cache; both cases fail, then pass after restoring the key.
// PATH=/tmp/pr1864-next-font-repro/node-v24.21.0-linux-x64/bin:$PATH pnpm --dir
// frontend exec vitest run src/components/integrations/__tests__/ContextSelection.test.tsx -t 'pending save'
it.each(['account', 'consent'])(
  'keeps a pending save scoped to its initiating %s',
  async (change) => {
    auth.user = { id: 'alice' };
    const client = new QueryClient({
      defaultOptions: {
        queries: { retry: false },
        mutations: { retry: false },
      },
    });
    const wrapper = ({ children }: { children: ReactNode }): ReactNode => (
      <QueryClientProvider client={client}>{children}</QueryClientProvider>
    );
    const alice = {
      ...options(
        ['private-memory'],
        [memory('private-memory', 'Alice private memory')]
      ),
      project_label: 'Alice private project',
      skills: [
        {
          version_id: 'private-v1',
          name: 'private-skill',
          version: 1,
          description: 'Private skill',
          content_hash: 'a'.repeat(64),
        },
      ],
      selected_skill_version_ids: ['private-v1'],
      skill_snapshot_status: 'ready' as const,
    };
    const nextRequest = change === 'consent' ? 'second-consent' : REQUEST;
    const nextOwner = change === 'account' ? 'bob' : 'alice';
    const next = {
      ...options([], [memory('public-memory', 'Current memory')]),
      request_id: nextRequest,
      project_label: 'Current project',
      skills: [],
      selected_skill_version_ids: [],
      skill_snapshot_status: 'none' as const,
    };
    vi.mocked(integrationContextService.options)
      .mockResolvedValueOnce(alice)
      .mockResolvedValue(next);
    let complete: (value: ApiContextOptions) => void = () => {
      throw new Error('Save did not start');
    };
    vi.mocked(integrationContextService.save).mockImplementation(
      () =>
        new Promise((resolve) => {
          complete = resolve;
        })
    );
    const { user, rerender } = render(
      <ContextSelection requestId={REQUEST} />,
      { wrapper }
    );
    await user.click(
      await screen.findByRole('button', { name: 'Save selection' })
    );
    auth.user = { id: nextOwner };
    rerender(<ContextSelection requestId={nextRequest} />);
    await screen.findByText(/Current project/);
    await act(async () => {
      complete(alice);
      await Promise.resolve();
    });
    await waitFor(() =>
      expect(client.getMutationCache().getAll()[0]?.state.status).toBe(
        'success'
      )
    );
    expect(
      client.getQueryData<ApiContextOptions>([
        'integration-context',
        nextOwner,
        nextRequest,
      ])?.project_label
    ).toBe('Current project');
    expect(screen.queryByText('Alice private memory')).not.toBeInTheDocument();
    expect(screen.queryByText(/private-skill/)).not.toBeInTheDocument();
  }
);
