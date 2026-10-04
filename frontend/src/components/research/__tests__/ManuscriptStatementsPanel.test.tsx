import { beforeEach, describe, expect, it, vi } from 'vitest';
import { render, screen, within } from '@/test/test-utils';
import { ManuscriptStatementsPanel } from '../ManuscriptStatementsPanel';
import { projectService } from '@/services/projectService';
import type {
  ApiAuthorIdentity,
  ApiStatementSet,
} from '@/types/api/statements-contract';

vi.mock('@/hooks/useAuth', () => ({ useAuth: () => ({ user: { id: 'me' } }) }));
vi.mock('@/services/projectService', () => ({
  projectService: {
    listStatements: vi.fn(),
    createStatementSet: vi.fn(),
    approveStatementSet: vi.fn(),
    orcidStart: vi.fn(),
  },
}));

function author(overrides: Partial<ApiAuthorIdentity>): ApiAuthorIdentity {
  return {
    author_key: 'a',
    order: 1,
    display_name: 'Ada',
    user_id: null,
    orcid: null,
    orcid_status: 'unknown',
    orcid_receipt: null,
    approval: null,
    ...overrides,
  };
}

function tip(authors: ApiAuthorIdentity[]): ApiStatementSet {
  return {
    id: 'set-1',
    collection_id: 'project-1',
    body: {
      authors: authors.map((a) => ({
        author_key: a.author_key,
        order: a.order,
        display_name: a.display_name,
        user_id: a.user_id,
        orcid: a.orcid,
        corresponding: a.order === 1,
        credit_roles: ['methodology'],
      })),
      limitations: null,
    },
    set_hash: 'a'.repeat(64),
    schema_id: 'nous.statements/1',
    credit_vocabulary: 'credit/1',
    supersedes_set_id: null,
    created_by_id: 'me',
    created_at: '2026-10-01T00:00:00Z',
    is_tip: true,
    missing_fields: ['limitations'],
    missing_items: [
      {
        rule: 'required',
        field: 'limitations',
        detail: 'The limitations statement is missing',
        fix: 'Write the limitations statement',
      },
    ],
    authors,
    approvals: [],
    all_approved: false,
    grants_permissions: false,
  };
}

function withTip(authors: ApiAuthorIdentity[]): void {
  vi.mocked(projectService.listStatements).mockResolvedValue({
    tip: tip(authors),
    history: [tip(authors)],
    credit_roles: ['methodology', 'software'],
    credit_vocabulary: 'credit/1',
  });
}

describe('ManuscriptStatementsPanel (GOO-316)', () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it('orcid badge never authenticated without receipt', async () => {
    withTip([
      // A status claiming "authenticated" with no receipt is not trusted.
      author({
        author_key: 'a',
        orcid: '0000-0002-1825-0097',
        orcid_status: 'authenticated',
      }),
      author({
        author_key: 'b',
        order: 2,
        display_name: 'Bea',
        orcid: '0000-0001-5109-3700',
        orcid_status: 'authenticated',
        orcid_receipt: {
          authentication_id: 'r1',
          environment: 'sandbox',
          token_received_at: '2026-09-30T00:00:00Z',
        },
      }),
      author({ author_key: 'c', order: 3, display_name: 'Cy' }),
    ]);
    render(<ManuscriptStatementsPanel projectId="project-1" />);
    const first = await screen.findByRole('row', { name: 'Author 1' });
    expect(
      within(first).getByLabelText('ORCID: Unauthenticated')
    ).toBeInTheDocument();
    expect(within(first).queryByLabelText('ORCID: Authenticated')).toBeNull();
    const second = screen.getByRole('row', { name: 'Author 2' });
    expect(
      within(second).getByLabelText('ORCID: Authenticated')
    ).toHaveTextContent(/sandbox/);
    const third = screen.getByRole('row', { name: 'Author 3' });
    expect(within(third).getByLabelText('ORCID: Unknown')).toBeInTheDocument();
  });

  it('missing fields listed with fix text', async () => {
    withTip([author({})]);
    render(<ManuscriptStatementsPanel projectId="project-1" />);
    const missing = await screen.findByLabelText('Missing fields');
    expect(
      within(missing).getByText(/limitations: Write the limitations statement/)
    ).toBeInTheDocument();
    expect(screen.getAllByText('Missing').length).toBeGreaterThan(1);
  });

  it('verify link only on own author row', async () => {
    withTip([
      author({ author_key: 'a', user_id: 'me' }),
      author({
        author_key: 'b',
        order: 2,
        display_name: 'Bea',
        user_id: 'someone',
      }),
    ]);
    render(<ManuscriptStatementsPanel projectId="project-1" />);
    const own = await screen.findByRole('row', { name: 'Author 1' });
    expect(
      within(own).getByRole('button', { name: /Verify with ORCID/ })
    ).toBeInTheDocument();
    expect(
      within(own).getByRole('button', { name: 'Approve as me' })
    ).toBeInTheDocument();
    const other = screen.getByRole('row', { name: 'Author 2' });
    expect(
      within(other).queryByRole('button', { name: /Verify with ORCID/ })
    ).toBeNull();
    expect(
      within(other).queryByRole('button', { name: 'Approve as me' })
    ).toBeNull();
    expect(
      within(other).getByRole('button', { name: 'Record attestation' })
    ).toBeDisabled();
  });
});
