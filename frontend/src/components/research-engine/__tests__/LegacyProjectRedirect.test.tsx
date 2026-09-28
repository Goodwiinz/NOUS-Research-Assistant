import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { render, screen, waitFor } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { LegacyProjectRedirect } from '../LegacyProjectRedirect';
import { getLegacyProject } from '@/services/researchEngineService';

const replace = vi.fn();

vi.mock('next/navigation', () => ({
  useRouter: () => ({ replace }),
}));
vi.mock('@/services/researchEngineService', () => ({
  getLegacyProject: vi.fn(),
  linkProject: vi.fn(),
}));
vi.mock('@/services/projectService', () => ({
  listWorkflowLinkOptions: vi.fn().mockResolvedValue([]),
}));

function renderRedirect(): void {
  render(
    <QueryClientProvider
      client={
        new QueryClient({ defaultOptions: { queries: { retry: false } } })
      }
    >
      <LegacyProjectRedirect engineProjectId="engine-1" />
    </QueryClientProvider>
  );
}

describe('LegacyProjectRedirect', () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it('redirects a mapped engine deep link to the canonical project workflow', async () => {
    vi.mocked(getLegacyProject).mockResolvedValue({
      research_engine_project_id: 'engine-1',
      project_id: 'collection-1',
      collection_id: 'collection-1',
      name: 'Project',
      status: 'active',
    });

    renderRedirect();

    expect(screen.getByText('Opening project workflow…')).toBeInTheDocument();
    await waitFor(() =>
      expect(replace).toHaveBeenCalledWith(
        '/projects/collection-1?tab=workflow'
      )
    );
  });
});
