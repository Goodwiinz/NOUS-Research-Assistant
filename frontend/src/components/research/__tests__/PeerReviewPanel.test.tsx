import { beforeEach, describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen, waitFor } from '@/test/test-utils';
import { PeerReviewPanel } from '../PeerReviewPanel';
import { projectService } from '@/services/projectService';
import { listProjectRoles } from '@/services/researchEngineService';
import type {
  ApiPeerReviewComment,
  ApiPeerReviewRound,
  ApiPeerReviewRoundDetail,
} from '@/types/api/peer-review-contract';

vi.mock('@/hooks/useAuth', () => ({ useAuth: () => ({ user: { id: 'me' } }) }));
vi.mock('@/services/researchEngineService', () => ({
  listProjectRoles: vi.fn(),
}));
vi.mock('@/services/projectService', () => ({
  projectService: {
    listPeerReviewRounds: vi.fn(),
    getPeerReviewRound: vi.fn(),
    listDrafts: vi.fn(),
    listClaims: vi.fn(),
    diffDrafts: vi.fn(),
    respondToPeerReviewComment: vi.fn(),
    decidePeerReviewComment: vi.fn(),
    downloadPeerReviewExport: vi.fn(),
  },
}));

const NOW = '2026-10-01T00:00:00Z';
const ROUND: ApiPeerReviewRound = {
  id: 'round-1',
  collection_id: 'project-1',
  draft_id: 'v1',
  draft_version: 1,
  draft_content_hash: 'a'.repeat(64),
  label: 'Round 1',
  received_at: null,
  created_by_id: 'me',
  created_at: NOW,
  reviewers: [],
};

function comment(
  overrides: Partial<ApiPeerReviewComment> = {}
): ApiPeerReviewComment {
  const current = {
    id: 'c1',
    round_id: 'round-1',
    reviewer_id: 'rev-1',
    number: 1,
    body: 'Soften this claim.',
    draft_id: 'v1',
    draft_content_hash: 'a'.repeat(64),
    start_char: 11,
    end_char: 49,
    quote: 'Beta rises sharply in the treated arm.',
    quote_sha256: 'b'.repeat(64),
    supersedes_comment_id: null,
    author_id: 'me',
    created_at: NOW,
  };
  return {
    comment_root_id: 'c1',
    reviewer_id: 'rev-1',
    reviewer_label: 'Reviewer 1',
    number: 1,
    status: 'open',
    anchor_state: 'exact',
    anchor_start: 11,
    anchor_end: 49,
    current,
    versions: [current],
    response: null,
    responses: [],
    assignment: null,
    resolution: null,
    decisions: [],
    ...overrides,
  };
}

const CHANGE = {
  id: 'resp-1',
  comment_root_id: 'c1',
  kind: 'change' as const,
  body: 'Softened.',
  revised_draft_id: 'v2',
  revised_content_hash: 'c'.repeat(64),
  base_draft_id: 'v1',
  diff_sha256: 'd'.repeat(64),
  rationale: null,
  evidence_claim_version_ids: [],
  supersedes_response_id: null,
  author_id: 'me',
  created_at: NOW,
  diff_verified: true,
  evidence: [],
  hunks: [
    {
      op: 'replace' as const,
      old: [11, 49],
      new: [11, 50],
      old_text: 'Beta rises sharply in the treated arm.',
      new_text: 'Beta rises modestly in the treated arm.',
    },
  ],
};

function detail(...comments: ApiPeerReviewComment[]): ApiPeerReviewRoundDetail {
  return {
    round: ROUND,
    target: { id: 'v2', version: 2, content_hash: 'c'.repeat(64) },
    comments,
  };
}

function setup(
  comments: ApiPeerReviewComment[],
  role: 'adjudicator' | null = null
): void {
  vi.mocked(projectService.listPeerReviewRounds).mockResolvedValue({
    rounds: [ROUND],
  });
  vi.mocked(projectService.getPeerReviewRound).mockResolvedValue(
    detail(...comments)
  );
  vi.mocked(projectService.listDrafts).mockResolvedValue({
    drafts: [
      { id: 'v1', version: 1 },
      { id: 'v2', version: 2 },
    ] as never,
    total: 2,
    skip: 0,
    limit: 50,
  });
  vi.mocked(projectService.listClaims).mockResolvedValue({
    items: [],
    counts: {
      claims: 0,
      links_by_kind: {},
      legacy_unanchored: 0,
      assessed: 0,
      unassessed: 0,
    },
  });
  vi.mocked(listProjectRoles).mockResolvedValue(
    role
      ? [
          {
            id: 'r',
            project_id: 'project-1',
            user_id: 'me',
            role,
            assigned_by_id: 'o',
            created_at: NOW,
          },
        ]
      : []
  );
}

