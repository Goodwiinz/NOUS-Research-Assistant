'use client';

import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import Link from 'next/link';
import { useSearchParams } from 'next/navigation';
import { Suspense } from 'react';

import { Alert, AlertDescription } from '@/components/ui/alert';
import { useAuth } from '@/hooks/useAuth';
import { scopeLabel } from '@/lib/integrations/scopeLabels';
import { api } from '@/services/api-client';
import type { ApiBindingKind } from '@/types/api/integration-connections-contract';
import type { components } from '@/types/generated/api';

type GrantRequest = components['schemas']['GrantRequestDTO'];
type GrantDecision = components['schemas']['GrantDecision'];
type AccountSummary = NonNullable<GrantRequest['account_summary']>;

const KNOWN_KINDS: readonly ApiBindingKind[] = [
  'project',
  'workspace',
  'account',
];

const counted = (count: number, one: string, many: string): string =>
  `${count} ${count === 1 ? one : many}`;

/** What an account approval reaches: the rule the server applies on every
 * call (design §2), and today's counts under it. */
function AccountCoverage({
  summary,
}: {
  summary: AccountSummary | null | undefined;
}): React.JSX.Element {
  return (
    <dd className="space-y-1">
      <p>
        Every project in the workspaces you own or belong to in your
        organization, including projects added later. Archived and deleted
        workspaces and projects are not included.
      </p>
      {summary && (
        <p>{`Today: ${counted(summary.workspace_count, 'workspace', 'workspaces')} and ${counted(summary.project_count, 'project', 'projects')}.`}</p>
      )}
    </dd>
  );
}

