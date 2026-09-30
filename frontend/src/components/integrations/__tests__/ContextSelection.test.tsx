import { screen, waitFor } from '@testing-library/react';
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