const draft = { id: 'v2', version: 2 };

describe('PeerReviewPanel (GOO-314)', () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it('unresolved anchor badge and quote shown', async () => {
    setup([comment({ anchor_state: 'unresolved_anchor', anchor_start: null })]);
    render(<PeerReviewPanel projectId="project-1" draft={draft} />);
    expect(
      await screen.findByLabelText('Anchor: Unresolved anchor')
    ).toBeInTheDocument();
    expect(screen.getByRole('status')).toHaveTextContent('Original quote (v1)');
    expect(
      screen.getByText(/Beta rises sharply in the treated arm\./)
    ).toBeInTheDocument();
    expect(projectService.getPeerReviewRound).toHaveBeenCalledWith(
      'project-1',
      'round-1',
      'v2'
    );
  });

  it('no-change requires rationale', async () => {
    setup([comment()]);
    render(<PeerReviewPanel projectId="project-1" draft={draft} />);
    fireEvent.click(await screen.findByRole('button', { name: 'Respond' }));
    fireEvent.change(screen.getByLabelText('Response'), {
      target: { value: 'We keep it.' },
    });
    const submit = screen.getByRole('button', { name: 'Submit response' });
    expect(submit).toBeDisabled();
    expect(screen.getByRole('alert')).toHaveTextContent('needs a rationale');
    fireEvent.click(submit);
    expect(projectService.respondToPeerReviewComment).not.toHaveBeenCalled();
    fireEvent.change(screen.getByLabelText(/Rationale/), {
      target: { value: 'It is the primary finding.' },
    });
    expect(submit).toBeEnabled();
    vi.mocked(projectService.respondToPeerReviewComment).mockResolvedValue(
      CHANGE
    );
    fireEvent.click(submit);
    await waitFor(() =>
      expect(projectService.respondToPeerReviewComment).toHaveBeenCalledWith(
        'project-1',
        'c1',
        expect.objectContaining({
          kind: 'no_change',
          rationale: 'It is the primary finding.',
          revised_draft_id: null,
          supersedes_response_id: null,
        })
      )
    );
  });

  it('resolve hidden without adjudicator role', async () => {
    setup([comment({ status: 'responded', response: CHANGE })]);
    const { unmount } = render(
      <PeerReviewPanel projectId="project-1" draft={draft} />
    );
    expect(await screen.findByText('Softened.')).toBeInTheDocument();
    await waitFor(() => expect(listProjectRoles).toHaveBeenCalled());
    expect(screen.queryByRole('button', { name: 'Resolve' })).toBeNull();
    unmount();
    setup([comment({ status: 'responded', response: CHANGE })], 'adjudicator');
    render(<PeerReviewPanel projectId="project-1" draft={draft} />);
    expect(
      await screen.findByRole('button', { name: 'Resolve' })
    ).toBeInTheDocument();
  });

  it('response links open old and new text', async () => {
    setup([comment({ status: 'responded', response: CHANGE })]);
    const onOpenPassage = vi.fn();
    render(
      <PeerReviewPanel
        projectId="project-1"
        draft={draft}
        onOpenPassage={onOpenPassage}
      />
    );
    fireEvent.click(await screen.findByRole('button', { name: 'Old text' }));
    await waitFor(() => expect(projectService.listDrafts).toHaveBeenCalled());
    fireEvent.click(screen.getByRole('button', { name: 'New text' }));
    expect(onOpenPassage).toHaveBeenCalledWith(
      expect.objectContaining({
        draftId: 'v1',
        start: 11,
        end: 49,
        text: 'Beta rises sharply in the treated arm.',
      })
    );
    expect(onOpenPassage).toHaveBeenLastCalledWith(
      expect.objectContaining({
        draftId: 'v2',
        version: 2,
        start: 11,
        end: 50,
        text: 'Beta rises modestly in the treated arm.',
      })
    );
  });
});
