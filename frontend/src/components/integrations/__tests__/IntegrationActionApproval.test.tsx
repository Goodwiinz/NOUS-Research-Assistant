import { screen, waitFor } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';

vi.mock('@/services/integrationActionService', () => ({
  integrationActionService: { review: vi.fn(), decide: vi.fn() },
}));
vi.mock('@/hooks/useAuth', () => ({
  useAuth: (): { user: { id: string } } => ({ user: { id: 'u1' } }),
}));

import { integrationActionService } from '@/services/integrationActionService';
import { render } from '@/test/test-utils';
import type {
  ActionState,
  ApiActionReview,
} from '@/types/api/integration-action-contract';

import {
  IntegrationActionApproval,
  revealHidden,
} from '../IntegrationActionApproval';

const ID = '44444444-4444-4444-8444-444444444444';

const review = (
  state: ActionState,
  extra: Partial<ApiActionReview> = {}
): ApiActionReview => ({
  invocation_id: ID,
  state,
  tool_name: 'create_project_note',
  summary: 'Create note “Findings” in project “Thesis”',
  arguments: {
    title: 'Findings',
    content: '# Findings\n**not bold here**',
    tags: ['draft', 'review, urgent'],
  },
  project_id: 'p1',
  project_label: 'Thesis',
  project_available: true,
  title: 'Findings',
  content: '# Findings\n**not bold here**',
  tags: ['draft', 'review, urgent'],
  requested_at: '2026-09-30T00:00:00Z',
  decided_at: null,
  result: null,
  last_error: null,
  ...extra,
});

describe('revealHidden', () => {
  it('shows bidi, zero-width and BOM characters as visible codes', () => {
    expect(revealHidden('safe\u202Etxt.exe')).toBe('safe⟨U+202E⟩txt.exe');
    expect(revealHidden('a\u200Bb\uFEFF')).toBe('a⟨U+200B⟩b⟨U+FEFF⟩');
    expect(revealHidden('plain text')).toBe('plain text');
  });
});

