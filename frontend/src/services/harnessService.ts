'use client';

import { api } from '@/services/api-client';
import type { components } from '@/types/generated/api';

export type HarnessDevice = components['schemas']['DeviceDTO'];
export type HarnessWorkspace = components['schemas']['WorkspaceBindingDTO'];
export type NativeDecision = components['schemas']['NativeDecision'];
export type NativeRequestView = components['schemas']['NativeRequestDTO'];

/** Browser-authenticated control surface. The CLI grant token stays on device. */
export const harnessService = {
  listDevices(): Promise<HarnessDevice[]> {
    return api.get('/integrations/devices');
  },
  listWorkspaces(deviceId: string): Promise<HarnessWorkspace[]> {
    return api.get(
      `/integrations/devices/${encodeURIComponent(deviceId)}/workspaces`
    );
  },
  readRequest(requestId: string): Promise<NativeRequestView> {
    return api.get(`/harness/requests/${encodeURIComponent(requestId)}`);
  },
  decideRequest(requestId: string, decision: NativeDecision): Promise<void> {
    return api.post(
      `/harness/requests/${encodeURIComponent(requestId)}/decision`,
      decision
    );
  },
};
