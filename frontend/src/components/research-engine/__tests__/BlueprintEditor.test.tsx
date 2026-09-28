import { beforeEach, describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen } from '@testing-library/react';
import { BlueprintEditor } from '../BlueprintEditor';
import {
  getBlueprint,
  getProject,
  startRun,
} from '@/services/researchEngineService';

vi.mock('next/navigation', () => ({ useRouter: () => ({ push: vi.fn() }) }));
vi.mock('@/services/researchEngineService', () => ({
  getProject: vi.fn(),
  getBlueprint: vi.fn(),
  createBlueprint: vi.fn(),
  startRun: vi.fn(),
}));

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
  });

  it('loads the saved blueprint after the request completes', async () => {
    render(<BlueprintEditor projectId="project-1" />);
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
    render(<BlueprintEditor projectId="project-1" />);
    expect(await screen.findByRole('alert')).toHaveTextContent(
      'Network unavailable'
    );
    fireEvent.click(screen.getByRole('button', { name: 'Retry' }));
    expect(screen.getByText('Loading project')).toBeInTheDocument();
    expect(
      await screen.findByRole('textbox', { name: 'Blueprint name' })
    ).toHaveValue('Paper discovery');
    expect(screen.queryByRole('alert')).not.toBeInTheDocument();
  });

  it('requires an approved protocol and starts with its exact version id', async () => {
    vi.mocked(getBlueprint).mockResolvedValue({
      id: 'blueprint-1',
      name: 'Paper discovery',
      steps: [
        {
          type: 'search',
          name: 'Search',
          parameters: {},
          mode: 'deterministic',
        },
      ],
      parameters: { topic: 'Saved method' },
    });
    vi.mocked(startRun).mockResolvedValue({ id: 'run-1' });

    const { rerender } = render(<BlueprintEditor projectId="project-1" />);
    const unavailable = await screen.findByRole('button', {
      name: 'Start run',
    });
    expect(unavailable).toBeDisabled();
    expect(
      screen.getByText('Approve a protocol version before starting a run.')
    ).toBeInTheDocument();

    rerender(
      <BlueprintEditor
        projectId="project-1"
        approvedProtocolVersionId="protocol-version-2"
      />
    );
    fireEvent.click(screen.getByRole('button', { name: 'Start run' }));
    expect(startRun).toHaveBeenCalledWith('blueprint-1', 'protocol-version-2');
  });
});
