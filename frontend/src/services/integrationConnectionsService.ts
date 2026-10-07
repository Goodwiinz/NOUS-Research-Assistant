import { api } from '@/services/api-client';
import type { ApiConnectedDevice } from '@/types/api/integration-connections-contract';

/** Browser-only: the backend refuses these calls from a connected device. */
export const integrationConnectionsService = {
  list(): Promise<ApiConnectedDevice[]> {
    return api.get<ApiConnectedDevice[]>('/integrations/connections');
  },
  revokeConsent(requestId: string): Promise<void> {
    return api.post<void>(
      `/integrations/grant-requests/${encodeURIComponent(requestId)}/revoke`
    );
  },
  disconnect(deviceId: string): Promise<void> {
    return api.post<void>(
      `/integrations/devices/${encodeURIComponent(deviceId)}/revoke`
    );
  },
};
