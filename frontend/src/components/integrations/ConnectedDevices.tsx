'use client';

import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import Link from 'next/link';
import { useId, useState, type ReactElement } from 'react';

import { getSelectedThreadUrl } from '@/components/chat/shared/chatNavigation';
import { useAuth } from '@/hooks/useAuth';
import { scopeSummary } from '@/lib/integrations/scopeLabels';
import { harnessService } from '@/services/harnessService';
import { integrationConnectionsService } from '@/services/integrationConnectionsService';
import type { ApiConnectionConsent } from '@/types/api/integration-connections-contract';

// A consent reaches one project, every project of one workspace (Plan 07),
// or every project in the account (account connections).
const consentTarget = (consent: ApiConnectionConsent): string =>
  consent.kind === 'account'
    ? 'Whole account'
    : consent.kind === 'workspace'
      ? `Workspace ${consent.workspace_label ?? ''} (${consent.workspace_id ?? ''}), every project in it`
      : `${consent.project_label ?? ''} (${consent.project_id ?? ''})`;

// Names a consent apart from its siblings in control labels: two consents of
// one device for the same project differ only by the chat they are bound to.
// Truthiness, not `!== null`: a backend older than this page omits thread_id.
// An account consent is never chat-bound.
const consentName = (consent: ApiConnectionConsent): string => {
  const target =
    consent.kind === 'account'
      ? 'your whole account'
      : consent.kind === 'workspace'
        ? `workspace ${consent.workspace_label ?? ''}`
        : (consent.project_label ?? '');
  if (!consent.thread_id) return target;
  return consent.thread_label
    ? `${target}, only from chat ${consent.thread_label}`
    : `${target}, only from a chat that is no longer available`;
};

/**
 * Folders on this computer that belong to no project (account folders): any
 * chat in the account can run Codex in them. Read through the same query key
 * as the chat composer's folder list, so the folder list has one cache.
 */
function AccountFolders({
  deviceId,
  deviceLabel,
  userId,
}: {
  deviceId: string;
  deviceLabel: string;
  userId: string | undefined;
}): ReactElement {
  const headingId = useId();
  const folders = useQuery({
    queryKey: ['harness', 'workspaces', userId, deviceId],
    enabled: Boolean(userId),
    queryFn: () => harnessService.listWorkspaces(deviceId),
    retry: false,
    staleTime: 0,
  });
  const accountFolders =
    folders.data?.filter((folder) => folder.project_id === null) ?? [];
  return (
    <div className="space-y-1">
      <h3 id={headingId} className="text-sm font-semibold">
        Account folders
        <span className="sr-only">{` on ${deviceLabel} (${deviceId})`}</span>
      </h3>
      {folders.isPending && (
        <p role="status" className="text-sm">
          Loading folders…
        </p>
      )}
      {folders.isError && (
        <p role="alert" className="text-sm">
          Account folders could not be loaded.
        </p>
      )}
      {folders.isSuccess && accountFolders.length === 0 && (
        <p className="text-sm text-muted-foreground">
          No account folders on this computer yet.
        </p>
      )}
      {accountFolders.length > 0 && (
        <ul aria-labelledby={headingId} className="list-disc pl-5 text-sm">
          {accountFolders.map((folder) => (
            <li key={folder.workspace_id}>{folder.label}</li>
          ))}
        </ul>
      )}
    </div>
  );
}

/**
 * Connected coding devices and what each may do. Revoking takes effect on
 * the device's next request.
 */
