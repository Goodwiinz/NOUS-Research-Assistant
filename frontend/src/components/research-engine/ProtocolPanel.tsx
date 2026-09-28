'use client';

import { useEffect, useMemo, useRef, useState, type ReactElement } from 'react';
import { CheckCircle2, FileDiff, Loader2, ShieldCheck } from 'lucide-react';

import {
  useProtocolActions,
  useProtocolDeviations,
  useProtocolRegistrations,
  useResearchProtocols,
  useResearchQuestions,
} from '@/hooks/useResearchProtocols';
import type {
  ProtocolSnapshot,
  ResearchProtocolResponse,
  ResearchProtocolVersionResponse,
  ResearchQuestionResponse,
} from '@/services/researchProtocolService';
import { useAuthStore } from '@/stores/authStore';

const SNAPSHOT_SECTIONS = [
  'eligibility',
  'sources_search',
  'selection',
  'extraction',
  'appraisal_synthesis',
  'outcomes',
  'reviewer_mode',
] as const;

type SnapshotSection = (typeof SNAPSHOT_SECTIONS)[number];
type SnapshotDraft = Record<SnapshotSection, string>;

const EMPTY_SNAPSHOT = Object.fromEntries(
  SNAPSHOT_SECTIONS.map((section) => [section, '{}'])
) as SnapshotDraft;

interface ProtocolPanelProps {
  projectId: string;
  blueprintId?: string;
  readOnly: boolean;
  onApprovedVersionChange: (versionId?: string) => void;
}

function displayDate(value?: string | null): string {
  return value ? new Date(value).toLocaleString() : 'Not recorded';
}

function newIdempotencyKey(prefix: string): string {
  return `${prefix}-${crypto.randomUUID()}`;
}

function idempotencyKeyFor(
  previous: { fingerprint: string; key: string } | undefined,
  fingerprint: string,
  prefix: string
): { fingerprint: string; key: string } {
  return previous?.fingerprint === fingerprint
    ? previous
    : { fingerprint, key: newIdempotencyKey(prefix) };
}

function parseSnapshot(draft: SnapshotDraft): ProtocolSnapshot {
  return Object.fromEntries(
    SNAPSHOT_SECTIONS.map((section) => {
      const value: unknown = JSON.parse(draft[section]);
      if (
        typeof value !== 'object' ||
        value === null ||
        Array.isArray(value) ||
        Object.keys(value).length === 0
      ) {
        throw new Error(`${section} must define at least one field`);
      }
      return [section, value];
    })
  ) as ProtocolSnapshot;
}

export function ProtocolPanel(props: ProtocolPanelProps): ReactElement {
  const questions = useResearchQuestions(props.projectId);
  const protocols = useResearchProtocols(props.projectId);
  const [selectedQuestionId, setSelectedQuestionId] = useState<string>();
  const [selectedProtocolId, setSelectedProtocolId] = useState<string>();

  if (questions.isLoading || protocols.isLoading) {
    return (
      <p role="status" className="text-sm text-muted-foreground">
        Loading protocol…
      </p>
    );
  }
  if (questions.error || protocols.error) {
    return (
      <p role="alert" className="text-sm text-destructive">
        {questions.error?.message ?? protocols.error?.message}
      </p>
    );
  }

  const questionOptions = questions.data ?? [];
  const protocolOptions = protocols.data ?? [];
  const question =
    questionOptions.find((candidate) => candidate.id === selectedQuestionId) ??
    questionOptions[0];
  const protocol =
    protocolOptions.find((candidate) => candidate.id === selectedProtocolId) ??
    protocolOptions[0];
  return (
    <ProtocolPanelContent
      key={`${question?.current_version_id ?? 'no-question'}:${protocol?.current_draft_version_id ?? 'no-protocol'}`}
      {...props}
      question={question}
      protocol={protocol}
      questionOptions={questionOptions}
      protocolOptions={protocolOptions}
      onQuestionSelect={setSelectedQuestionId}
      onProtocolSelect={setSelectedProtocolId}
    />
  );
}

function snapshotDraft(
  version?: ResearchProtocolVersionResponse
): SnapshotDraft {
  if (!version) return { ...EMPTY_SNAPSHOT };
  return Object.fromEntries(
    SNAPSHOT_SECTIONS.map((section) => [
      section,
      JSON.stringify(version.snapshot[section], null, 2),
    ])
  ) as SnapshotDraft;
}

