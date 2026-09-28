import { act, fireEvent, render, screen } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { RunView } from '../RunView';
import { getRun } from '@/services/researchEngineService';
import { useResearchEngineStore } from '@/store/research-engine-store';

const push = vi.fn();
const back = vi.fn();

vi.mock('next/navigation', () => ({
  useRouter: () => ({ push, back }),
}));
vi.mock('@/services/researchEngineService', () => ({
  getRun: vi.fn(),
  pauseRun: vi.fn(),
  resumeRun: vi.fn(),
}));

describe('RunView canonical navigation', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    useResearchEngineStore.setState({
      activeRun: null,
      runEvents: [],
      isLoading: false,
      error: null,
    });
  });

  it('returns a stable run URL to the canonical collection workflow', async () => {
    vi.mocked(getRun).mockResolvedValue({
      id: 'run-1',
      project_id: 'collection-1',
      research_engine_project_id: 'engine-1',
      blueprint_id: 'blueprint-1',
      blueprint_version: 1,
      protocol_version_id: 'protocol-version-1',
      effective_plan_hash: 'a'.repeat(64),
      conformance_status: 'plan_verified',
      status: 'completed',
      total_tokens: 10,
    });

    render(<RunView runId="run-1" />);
    await screen.findByText('Completed');
    expect(screen.getByText('Conformance: Plan verified')).toBeInTheDocument();
    expect(screen.getByText('Protocol protocol')).toBeInTheDocument();
    expect(screen.getByText('Plan aaaaaaaaaaaa')).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: 'Go back' }));

    expect(push).toHaveBeenCalledWith('/projects/collection-1?tab=workflow');
    expect(back).not.toHaveBeenCalled();
  });

  it('ignores an old project response after navigating to another run', async () => {
    let resolveOld!: (run: Awaited<ReturnType<typeof getRun>>) => void;
    let resolveNew!: (run: Awaited<ReturnType<typeof getRun>>) => void;
    vi.mocked(getRun)
      .mockReturnValueOnce(
        new Promise((resolve) => {
          resolveOld = resolve;
        })
      )
      .mockReturnValueOnce(
        new Promise((resolve) => {
          resolveNew = resolve;
        })
      );

    const { rerender } = render(<RunView runId="old-run" />);
    rerender(<RunView runId="new-run" />);
    const newRun = {
      id: 'new-run',
      project_id: 'new-collection',
      research_engine_project_id: 'new-engine',
      blueprint_id: 'blueprint-2',
      blueprint_version: 1,
      status: 'completed',
      total_tokens: 10,
    };
    await act(async () => {
      resolveNew(newRun);
    });
    await screen.findByText('Completed');
    await act(async () => {
      resolveOld({ ...newRun, id: 'old-run', project_id: 'old-collection' });
    });

    expect(useResearchEngineStore.getState().activeRun?.id).toBe('new-run');
    fireEvent.click(screen.getByRole('button', { name: 'Go back' }));
    expect(push).toHaveBeenCalledWith('/projects/new-collection?tab=workflow');
  });
});