export function ConnectedDevices(): ReactElement {
  const { user } = useAuth();
  const [confirmDeviceId, setConfirmDeviceId] = useState<string | null>(null);
  const queryClient = useQueryClient();
  const queryKey = ['integration-connections', user?.id];
  const connections = useQuery({
    queryKey,
    enabled: Boolean(user?.id),
    queryFn: () => integrationConnectionsService.list(),
    retry: false,
    staleTime: 0,
  });
  // The chat composer offers the computers and folders read from the same
  // access, under ['harness', ...] with a 30 s staleTime: refresh them with
  // the list so a revoked computer is not offered for the rest of that time.
  const refresh = async (): Promise<void> => {
    await Promise.all([
      queryClient.invalidateQueries({ queryKey }),
      queryClient.invalidateQueries({ queryKey: ['harness'] }),
    ]);
  };
  const revokeConsent = useMutation({
    mutationFn: (requestId: string) =>
      integrationConnectionsService.revokeConsent(requestId),
    onSettled: refresh,
  });
  const disconnect = useMutation({
    mutationFn: (deviceId: string) =>
      integrationConnectionsService.disconnect(deviceId),
    onSettled: refresh,
    onSuccess: () => setConfirmDeviceId(null),
  });
  const busy = revokeConsent.isPending || disconnect.isPending;

  return (
    <section className="mx-auto max-w-2xl space-y-6 p-6">
      <h1 className="text-2xl font-semibold">Connected devices</h1>
      <p>
        Devices you paired with nous-harness connect. Revoking access stops the
        device on its next request.
      </p>
      {connections.isPending && <p role="status">Loading devices…</p>}
      {connections.isError && (
        <p role="alert">Connected devices could not be loaded.</p>
      )}
      {(revokeConsent.isError || disconnect.isError) && (
        <p role="alert">
          That access could not be revoked. It may already have been removed.
        </p>
      )}
      {connections.data?.length === 0 && <p>No devices are connected.</p>}
      <ul className="space-y-4">
        {connections.data?.map((device) => (
          <li key={device.device_id} className="space-y-3 rounded border p-4">
            <div className="flex items-center justify-between gap-3">
              <h2 className="font-semibold">
                {device.device_label}{' '}
                <span className="text-sm font-normal text-muted-foreground">
                  connected {new Date(device.connected_at).toLocaleString()}
                </span>
              </h2>
              <button
                type="button"
                disabled={busy}
                onClick={() => setConfirmDeviceId(device.device_id)}
                className="rounded border px-3 py-1 text-sm disabled:opacity-50"
                aria-label={`Disconnect ${device.device_label} (${device.device_id})`}
              >
                Disconnect
              </button>
            </div>
            <p className="break-all text-xs text-muted-foreground">
              Device ID: {device.device_id}
            </p>
            {confirmDeviceId === device.device_id && (
              <div className="space-y-2 rounded border p-3">
                <p>
                  Disconnect {device.device_label} ({device.device_id})? This
                  revokes its access and ends all existing CLI sign-ins for your
                  account, so every other connected device also stops working
                  until you run nous-harness connect on it again. Each reconnect
                  adds a new device here. Revoke the old device&apos;s access
                  rather than disconnecting the old device, which would end
                  every CLI sign-in again; the old device then stays listed with
                  no access.
                </p>
                <button
                  type="button"
                  disabled={busy}
                  onClick={() => disconnect.mutate(device.device_id)}
                  className="rounded border px-3 py-1 text-sm disabled:opacity-50"
                >
                  Disconnect and end CLI sign-ins
                </button>{' '}
                <button
                  type="button"
                  disabled={busy}
                  onClick={() => setConfirmDeviceId(null)}
                  className="rounded border px-3 py-1 text-sm disabled:opacity-50"
                >
                  Cancel
                </button>
              </div>
            )}
            {device.consents.length === 0 ? (
              <p className="text-sm">No active project access.</p>
            ) : (
              <ul className="space-y-2">
                {device.consents.map((consent) => (
                  <li
                    key={consent.request_id}
                    className="flex items-start justify-between gap-3"
                  >
                    <div className="space-y-1">
                      <p>{consentTarget(consent)}</p>
                      {consent.thread_id && (
                        <p className="text-sm">
                          Only from chat:{' '}
                          {consent.thread_label ? (
                            <Link
                              href={getSelectedThreadUrl(consent.thread_id)}
                              className="underline"
                            >
                              {consent.thread_label}
                            </Link>
                          ) : (
                            'a chat that is no longer available'
                          )}
                        </p>
                      )}
                      <p className="text-sm text-muted-foreground">
                        {consent.scopes
                          .map((scope) => scopeSummary(scope, consent.kind))
                          .join(', ')}
                      </p>
                      {consent.scopes.includes('context:read') && (
                        <p className="text-sm">
                          <Link
                            href={`/integrations/context/${encodeURIComponent(consent.request_id)}`}
                            className="underline"
                          >
                            Choose shared memories
                            <span className="sr-only">{` for ${consentName(consent)}`}</span>
                          </Link>
                        </p>
                      )}
                    </div>
                    <button
                      type="button"
                      disabled={busy}
                      onClick={() => revokeConsent.mutate(consent.request_id)}
                      className="rounded border px-3 py-1 text-sm disabled:opacity-50"
                      aria-label={`Revoke ${device.device_label} (${device.device_id}) access to ${consentName(consent)}`}
                    >
                      Revoke
                    </button>
                  </li>
                ))}
              </ul>
            )}
            {device.consents.some((consent) => consent.kind === 'account') && (
              <AccountFolders
                deviceId={device.device_id}
                deviceLabel={device.device_label}
                userId={user?.id}
              />
            )}
          </li>
        ))}
      </ul>
    </section>
  );
}
