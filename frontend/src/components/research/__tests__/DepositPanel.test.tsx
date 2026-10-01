import { beforeEach, describe, expect, it, vi } from 'vitest';
import { render, screen } from '@/test/test-utils';
import { DepositPanel } from '../DepositPanel';
import { projectService } from '@/services/projectService';
import type { ApiManuscriptRelease } from '@/types/api/manuscript-release-contract';
import type {
  ApiDeposit,
  ApiDepositApproval,
  ApiDepositList,
} from '@/types/api/research-deposit-contract';

vi.mock('@/services/projectService', () => ({
  projectService: {
    listDeposits: vi.fn(),
    approveDeposit: vi.fn(),
    revokeDepositApproval: vi.fn(),
    requestDeposit: vi.fn(),
    requeueDeposit: vi.fn(),
  },
}));

const SHA = 'd'.repeat(64);
const DOI = '10.5072/zenodo.101';

const release = {
  id: 'release-verified-1',
  package_sha256: SHA,
  stage: 'verified',
  status: 'verified',
} as ApiManuscriptRelease;

function approval(
  overrides: Partial<ApiDepositApproval> = {}
): ApiDepositApproval {
  return {
    id: 'approval-1',
    release_id: release.id,
    package_sha256: SHA,
    repository: 'zenodo_sandbox',
    account_ref: 'zenodo_sandbox:nous-fixture',
    action: 'publish',
    kind: 'approved',
    approved_by_id: 'adjudicator-1',
    actor_role: 'adjudicator',
    rationale: 'ok',
    created_at: '2026-10-01T00:00:00Z',
    in_force: true,
    ...overrides,
  };
}

function deposit(overrides: Partial<ApiDeposit> = {}): ApiDeposit {
  return {
    operation_id: 'operation-1',
    release_id: release.id,
    package_sha256: SHA,
    repository: 'zenodo_sandbox',
    account_ref: 'zenodo_sandbox:nous-fixture',
    requested_by_id: 'supervisor-1',
    approval_id: 'approval-1',
    approval_in_force: true,
    status: 'published',
    last_reason: null,
    files: [],
    remote_deposition_id: '101',
    remote_record_id: '101',
    doi: null,
    doi_url: null,
    queue_status: 'pending',
    attempts: [],
    created_at: '2026-10-01T00:00:00Z',
    ...overrides,
  };
}

function withList(overrides: Partial<ApiDepositList> = {}): void {
  vi.mocked(projectService.listDeposits).mockResolvedValue({
    repository: 'zenodo_sandbox',
    configured: true,
    account_ref: 'zenodo_sandbox:nous-fixture',
    approvals: [],
    deposits: [],
    ...overrides,
  });
}

function renderPanel(): void {
  render(<DepositPanel projectId="project-1" release={release} canRelease />);
}

describe('DepositPanel (GOO-318)', () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it('doi hidden until verified', async () => {
    withList({ approvals: [approval()], deposits: [deposit()] });
    renderPanel();
    expect(await screen.findByLabelText('Deposit status')).toHaveTextContent(
      'Published'
    );
    expect(screen.queryByRole('link')).not.toBeInTheDocument();
    expect(screen.queryByText(/10\.5072/)).not.toBeInTheDocument();
    expect(screen.getByText('Sandbox')).toBeInTheDocument();
  });

  it('shows the DOI link once verified', async () => {
    withList({
      approvals: [approval()],
      deposits: [
        deposit({
          status: 'verified',
          doi: DOI,
          doi_url: `https://doi.org/${DOI}`,
          queue_status: 'done',
        }),
      ],
    });
    renderPanel();
    const link = await screen.findByRole('link', { name: `DOI ${DOI}` });
    expect(link).toHaveAttribute('href', `https://doi.org/${DOI}`);
  });

  it('ambiguous never reads published', async () => {
    withList({
      approvals: [approval()],
      deposits: [deposit({ status: 'ambiguous', remote_record_id: null })],
    });
    renderPanel();
    expect(await screen.findByLabelText('Deposit status')).toHaveTextContent(
      'Checking with Zenodo…'
    );
    expect(screen.getByLabelText('Published: not yet')).toBeInTheDocument();
    expect(screen.getByLabelText('Draft created: not yet')).toBeInTheDocument();
    expect(screen.queryByRole('link')).not.toBeInTheDocument();
  });

  it('request disabled without valid approval', async () => {
    withList({ approvals: [approval({ in_force: false })] });
    renderPanel();
    const button = await screen.findByRole('button', {
      name: 'Deposit to Zenodo sandbox',
    });
    expect(button).toBeDisabled();
    expect(screen.getByLabelText('Deposit approval')).toHaveTextContent(
      'No deposit approval in force'
    );
    button.click();
    expect(projectService.requestDeposit).not.toHaveBeenCalled();
  });

  it('request enabled with an approval in force', async () => {
    withList({ approvals: [approval()] });
    vi.mocked(projectService.requestDeposit).mockResolvedValue(deposit());
    renderPanel();
    const button = await screen.findByRole('button', {
      name: 'Deposit to Zenodo sandbox',
    });
    await vi.waitFor(() => expect(button).toBeEnabled());
    button.click();
    await vi.waitFor(() =>
      expect(projectService.requestDeposit).toHaveBeenCalledWith(
        'project-1',
        expect.objectContaining({
          release_id: release.id,
          package_sha256: SHA,
        })
      )
    );
  });
});
