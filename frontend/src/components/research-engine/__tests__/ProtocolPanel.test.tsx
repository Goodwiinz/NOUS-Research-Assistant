import { fireEvent, render, screen } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';

import { ProtocolPanel } from '../ProtocolPanel';
import {
  useProtocolActions,
  useProtocolDeviations,
  useProtocolRegistrations,
  useResearchProtocols,
  useResearchQuestions,
} from '@/hooks/useResearchProtocols';

vi.mock('@/hooks/useResearchProtocols', () => ({
  useResearchQuestions: vi.fn(),
  useResearchProtocols: vi.fn(),
  useProtocolActions: vi.fn(),
  useProtocolRegistrations: vi.fn(),
  useProtocolDeviations: vi.fn(),
}));

interface MutationStub {
  mutate: ReturnType<typeof vi.fn>;
  isPending: boolean;
  error: null;
}

interface ActionStubs {
  createQuestion: MutationStub;
  createQuestionVersion: MutationStub;
  createProtocol: MutationStub;
  createVersion: MutationStub;
  approve: MutationStub;
  register: MutationStub;
}

const mutation = (): MutationStub => ({
  mutate: vi.fn(),
  isPending: false,
  error: null,
});

const question = {
  id: 'question-1',
  project_id: 'collection-1',
  current_version_id: 'question-version-2',
  current_version: {
    id: 'question-version-2',
    question_id: 'question-1',
    version: 2,
    parent_version_id: 'question-version-1',
    question: 'Does the intervention improve recovery?',
    hypothesis: null,
    scope: 'Adults',
    framework: {},
    content_hash: 'q'.repeat(64),
    author_user_id: 'editor-1',
    created_at: '2026-01-02T00:00:00Z',
  },
  versions: [],
  created_at: '2026-01-01T00:00:00Z',
};

const initialVersion = {
  id: 'protocol-version-1',
  protocol_id: 'protocol-1',
  version: 1,
  parent_version_id: null,
  question_version_id: 'question-version-1',
  blueprint_id: 'blueprint-1',
  execution_plan: {
    blueprint_id: 'blueprint-1',
    blueprint_version: 1,
    steps: [],
    parameters: {},
  },
  snapshot: {
    eligibility: { population: 'Adults' },
    sources_search: {},
    selection: {},
    extraction: {},
    appraisal_synthesis: {},
    outcomes: {},
    reviewer_mode: {},
  },
  content_hash: 'a'.repeat(64),
  status: 'approved',
  change_kind: 'initial',
  amendment_reason: null,
  author_user_id: 'editor-1',
  approved_by_user_id: 'supervisor-1',
  approved_at: '2026-01-01T12:00:00Z',
  superseded_at: null,
  created_at: '2026-01-01T00:00:00Z',
};

const draftVersion = {
  ...initialVersion,
  id: 'protocol-version-2',
  version: 2,
  parent_version_id: 'protocol-version-1',
  question_version_id: 'question-version-2',
  snapshot: {
    ...initialVersion.snapshot,
    eligibility: { population: 'Adults over 50' },
  },
  content_hash: 'b'.repeat(64),
  status: 'draft',
  change_kind: 'amendment',
  amendment_reason: 'Narrow the population',
  approved_by_user_id: null,
  approved_at: null,
  created_at: '2026-01-02T00:00:00Z',
};

function arrange(
  canApprove: boolean,
  extraProtocols: Array<Record<string, unknown>> = []
): ActionStubs {
  vi.mocked(useResearchQuestions).mockReturnValue({
    data: [{ ...question, versions: [question.current_version] }],
    isLoading: false,
    error: null,
  } as ReturnType<typeof useResearchQuestions>);
  vi.mocked(useResearchProtocols).mockReturnValue({
    data: [
      {
        id: 'protocol-1',
        project_id: 'collection-1',
        name: 'Recovery protocol',
        current_draft_version_id: draftVersion.id,
        current_approved_version_id: initialVersion.id,
        versions: [
          { ...initialVersion, can_approve: false },
          { ...draftVersion, can_approve: canApprove },
        ],
        can_edit: true,
        can_approve: canApprove,
        created_at: '2026-01-01T00:00:00Z',
        updated_at: '2026-01-02T00:00:00Z',
      },
      ...extraProtocols,
    ],
    isLoading: false,
    error: null,
  } as ReturnType<typeof useResearchProtocols>);
  const actions = {
    createQuestion: mutation(),
    createQuestionVersion: mutation(),
    createProtocol: mutation(),
    createVersion: mutation(),
    approve: mutation(),
    register: mutation(),
  };
  vi.mocked(useProtocolActions).mockReturnValue(
    actions as ReturnType<typeof useProtocolActions>
  );
  vi.mocked(useProtocolRegistrations).mockReturnValue({
    data: [],
  } as ReturnType<typeof useProtocolRegistrations>);
  vi.mocked(useProtocolDeviations).mockReturnValue({ data: [] } as ReturnType<
    typeof useProtocolDeviations
  >);
  return actions;
}

