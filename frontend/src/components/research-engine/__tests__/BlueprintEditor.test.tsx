import { beforeEach, describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen } from '@testing-library/react';
import { BlueprintEditor } from '../BlueprintEditor';
import { getBlueprint, getProject } from '@/services/researchEngineService';

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
});