function VersionDiff({
  current,
  parent,
}: {
  current: ResearchProtocolVersionResponse;
  parent?: ResearchProtocolVersionResponse;
}): ReactElement {
  if (!parent) {
    return (
      <p className="text-sm text-muted-foreground">Initial protocol version.</p>
    );
  }
  const changed = SNAPSHOT_SECTIONS.filter(
    (section) =>
      JSON.stringify(parent.snapshot[section]) !==
      JSON.stringify(current.snapshot[section])
  );
  return (
    <div className="space-y-3">
      <p className="text-sm text-muted-foreground">
        {changed.length === 0
          ? 'No snapshot sections changed.'
          : `Changed sections: ${changed.join(', ')}`}
      </p>
      {changed.map((section) => (
        <details key={section} className="rounded-md border border-border p-3">
          <summary className="cursor-pointer text-sm font-medium">
            {section}
          </summary>
          <div className="mt-3 grid gap-3 md:grid-cols-2">
            <pre className="overflow-auto rounded bg-muted p-3 text-xs">
              {JSON.stringify(parent.snapshot[section], null, 2)}
            </pre>
            <pre className="overflow-auto rounded bg-muted p-3 text-xs">
              {JSON.stringify(current.snapshot[section], null, 2)}
            </pre>
          </div>
        </details>
      ))}
      <details className="rounded-md border border-border p-3">
        <summary className="cursor-pointer text-sm font-medium">
          Execution plan
        </summary>
        <div className="mt-3 grid gap-3 md:grid-cols-2">
          <pre className="overflow-auto rounded bg-muted p-3 text-xs">
            {JSON.stringify(parent.execution_plan, null, 2)}
          </pre>
          <pre className="overflow-auto rounded bg-muted p-3 text-xs">
            {JSON.stringify(current.execution_plan, null, 2)}
          </pre>
        </div>
      </details>
    </div>
  );
}

interface ProtocolPanelContentProps extends ProtocolPanelProps {
  question?: ResearchQuestionResponse;
  protocol?: ResearchProtocolResponse;
  questionOptions: ResearchQuestionResponse[];
  protocolOptions: ResearchProtocolResponse[];
  onQuestionSelect: (questionId: string) => void;
  onProtocolSelect: (protocolId: string) => void;
}

