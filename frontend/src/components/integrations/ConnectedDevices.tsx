'use client';

import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import type { ReactElement } from 'react';

import { useAuth } from '@/hooks/useAuth';
import { integrationConnectionsService } from '@/services/integrationConnectionsService';

const SCOPE_TEXT: Record<string, string> = {
  'harness:execute': 'run coding sessions',
  'tools:read': 'read project documents',
  'tools:write': 'request notes (you approve each one)',
  'context:read': 'read memories you chose to share',
  'artifacts:publish': 'publish files to chats',
};

/**
 * Connected coding devices and what each may do. Revoking takes effect on
 * the device's next request.
 */
export function ConnectedDevices(): ReactElement {
  const { user } = useAuth();
  const queryClient = useQueryClient();
  const queryKey = ['integration-connections', user?.id];
  const connections = useQuery({
    queryKey,
    enabled: Boolean(user?.id),
    queryFn: () => integrationConnectionsService.list(),
    retry: false,
    staleTime: 0,
  });
  const refresh = (): Promise<void> =>
    queryClient.invalidateQueries({ queryKey });
  const revokeConsent = useMutation({
    mutationFn: (requestId: string) =>
      integrationConnectionsService.revokeConsent(requestId),
    onSettled: refresh,
  });
  const disconnect = useMutation({
    mutationFn: (deviceId: string) =>
      integrationConnectionsService.disconnect(deviceId),
    onSettled: refresh,
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
                  connected {new Date(device.connected_at).toLocaleDateString()}
                </span>
              </h2>
              <button
                type="button"
                disabled={busy}
                onClick={() => disconnect.mutate(device.device_id)}
                className="rounded border px-3 py-1 text-sm disabled:opacity-50"
                aria-label={`Disconnect ${device.device_label}`}
              >
                Disconnect
              </button>
            </div>
            {device.consents.length === 0 ? (
              <p className="text-sm">No active project access.</p>
            ) : (
              <ul className="space-y-2">
                {device.consents.map((consent) => (
                  <li
                    key={consent.request_id}
                    className="flex items-start justify-between gap-3"
                  >
                    <div>
                      <p>
                        {consent.project_label} ({consent.project_id})
                      </p>
                      <p className="text-sm text-muted-foreground">
                        {consent.scopes
                          .map((scope) => SCOPE_TEXT[scope] ?? scope)
                          .join(', ')}
                      </p>
                    </div>
                    <button
                      type="button"
                      disabled={busy}
                      onClick={() => revokeConsent.mutate(consent.request_id)}
                      className="rounded border px-3 py-1 text-sm disabled:opacity-50"
                      aria-label={`Revoke ${device.device_label} access to ${consent.project_label}`}
                    >
                      Revoke
                    </button>
                  </li>
                ))}
              </ul>
            )}
          </li>
        ))}
      </ul>
    </section>
  );
}