describe('IntegrationActionApproval', () => {
  beforeEach(() => {
    vi.mocked(integrationActionService.decide).mockResolvedValue({
      invocation_id: ID,
      state: 'approved',
      tool_name: 'create_project_note',
      result: null,
      approval_url: null,
    });
  });

  it('shows the exact stored target as plain text before any decision', async () => {
    vi.mocked(integrationActionService.review).mockResolvedValue(
      review('awaiting_approval')
    );
    render(<IntegrationActionApproval invocationId={ID} />);
    expect(await screen.findByText(/Thesis \(p1\)/)).toBeInTheDocument();
    expect(
      screen.getByText('Create note “Findings” in project “Thesis”')
    ).toBeInTheDocument();
    expect(
      screen.getByRole('heading', { name: 'Approve a note' })
    ).toBeInTheDocument();
    expect(screen.getByText('Findings')).toBeInTheDocument();
    expect(screen.getByText('“review, urgent”')).toBeInTheDocument();
    expect(screen.getByText('“draft”')).toBeInTheDocument();
    // Markdown is not rendered: the raw markers stay visible.
    expect(screen.getByText(/\*\*not bold here\*\*/)).toBeInTheDocument();
    expect(screen.getByLabelText('Note content')).toHaveAttribute(
      'tabindex',
      '0'
    );
    expect(screen.getByText(/Content \(\d+ characters\)/)).toBeInTheDocument();
    expect(screen.getByRole('status')).toHaveTextContent(
      'Nothing has been created yet'
    );
    expect(integrationActionService.review).toHaveBeenCalledWith(ID);
  });

  it('records one decision and ignores a second click', async () => {
    vi.mocked(integrationActionService.review)
      .mockResolvedValueOnce(review('awaiting_approval'))
      .mockResolvedValue(review('approved'));
    const { user } = render(<IntegrationActionApproval invocationId={ID} />);
    const approve = await screen.findByRole('button', {
      name: 'Approve and create note',
    });
    await user.click(approve);
    await user.click(approve);
    await user.click(screen.getByRole('button', { name: 'Deny' }));
    expect(integrationActionService.decide).toHaveBeenCalledTimes(1);
    expect(integrationActionService.decide).toHaveBeenCalledWith(ID, true);
    await waitFor(() =>
      expect(screen.getByRole('status')).toHaveTextContent('Approved')
    );
    expect(approve).toBeDisabled();
  });

  it.each([
    ['succeeded', 'Created.'],
    ['outcome_unknown', 'could not confirm'],
  ] as const)(
    'describes %s honestly with no decision buttons enabled',
    async (state, text) => {
      vi.mocked(integrationActionService.review).mockResolvedValue(
        review(state)
      );
      render(<IntegrationActionApproval invocationId={ID} />);
      await screen.findByText(/Thesis \(p1\)/);
      expect(screen.getByRole('status')).toHaveTextContent(text);
      expect(screen.getByRole('button', { name: 'Deny' })).toBeDisabled();
    }
  );

  it('keeps a request for a deleted project deniable but not approvable', async () => {
    vi.mocked(integrationActionService.review).mockResolvedValue(
      review('awaiting_approval', { project_available: false })
    );
    render(<IntegrationActionApproval invocationId={ID} />);
    expect(
      await screen.findByText(/This project was deleted/)
    ).toBeInTheDocument();
    expect(
      screen.getByRole('button', { name: 'Approve and create note' })
    ).toBeDisabled();
    expect(screen.getByRole('button', { name: 'Deny' })).toBeEnabled();
  });

  it('shows the workspace when the action has no project', async () => {
    vi.mocked(integrationActionService.review).mockResolvedValue(
      review('awaiting_approval', {
        project_id: null,
        project_label: null,
        workspace_id: 'w1',
        workspace_label: 'Lab',
      })
    );
    render(<IntegrationActionApproval invocationId={ID} />);
    expect(await screen.findByText(/Lab \(w1\)/)).toBeInTheDocument();
    expect(screen.getByText('Workspace')).toBeInTheDocument();
    expect(screen.queryByText('Project')).not.toBeInTheDocument();
    expect(screen.queryByText(/This .* was deleted/)).not.toBeInTheDocument();
  });

  it('keeps a request for a deleted workspace deniable but not approvable', async () => {
    vi.mocked(integrationActionService.review).mockResolvedValue(
      review('awaiting_approval', {
        project_id: null,
        project_label: null,
        workspace_id: 'w1',
        workspace_label: 'Lab',
        project_available: false,
      })
    );
    render(<IntegrationActionApproval invocationId={ID} />);
    expect(
      await screen.findByText(/This workspace was deleted/)
    ).toBeInTheDocument();
    expect(
      screen.getByRole('button', { name: 'Approve and create note' })
    ).toBeDisabled();
    expect(screen.getByRole('button', { name: 'Deny' })).toBeEnabled();
  });

  it('shows why a failed request was not created', async () => {
    vi.mocked(integrationActionService.review).mockResolvedValue(
      review('failed', { last_error: 'denied by user' })
    );
    render(<IntegrationActionApproval invocationId={ID} />);
    await screen.findByText(/Thesis \(p1\)/);
    expect(screen.getByRole('status')).toHaveTextContent(
      'Not created. denied by user.'
    );
  });

  it('reports a request it cannot load and a decision it could not record', async () => {
    vi.mocked(integrationActionService.review).mockRejectedValueOnce(
      new Error('404')
    );
    const first = render(<IntegrationActionApproval invocationId={ID} />);
    expect(await screen.findByRole('alert')).toHaveTextContent(
      'could not be loaded'
    );
    first.unmount();

    vi.mocked(integrationActionService.review).mockResolvedValue(
      review('awaiting_approval')
    );
    vi.mocked(integrationActionService.decide).mockRejectedValueOnce(
      new Error('409')
    );
    const { user } = render(<IntegrationActionApproval invocationId={ID} />);
    await user.click(
      await screen.findByRole('button', { name: 'Approve and create note' })
    );
    expect(await screen.findByRole('alert')).toHaveTextContent(
      'was not recorded'
    );
    expect(
      screen.getByRole('button', { name: 'Approve and create note' })
    ).toBeDisabled();
  });
});

// A library action: the same page, led by the server's summary and listing
// the stored arguments where a note shows its title and content.
const change = (
  state: ActionState,
  extra: Partial<ApiActionReview> = {}
): ApiActionReview =>
  review(state, {
    tool_name: 'save_papers_to_folder',
    summary: 'Save 2 papers to folder “Thesis”: “Attention”, “BERT”',
    arguments: { document_ids: ['d1', 'd2'], project_id: 'p1' },
    title: '',
    content: '',
    tags: [],
    ...extra,
  });