describe('ProtocolPanel', () => {
  beforeEach(() => vi.clearAllMocks());

  it('shows version provenance, snapshot differences, and approved binding', () => {
    arrange(false);
    const onApprovedVersionChange = vi.fn();
    render(
      <ProtocolPanel
        projectId="collection-1"
        blueprintId="blueprint-1"
        readOnly={false}
        onApprovedVersionChange={onApprovedVersionChange}
      />
    );

    expect(screen.getAllByText('Version 2')).toHaveLength(2);
    expect(screen.getByText(/Narrow the population/)).toBeInTheDocument();
    fireEvent.click(screen.getAllByText('Compare snapshot')[1]);
    expect(
      screen.getByText('Changed sections: eligibility')
    ).toBeInTheDocument();
    expect(screen.getByText(/Approved by supervisor-1/)).toBeInTheDocument();
    expect(
      screen.getByText(/independent assigned supervisor/)
    ).toBeInTheDocument();
    expect(onApprovedVersionChange).toHaveBeenCalledWith('protocol-version-1');
  });

  it('lets only a server-authorized supervisor approve the exact version and hash', () => {
    const actions = arrange(true);
    render(
      <ProtocolPanel
        projectId="collection-1"
        blueprintId="blueprint-1"
        readOnly={false}
        onApprovedVersionChange={vi.fn()}
      />
    );

    fireEvent.change(screen.getByRole('textbox', { name: 'Approval reason' }), {
      target: { value: 'Independent review completed' },
    });
    fireEvent.click(
      screen.getByRole('button', { name: 'Approve exact version' })
    );

    expect(actions.approve.mutate).toHaveBeenCalledWith(
      {
        protocolId: 'protocol-1',
        versionId: 'protocol-version-2',
        body: expect.objectContaining({
          expected_protocol_version: 2,
          expected_content_hash: 'b'.repeat(64),
          expected_current_approved_version_id: 'protocol-version-1',
          reason: 'Independent review completed',
        }),
      },
      expect.any(Object)
    );
    const firstApprovalKey = actions.approve.mutate.mock.calls[0][0].body
      .idempotency_key as string;
    fireEvent.click(
      screen.getByRole('button', { name: 'Approve exact version' })
    );
    expect(actions.approve.mutate.mock.calls[1][0].body.idempotency_key).toBe(
      firstApprovalKey
    );
    fireEvent.change(screen.getByRole('textbox', { name: 'Approval reason' }), {
      target: { value: 'Updated independent review rationale' },
    });
    fireEvent.click(
      screen.getByRole('button', { name: 'Approve exact version' })
    );
    expect(
      actions.approve.mutate.mock.calls[2][0].body.idempotency_key
    ).not.toBe(firstApprovalKey);

    fireEvent.click(screen.getByText('Recorded registration receipts'));
    fireEvent.change(
      screen.getByRole('textbox', { name: 'Registry provider' }),
      {
        target: { value: 'OSF' },
      }
    );
    fireEvent.change(
      screen.getByRole('textbox', {
        name: 'External registration identifier',
      }),
      { target: { value: 'osf-123' } }
    );
    fireEvent.click(screen.getByRole('button', { name: 'Record receipt' }));
    expect(actions.register.mutate).toHaveBeenCalledWith(
      {
        protocolId: 'protocol-1',
        body: expect.objectContaining({
          protocol_version_id: 'protocol-version-1',
          protocol_version_hash: 'a'.repeat(64),
          provider: 'OSF',
          external_identifier: 'osf-123',
          status: 'registered',
        }),
      },
      expect.any(Object)
    );
  });

  it('does not expose an approval bound to a different execution blueprint', () => {
    arrange(false);
    const onApprovedVersionChange = vi.fn();
    render(
      <ProtocolPanel
        projectId="collection-1"
        blueprintId="blueprint-2"
        readOnly={false}
        onApprovedVersionChange={onApprovedVersionChange}
      />
    );

    expect(
      screen.getByText(/current execution blueprint differs/)
    ).toBeInTheDocument();
    expect(onApprovedVersionChange).toHaveBeenCalledWith(undefined);
  });

  it('requires an explicit protocol selection when multiple records exist', () => {
    arrange(false, [
      {
        id: 'protocol-2',
        project_id: 'collection-1',
        name: 'Secondary protocol',
        current_draft_version_id: null,
        current_approved_version_id: null,
        versions: [],
        can_edit: true,
        can_approve: false,
        created_at: '2026-01-03T00:00:00Z',
        updated_at: '2026-01-03T00:00:00Z',
      },
    ]);
    render(
      <ProtocolPanel
        projectId="collection-1"
        blueprintId="blueprint-1"
        readOnly={false}
        onApprovedVersionChange={vi.fn()}
      />
    );

    fireEvent.change(screen.getByLabelText('Protocol record'), {
      target: { value: 'protocol-2' },
    });
    expect(screen.getByText(/No approved protocol exists/)).toBeInTheDocument();
  });
});