function ProtocolPanelContent({
  projectId,
  blueprintId,
  readOnly,
  onApprovedVersionChange,
  question,
  protocol,
  questionOptions,
  protocolOptions,
  onQuestionSelect,
  onProtocolSelect,
}: ProtocolPanelContentProps): ReactElement {
  const actorUserId = useAuthStore((state) => state.user?.id ?? null);
  const actions = useProtocolActions(projectId);
  const versions = useMemo(
    () => protocol?.versions ?? [],
    [protocol?.versions]
  );
  const draftVersion = versions.find(
    (version) => version.id === protocol?.current_draft_version_id
  );
  const approvedVersion = versions.find(
    (version) => version.id === protocol?.current_approved_version_id
  );
  const approvedVersionMatchesBlueprint = Boolean(
    approvedVersion && blueprintId === approvedVersion.blueprint_id
  );
  const registrations = useProtocolRegistrations(projectId, protocol?.id);
  const deviations = useProtocolDeviations(projectId, protocol?.id);

  const [questionText, setQuestionText] = useState(
    question?.current_version.question ?? ''
  );
  const [hypothesis, setHypothesis] = useState(
    question?.current_version.hypothesis ?? ''
  );
  const [scope, setScope] = useState(question?.current_version.scope ?? '');
  const [protocolName, setProtocolName] = useState('Research protocol');
  const [snapshot, setSnapshot] = useState<SnapshotDraft>(() =>
    snapshotDraft(draftVersion)
  );
  const [amendmentReason, setAmendmentReason] = useState('');
  const [approvalReason, setApprovalReason] = useState('');
  const [registrationProvider, setRegistrationProvider] = useState('');
  const [registrationId, setRegistrationId] = useState('');
  const [registrationUrl, setRegistrationUrl] = useState('');
  const [formError, setFormError] = useState<string | null>(null);
  const approvalAttempt = useRef<{ fingerprint: string; key: string }>();
  const registrationAttempt = useRef<{ fingerprint: string; key: string }>();

  useEffect(() => {
    onApprovedVersionChange(
      approvedVersionMatchesBlueprint ? approvedVersion?.id : undefined
    );
  }, [
    approvedVersion?.id,
    approvedVersionMatchesBlueprint,
    onApprovedVersionChange,
  ]);

  const parseCurrentSnapshot = (): ProtocolSnapshot | null => {
    try {
      const parsed = parseSnapshot(snapshot);
      setFormError(null);
      return parsed;
    } catch {
      setFormError(
        'Every protocol section must contain a non-empty JSON object.'
      );
      return null;
    }
  };

  const saveQuestion = (): void => {
    if (!questionText.trim()) return;
    const body = {
      question: questionText.trim(),
      hypothesis: hypothesis.trim() || null,
      scope: scope.trim() || null,
      framework: {},
      ...(question ? { parent_version_id: question.current_version_id } : {}),
    };
    if (question) {
      actions.createQuestionVersion.mutate({ questionId: question.id, body });
    } else {
      actions.createQuestion.mutate(body);
    }
  };

  const saveProtocol = (): void => {
    const parsed = parseCurrentSnapshot();
    if (!parsed || !question?.current_version_id || !blueprintId) return;
    if (!protocol) {
      actions.createProtocol.mutate({
        name: protocolName.trim(),
        question_version_id: question.current_version_id,
        blueprint_id: blueprintId,
        snapshot: parsed,
      });
      return;
    }
    if (!draftVersion || !amendmentReason.trim()) return;
    actions.createVersion.mutate({
      protocolId: protocol.id,
      body: {
        question_version_id: question.current_version_id,
        blueprint_id: blueprintId,
        snapshot: parsed,
        parent_version_id: draftVersion.id,
        amendment_reason: amendmentReason.trim(),
      },
    });
    setAmendmentReason('');
  };

  const approve = (): void => {
    if (!protocol || !draftVersion || !approvalReason.trim()) return;
    const fingerprint = JSON.stringify({
      protocolId: protocol.id,
      versionId: draftVersion.id,
      version: draftVersion.version,
      hash: draftVersion.content_hash,
      approvedPointer: protocol.current_approved_version_id ?? null,
      reason: approvalReason.trim(),
      actorUserId,
    });
    approvalAttempt.current = idempotencyKeyFor(
      approvalAttempt.current,
      fingerprint,
      'protocol-approval'
    );
    actions.approve.mutate(
      {
        protocolId: protocol.id,
        versionId: draftVersion.id,
        body: {
          expected_protocol_version: draftVersion.version,
          expected_content_hash: draftVersion.content_hash,
          expected_current_approved_version_id:
            protocol.current_approved_version_id ?? null,
          reason: approvalReason.trim(),
          idempotency_key: approvalAttempt.current.key,
        },
      },
      {
        onSuccess: () => setApprovalReason(''),
      }
    );
  };

  const recordRegistration = (): void => {
    if (
      !protocol ||
      !approvedVersion ||
      !registrationProvider.trim() ||
      !registrationId.trim()
    )
      return;
    const fingerprint = JSON.stringify({
      protocolId: protocol.id,
      versionId: approvedVersion.id,
      hash: approvedVersion.content_hash,
      provider: registrationProvider.trim(),
      externalIdentifier: registrationId.trim(),
      url: registrationUrl.trim() || null,
      actorUserId,
    });
    registrationAttempt.current = idempotencyKeyFor(
      registrationAttempt.current,
      fingerprint,
      'protocol-registration'
    );
    actions.register.mutate(
      {
        protocolId: protocol.id,
        body: {
          protocol_version_id: approvedVersion.id,
          protocol_version_hash: approvedVersion.content_hash,
          provider: registrationProvider.trim(),
          external_identifier: registrationId.trim(),
          url: registrationUrl.trim() || null,
          receipt: null,
          status: 'registered',
          failure_reason: null,
          idempotency_key: registrationAttempt.current.key,
        },
      },
      {
        onSuccess: () => {
          setRegistrationProvider('');
          setRegistrationId('');
          setRegistrationUrl('');
        },
      }
    );
  };

  const mutationError = Object.values(actions).find(
    (action) => action.error
  )?.error;

  return (
    <section className="space-y-5 rounded-xl border border-border bg-card p-5">
      <div>
        <h2 className="font-medium text-foreground">
          Approved research protocol
        </h2>
        <p className="mt-1 text-sm text-muted-foreground">
          Runs use the exact approved protocol version and content hash. Editing
          creates a new draft amendment.
        </p>
      </div>

      {(questionOptions.length > 1 || protocolOptions.length > 1) && (
        <div className="grid gap-3 sm:grid-cols-2">
          {questionOptions.length > 1 && (
            <label className="text-xs font-medium text-muted-foreground">
              Research question record
              <select
                value={question?.id}
                onChange={(event) => onQuestionSelect(event.target.value)}
                className="mt-1 w-full rounded-md border border-border bg-background px-3 py-2 text-sm text-foreground"
              >
                {questionOptions.map((option) => (
                  <option key={option.id} value={option.id}>
                    {option.current_version.question}
                  </option>
                ))}
              </select>
            </label>
          )}
          {protocolOptions.length > 1 && (
            <label className="text-xs font-medium text-muted-foreground">
              Protocol record
              <select
                value={protocol?.id}
                onChange={(event) => onProtocolSelect(event.target.value)}
                className="mt-1 w-full rounded-md border border-border bg-background px-3 py-2 text-sm text-foreground"
              >
                {protocolOptions.map((option) => (
                  <option key={option.id} value={option.id}>
                    {option.name}
                  </option>
                ))}
              </select>
            </label>
          )}
        </div>
      )}

      <fieldset disabled={readOnly} className="space-y-3">
        <legend className="text-sm font-medium">Research question</legend>
        <textarea
          aria-label="Research question"
          value={questionText}
          onChange={(event) => setQuestionText(event.target.value)}
          className="min-h-20 w-full rounded-md border border-border bg-background p-3 text-sm"
        />
        <input
          aria-label="Hypothesis"
          value={hypothesis}
          onChange={(event) => setHypothesis(event.target.value)}
          placeholder="Hypothesis (optional)"
          className="w-full rounded-md border border-border bg-background px-3 py-2 text-sm"
        />
        <input
          aria-label="Question scope"
          value={scope}
          onChange={(event) => setScope(event.target.value)}
          placeholder="Scope (optional)"
          className="w-full rounded-md border border-border bg-background px-3 py-2 text-sm"
        />
        <button
          type="button"
          onClick={saveQuestion}
          disabled={
            !questionText.trim() ||
            actions.createQuestion.isPending ||
            actions.createQuestionVersion.isPending
          }
          className="rounded-md border border-border px-3 py-2 text-sm font-medium disabled:opacity-50"
        >
          {question ? 'Create question version' : 'Save question'}
        </button>
      </fieldset>

      {question && (
        <details>
          <summary className="cursor-pointer text-sm font-medium">
            Question version history
          </summary>
          <ol className="mt-2 space-y-2 text-sm">
            {(question.versions ?? [question.current_version]).map(
              (version) => (
                <li
                  key={version.id}
                  className="rounded border border-border p-3"
                >
                  <span className="font-medium">Version {version.version}</span>
                  <span className="ml-2 text-muted-foreground">
                    {version.content_hash.slice(0, 12)}
                  </span>
                  <p className="mt-1">{version.question}</p>
                  <p className="mt-1 text-xs text-muted-foreground">
                    Author {version.author_user_id} ·{' '}
                    {displayDate(version.created_at)}
                  </p>
                </li>
              )
            )}
          </ol>
        </details>
      )}

      {question && (
        <fieldset
          disabled={readOnly || protocol?.can_edit === false}
          className="space-y-3"
        >
          <legend className="text-sm font-medium">Method snapshot</legend>
          {!protocol && (
            <input
              aria-label="Protocol name"
              value={protocolName}
              onChange={(event) => setProtocolName(event.target.value)}
              className="w-full rounded-md border border-border bg-background px-3 py-2 text-sm"
            />
          )}
          <div className="grid gap-3 lg:grid-cols-2">
            {SNAPSHOT_SECTIONS.map((section) => (
              <label
                key={section}
                className="text-xs font-medium text-muted-foreground"
              >
                {section}
                <textarea
                  aria-label={`Protocol ${section}`}
                  value={snapshot[section]}
                  onChange={(event) =>
                    setSnapshot((current) => ({
                      ...current,
                      [section]: event.target.value,
                    }))
                  }
                  className="mt-1 min-h-28 w-full rounded-md border border-border bg-background p-2 font-mono text-xs text-foreground"
                />
              </label>
            ))}
          </div>
          {protocol && (
            <textarea
              aria-label="Amendment reason"
              value={amendmentReason}
              onChange={(event) => setAmendmentReason(event.target.value)}
              placeholder="Why is this amendment needed?"
              className="min-h-20 w-full rounded-md border border-border bg-background p-3 text-sm"
            />
          )}
          {!blueprintId && (
            <p className="text-sm text-muted-foreground">
              Save an execution blueprint before creating a protocol version.
            </p>
          )}
          <button
            type="button"
            onClick={saveProtocol}
            disabled={
              !blueprintId ||
              !protocolName.trim() ||
              !question.current_version_id ||
              (Boolean(protocol) && !amendmentReason.trim()) ||
              actions.createProtocol.isPending ||
              actions.createVersion.isPending
            }
            className="rounded-md bg-primary px-4 py-2 text-sm font-medium text-primary-foreground disabled:opacity-50"
          >
            {protocol ? 'Create amendment' : 'Create protocol draft'}
          </button>
        </fieldset>
      )}

      {protocol && versions.length > 0 && (
        <div className="space-y-3">
          <h3 className="flex items-center gap-2 text-sm font-medium">
            <FileDiff className="h-4 w-4" aria-hidden="true" />
            Protocol history
          </h3>
          <ol className="space-y-3">
            {versions.map((version) => {
              const parent = versions.find(
                (candidate) => candidate.id === version.parent_version_id
              );
              return (
                <li
                  key={version.id}
                  className="rounded-md border border-border p-3"
                >
                  <div className="flex flex-wrap items-center gap-2 text-sm">
                    <strong>Version {version.version}</strong>
                    <span className="rounded bg-muted px-2 py-0.5 text-xs">
                      {version.status}
                    </span>
                    <code className="text-xs text-muted-foreground">
                      {version.content_hash.slice(0, 12)}
                    </code>
                  </div>
                  <p className="mt-1 text-xs text-muted-foreground">
                    Author {version.author_user_id} · created{' '}
                    {displayDate(version.created_at)}
                  </p>
                  <p className="mt-1 text-xs text-muted-foreground">
                    Execution blueprint {version.blueprint_id}
                  </p>
                  {version.approved_by_user_id && (
                    <p className="mt-1 text-xs text-muted-foreground">
                      Approved by {version.approved_by_user_id} ·{' '}
                      {displayDate(version.approved_at)}
                    </p>
                  )}
                  {version.amendment_reason && (
                    <p className="mt-2 text-sm">
                      Amendment: {version.amendment_reason}
                    </p>
                  )}
                  <details className="mt-2">
                    <summary className="cursor-pointer text-sm">
                      Compare snapshot
                    </summary>
                    <div className="mt-2">
                      <VersionDiff current={version} parent={parent} />
                    </div>
                  </details>
                </li>
              );
            })}
          </ol>
        </div>
      )}

      {protocol &&
        draftVersion &&
        draftVersion.can_approve &&
        draftVersion.status === 'draft' && (
          <div className="rounded-md border border-border p-4">
            <h3 className="flex items-center gap-2 text-sm font-medium">
              <ShieldCheck className="h-4 w-4" aria-hidden="true" />
              Supervisor approval
            </h3>
            <p className="mt-1 text-xs text-muted-foreground">
              Approval is bound to version {draftVersion.version} and hash{' '}
              {draftVersion.content_hash}.
            </p>
            <textarea
              aria-label="Approval reason"
              value={approvalReason}
              onChange={(event) => setApprovalReason(event.target.value)}
              className="mt-3 min-h-20 w-full rounded-md border border-border bg-background p-3 text-sm"
              placeholder="Approval reason"
            />
            <button
              type="button"
              onClick={approve}
              disabled={!approvalReason.trim() || actions.approve.isPending}
              className="mt-2 rounded-md bg-primary px-4 py-2 text-sm font-medium text-primary-foreground disabled:opacity-50"
            >
              Approve exact version
            </button>
          </div>
        )}

      {protocol &&
        !draftVersion?.can_approve &&
        draftVersion?.status === 'draft' && (
          <p className="text-sm text-muted-foreground">
            An independent assigned supervisor must approve this exact draft
            version.
          </p>
        )}

      {approvedVersion ? (
        <div className="rounded-md border border-primary/30 bg-primary/5 p-4">
          <h3 className="flex items-center gap-2 text-sm font-medium">
            <CheckCircle2 className="h-4 w-4" aria-hidden="true" />
            Approved version {approvedVersion.version}
          </h3>
          <p className="mt-1 break-all text-xs text-muted-foreground">
            Effective plan hash: {approvedVersion.content_hash}
          </p>
          {!approvedVersionMatchesBlueprint && (
            <p className="mt-2 text-sm text-destructive">
              The current execution blueprint differs from this approved
              version. Create and approve an amendment before starting a run.
            </p>
          )}
        </div>
      ) : (
        <p className="rounded-md border border-amber-500/30 bg-amber-500/5 p-3 text-sm">
          No approved protocol exists. Draft generation and research runs remain
          unavailable.
        </p>
      )}

      {protocol && approvedVersion && (
        <details>
          <summary className="cursor-pointer text-sm font-medium">
            Recorded registration receipts
          </summary>
          <p className="mt-2 text-xs text-muted-foreground">
            This records a receipt from an external registry; it does not submit
            the protocol.
          </p>
          <ul className="mt-2 space-y-2 text-sm">
            {(registrations.data ?? []).map((registration) => (
              <li
                key={registration.id}
                className="rounded border border-border p-2"
              >
                {registration.provider}: {registration.external_identifier} (
                {registration.status})
              </li>
            ))}
          </ul>
          {!readOnly && protocol.can_edit && (
            <div className="mt-3 grid gap-2 md:grid-cols-3">
              <input
                aria-label="Registry provider"
                value={registrationProvider}
                onChange={(event) =>
                  setRegistrationProvider(event.target.value)
                }
                placeholder="Registry provider"
                className="rounded-md border border-border bg-background px-3 py-2 text-sm"
              />
              <input
                aria-label="External registration identifier"
                value={registrationId}
                onChange={(event) => setRegistrationId(event.target.value)}
                placeholder="External identifier"
                className="rounded-md border border-border bg-background px-3 py-2 text-sm"
              />
              <input
                aria-label="Registration URL"
                value={registrationUrl}
                onChange={(event) => setRegistrationUrl(event.target.value)}
                placeholder="Receipt URL (optional)"
                className="rounded-md border border-border bg-background px-3 py-2 text-sm"
              />
              <button
                type="button"
                onClick={recordRegistration}
                disabled={
                  !registrationProvider.trim() ||
                  !registrationId.trim() ||
                  actions.register.isPending
                }
                className="rounded-md border border-border px-3 py-2 text-sm disabled:opacity-50"
              >
                Record receipt
              </button>
            </div>
          )}
        </details>
      )}

      {protocol && (deviations.data?.length ?? 0) > 0 && (
        <details>
          <summary className="cursor-pointer text-sm font-medium">
            Recorded deviations ({deviations.data?.length})
          </summary>
          <ul className="mt-2 space-y-2 text-sm">
            {deviations.data?.map((deviation) => (
              <li
                key={deviation.id}
                className="rounded border border-border p-2"
              >
                <strong>{deviation.disposition}</strong>:{' '}
                {deviation.observed_difference}
                <p className="text-xs text-muted-foreground">
                  {deviation.rationale}
                </p>
              </li>
            ))}
          </ul>
        </details>
      )}

      {(formError || mutationError) && (
        <p role="alert" className="text-sm text-destructive">
          {formError ?? mutationError?.message}
        </p>
      )}
      {Object.values(actions).some((action) => action.isPending) && (
        <p
          role="status"
          className="flex items-center gap-2 text-sm text-muted-foreground"
        >
          <Loader2 className="h-4 w-4 animate-spin" aria-hidden="true" />
          Saving protocol record…
        </p>
      )}
    </section>
  );
}