describe('IntegrationActionApproval for a library change', () => {
  beforeEach(() => {
    vi.mocked(integrationActionService.decide).mockResolvedValue({
      invocation_id: ID,
      state: 'approved',
      tool_name: 'save_papers_to_folder',
      result: null,
      approval_url: null,
    });
  });

  it('leads with the summary and lists the stored arguments, not a note', async () => {
    vi.mocked(integrationActionService.review).mockResolvedValue(
      change('awaiting_approval')
    );
    render(<IntegrationActionApproval invocationId={ID} />);
    expect(
      await screen.findByText(
        'Save 2 papers to folder “Thesis”: “Attention”, “BERT”'
      )
    ).toBeInTheDocument();
    expect(
      screen.getByRole('heading', { name: 'Approve a library change' })
    ).toBeInTheDocument();
    // The stored target, named a folder like the summary does.
    expect(screen.getByText('Folder')).toBeInTheDocument();
    expect(screen.getByText(/Thesis \(p1\)/)).toBeInTheDocument();
    // Every stored argument under its own key, one quoted item per entry.
    expect(screen.getByText('document_ids')).toBeInTheDocument();
    expect(screen.getByText('“d1”')).toBeInTheDocument();
    expect(screen.getByText('“d2”')).toBeInTheDocument();
    expect(screen.getByText('project_id')).toBeInTheDocument();
    expect(screen.queryByLabelText('Note content')).not.toBeInTheDocument();
    expect(screen.queryByText('Title')).not.toBeInTheDocument();
    expect(screen.getByRole('status')).toHaveTextContent(
      'Waiting for your decision. Nothing has changed yet.'
    );
  });

  it('records one approval', async () => {
    vi.mocked(integrationActionService.review)
      .mockResolvedValueOnce(change('awaiting_approval'))
      .mockResolvedValue(change('approved'));
    const { user } = render(<IntegrationActionApproval invocationId={ID} />);
    const approve = await screen.findByRole('button', { name: 'Approve' });
    await user.click(approve);
    await user.click(approve);
    expect(integrationActionService.decide).toHaveBeenCalledTimes(1);
    expect(integrationActionService.decide).toHaveBeenCalledWith(ID, true);
    await waitFor(() =>
      expect(screen.getByRole('status')).toHaveTextContent(
        'Approved. NOUS is making the change.'
      )
    );
  });

  it.each([
    ['succeeded', null, 'Done.'],
    ['failed', 'target not found', 'Not done. target not found.'],
    [
      'outcome_unknown',
      null,
      'NOUS could not confirm whether the change was made.',
    ],
  ] as const)(
    'describes %s in terms of a change',
    async (state, lastError, text) => {
      vi.mocked(integrationActionService.review).mockResolvedValue(
        change(state, { last_error: lastError })
      );
      render(<IntegrationActionApproval invocationId={ID} />);
      await screen.findByText(/Thesis \(p1\)/);
      expect(screen.getByRole('status')).toHaveTextContent(text);
      expect(screen.getByRole('status')).not.toHaveTextContent(/note|created/i);
      expect(screen.getByRole('button', { name: 'Deny' })).toBeDisabled();
    }
  );

  it('keeps a change to a deleted folder deniable but not approvable', async () => {
    vi.mocked(integrationActionService.review).mockResolvedValue(
      change('awaiting_approval', {
        tool_name: 'delete_folder',
        summary: 'Delete folder “Thesis” (documents are kept)',
        arguments: { project_id: 'p1' },
        project_available: false,
      })
    );
    render(<IntegrationActionApproval invocationId={ID} />);
    expect(
      await screen.findByText(/This folder was deleted/)
    ).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Approve' })).toBeDisabled();
    expect(screen.getByRole('button', { name: 'Deny' })).toBeEnabled();
  });

  it('shows a workspace-level change and an argument with no entries', async () => {
    vi.mocked(integrationActionService.review).mockResolvedValue(
      change('awaiting_approval', {
        tool_name: 'update_document_metadata',
        summary: 'Remove every tag from paper “Attention”',
        arguments: { document_id: 'd1', tags: [] },
        project_id: null,
        project_label: null,
        workspace_id: 'w1',
        workspace_label: 'Lab',
      })
    );
    render(<IntegrationActionApproval invocationId={ID} />);
    expect(await screen.findByText(/Lab \(w1\)/)).toBeInTheDocument();
    expect(screen.getByText('Workspace')).toBeInTheDocument();
    expect(screen.getByText('document_id')).toBeInTheDocument();
    expect(screen.getByText('d1')).toBeInTheDocument();
    // An empty list is shown as empty, never dropped.
    expect(screen.getByText('tags')).toBeInTheDocument();
    expect(screen.getByText('(empty)')).toBeInTheDocument();
  });

  it('reveals hidden characters in the summary and the arguments', async () => {
    vi.mocked(integrationActionService.review).mockResolvedValue(
      change('awaiting_approval', {
        tool_name: 'rename_folder',
        summary: 'Rename folder “Thesis” to “safe\u202Etxt.exe”',
        arguments: { project_id: 'p1', name: 'safe\u202Etxt.exe' },
      })
    );
    render(<IntegrationActionApproval invocationId={ID} />);
    expect(
      await screen.findByText('Rename folder “Thesis” to “safe⟨U+202E⟩txt.exe”')
    ).toBeInTheDocument();
    expect(screen.getByText('safe⟨U+202E⟩txt.exe')).toBeInTheDocument();
  });
});