function ApprovalContent(): React.JSX.Element {
  const requestId = useSearchParams().get('request_id');
  const { user, isAuthenticated } = useAuth();
  const queryClient = useQueryClient();
  const queryKey = ['integration-grant-request', user?.id, requestId];
  const request = useQuery({
    queryKey,
    enabled: Boolean(requestId && isAuthenticated),
    queryFn: () =>
      api.get<GrantRequest>(
        `/integrations/grant-requests/${encodeURIComponent(requestId ?? '')}`
      ),
    staleTime: 0,
    gcTime: 0,
  });
  const decision = useMutation({
    mutationFn: (data: GrantDecision) =>
      api.post<GrantRequest>(
        `/integrations/grant-requests/${encodeURIComponent(requestId ?? '')}/decision`,
        data
      ),
    onSuccess: (data) => queryClient.setQueryData(queryKey, data),
  });
  const consent = request.data;
  // The API derives the expiry state from the authoritative server clock.
  // Avoid a client clock comparison during render, which can drift and is
  // rejected by React's render-purity lint rule.
  const expired = consent?.status === 'expired';
  const canDecide =
    consent?.status === 'pending' && !expired && !decision.isPending;
  // A backend older than account connections sends no binding_kind; its
  // requests are told apart by what they bind: a workspace, a project, or
  // (neither) the whole account. A kind this page does not know, or a project
  // or workspace request without its id, cannot be described truthfully, so
  // it is refused rather than shown as a project.
  const wireKind: string | null | undefined = consent?.binding_kind;
  const kind: ApiBindingKind | null = !consent
    ? null
    : wireKind == null
      ? consent.workspace_id
        ? 'workspace'
        : consent.project_id
          ? 'project'
          : 'account'
      : KNOWN_KINDS.includes(wireKind as ApiBindingKind)
        ? (wireKind as ApiBindingKind)
        : null;
  const shown =
    kind === 'account' ||
    (kind === 'workspace' && Boolean(consent?.workspace_id)) ||
    (kind === 'project' && Boolean(consent?.project_id));
  const isAccount = kind === 'account';
  const canApprove = canDecide && shown;

  return (
    <section className="mx-auto max-w-xl space-y-6 p-6">
      <h1 className="text-2xl font-semibold">Approve integration access</h1>
      <p>
        Only approve a request you started. Check the device, project, workspace
        or whole account, and permissions below against your terminal.
      </p>
      <p>
        <Link href="/integrations/devices" className="underline">
          Manage connected devices
        </Link>
      </p>
      {!requestId && (
        <p role="alert">This approval link is missing its request ID.</p>
      )}
      {request.isPending && requestId && <p role="status">Loading request…</p>}
      {(request.isError || decision.isError) && (
        <p role="alert">
          This request could not be processed. It may have expired or access may
          have changed.
        </p>
      )}
      {consent && (
        <>
          {!shown && <p role="alert">This request can&apos;t be shown here.</p>}
          {shown && (
            <>
              {isAccount && (
                <h2 className="text-xl font-semibold">{`${consent.device_label} wants access to your whole NOUS account`}</h2>
              )}
              <dl className="space-y-3 break-words">
                <div>
                  <dt className="font-semibold">Device</dt>
                  <dd>
                    {consent.device_label} ({consent.device_id})
                  </dd>
                </div>
                {isAccount ? (
                  <div>
                    <dt className="font-semibold">Covers</dt>
                    <AccountCoverage summary={consent.account_summary} />
                  </div>
                ) : consent.workspace_id ? (
                  <div>
                    <dt className="font-semibold">Workspace</dt>
                    <dd>
                      {consent.workspace_label} ({consent.workspace_id})
                    </dd>
                  </div>
                ) : (
                  <div>
                    <dt className="font-semibold">Project</dt>
                    <dd>
                      {consent.project_label} ({consent.project_id})
                    </dd>
                  </div>
                )}
                {consent.thread_id && (
                  <div>
                    <dt className="font-semibold">Chat</dt>
                    <dd>
                      {consent.thread_label} ({consent.thread_id})
                    </dd>
                  </div>
                )}
                <div>
                  <dt className="font-semibold">Request</dt>
                  <dd>{consent.id}</dd>
                </div>
                <div>
                  <dt className="font-semibold">Permissions</dt>
                  <dd>
                    <ul>
                      {consent.scopes.map((scope) => (
                        <li key={scope}>
                          {scopeLabel(scope, kind ?? undefined)}{' '}
                          <code>{scope}</code>
                        </li>
                      ))}
                    </ul>
                  </dd>
                </div>
                <div>
                  <dt className="font-semibold">Expires</dt>
                  <dd>{new Date(consent.expires_at).toLocaleString()}</dd>
                </div>
              </dl>
              {isAccount && consent.scopes.includes('library:write') && (
                <Alert variant="destructive">
                  <AlertDescription className="space-y-2">
                    <p className="font-medium">
                      This device can change your library without asking.
                    </p>
                    <p>
                      It can make reversible changes in every project in your
                      account without asking you first: adding, removing, moving
                      and renaming items, and changing a paper&apos;s title and
                      tags. Deleting folders and ingesting papers still ask you
                      first. You can revoke this access at any time from Manage
                      connected devices.
                    </p>
                  </AlertDescription>
                </Alert>
              )}
            </>
          )}
          <p role="status">
            {expired && consent.status === 'pending'
              ? 'expired'
              : consent.status}
          </p>
          {consent.scopes.includes('context:read') &&
            (consent.status === 'approved' ||
              consent.status === 'consumed') && (
              <p>
                <Link
                  href={`/integrations/context/${encodeURIComponent(consent.id)}`}
                  className="underline"
                >
                  Choose which project memories this device may read
                </Link>
              </p>
            )}
          <div className="flex gap-3">
            <button
              type="button"
              disabled={!canDecide}
              onClick={() => decision.mutate({ approved: false })}
              className="rounded border px-4 py-2 disabled:opacity-50"
            >
              Deny
            </button>
            <button
              type="button"
              disabled={!canApprove}
              onClick={() => decision.mutate({ approved: true })}
              className="rounded bg-primary px-4 py-2 text-primary-foreground disabled:opacity-50"
            >
              Approve access
            </button>
          </div>
        </>
      )}
    </section>
  );
}

export default function IntegrationApprovalPage(): React.JSX.Element {
  return (
    <Suspense fallback={<p role="status">Loading request…</p>}>
      <ApprovalContent />
    </Suspense>
  );
}
