import { describe, expect, it, vi } from 'vitest';

import { render, screen } from '@/test/test-utils';

import IntegrationApprovalPage from '../../../../app/(dashboard)/integrations/approve/page';

const mockGet = vi.fn();

vi.mock('next/navigation', () => ({
  useSearchParams: () => new URLSearchParams('request_id=req-1'),
}));
vi.mock('@/hooks/useAuth', () => ({
  useAuth: () => ({ user: { id: 'user-1' }, isAuthenticated: true }),
}));
vi.mock('@/services/api-client', () => ({
  api: { get: (...args: unknown[]) => mockGet(...args), post: vi.fn() },
}));

const request = {
  id: 'req-1',
  status: 'pending',
  expires_at: '2026-10-05T12:00:00Z',
  approval_url: '/integrations/approve?request_id=req-1',
  project_id: 'project-1',
  device_id: 'device-1',
  scopes: ['artifacts:publish'],
  project_label: 'Thesis',
  device_label: 'Laptop',
};

describe('IntegrationApprovalPage', () => {
  it('shows the chat a request is bound to', async () => {
    mockGet.mockResolvedValue({
      ...request,
      thread_id: 'thread-1',
      thread_label: 'Literature review',
    });
    render(<IntegrationApprovalPage />);
    expect(await screen.findByText('Chat')).toBeInTheDocument();
    expect(
      screen.getByText('Literature review (thread-1)')
    ).toBeInTheDocument();
  });

  it('omits the chat row for a project-only request', async () => {
    mockGet.mockResolvedValue({
      ...request,
      thread_id: null,
      thread_label: null,
    });
    render(<IntegrationApprovalPage />);
    expect(await screen.findByText('Thesis (project-1)')).toBeInTheDocument();
    expect(screen.queryByText('Chat')).not.toBeInTheDocument();
  });
});
