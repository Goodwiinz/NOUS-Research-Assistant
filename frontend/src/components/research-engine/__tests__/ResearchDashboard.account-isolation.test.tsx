import { act, render, waitFor } from '@testing-library/react';
import { beforeEach, expect, it, vi } from 'vitest';
import { ResearchDashboard } from '../ResearchDashboard';
import { useResearchEngineStore } from '@/store/research-engine-store';
import { resetAccountSession } from '@/lib/account-session';
import { listProjects, listTemplates } from '@/services/researchEngineService';

vi.mock('@/services/researchEngineService', () => ({
  listProjects: vi.fn(),
  listTemplates: vi.fn().mockResolvedValue([]),
}));
vi.mock('../ProjectCard', () => ({ ProjectCard: () => null }));
vi.mock('../CreateProjectModal', () => ({ CreateProjectModal: () => null }));
beforeEach(() => {
  vi.mocked(listTemplates).mockResolvedValue([]);
  useResearchEngineStore.setState(useResearchEngineStore.getInitialState());
});

it.each(['success', 'rejection'])(
  'ignores A research project %s after an account reset',
  async (outcome) => {
    let resolve!: (value: never) => void;
    let reject!: (reason: Error) => void;
    vi.mocked(listProjects).mockReturnValueOnce(
      new Promise((yes, no) => {
        resolve = yes;
        reject = no;
      })
    );
    render(<ResearchDashboard />);
    await waitFor(() => expect(listProjects).toHaveBeenCalledOnce());
    act(() => {
      resetAccountSession();
      useResearchEngineStore.setState({
        ...useResearchEngineStore.getInitialState(),
        projects: [{ id: 'B-project' } as never],
        isLoading: true,
      });
    });
    const before = useResearchEngineStore.getState();
    await act(async () => {
      if (outcome === 'success') resolve([{ id: 'A-only-project' }] as never);
      else reject(new Error('A-only-error'));
    });
    expect(useResearchEngineStore.getState()).toEqual(before);
  }
);
