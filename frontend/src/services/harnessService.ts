'use client';

import { api } from '@/services/api-client';
import type {
  ApiAccountWorkspaceBinding,
  ApiWorkspaceBinding,
} from '@/types/api/integration-connections-contract';
import type { components } from '@/types/generated/api';

export type HarnessDevice = components['schemas']['DeviceListItemDTO'];
/**
 * An account folder as the composer and Connected devices see it. The wire
 * shape (`AccountWorkspaceBindingDTO`) names no project; `project_id: null`
 * is the view-model tag `listFolders` adds so one list can hold both kinds
 * and a folder's kind is read from the one field.
 */
export type HarnessAccountFolder = ApiAccountWorkspaceBinding & {
  project_id: null;
};
export type HarnessWorkspace = ApiWorkspaceBinding | HarnessAccountFolder;
export type NativeDecision = components['schemas']['NativeDecision'];
export type NativeRequestView = components['schemas']['NativeRequestDTO'];

/** Browser-authenticated control surface. The CLI grant token stays on device. */
export const harnessService = {
  listDevices(): Promise<HarnessDevice[]> {
    return api.get('/integrations/devices');
  },
  /** The device's project folders (each tied to one project). */
  listWorkspaces(deviceId: string): Promise<ApiWorkspaceBinding[]> {
    return api.get(
      `/integrations/devices/${encodeURIComponent(deviceId)}/workspaces`
    );
  },
  /** The device's account folders; empty unless its account approval lives. */
  listAccountWorkspaces(
    deviceId: string
  ): Promise<ApiAccountWorkspaceBinding[]> {
    return api.get(
      `/integrations/devices/${encodeURIComponent(deviceId)}/account-workspaces`
    );
  },
  /**
   * Every folder of the device: project folders, then account folders. The
   * frontend can deploy before a backend that has `/account-workspaces`, so
   * a failed account request lists no account folders instead of hiding the
   * project folders; a failed project request still rejects.
   */
  async listFolders(deviceId: string): Promise<HarnessWorkspace[]> {
    const [project, account] = await Promise.all([
      this.listWorkspaces(deviceId),
      this.listAccountWorkspaces(deviceId).catch(
        (): ApiAccountWorkspaceBinding[] => []
      ),
    ]);
    return [
      ...project,
      ...account.map((folder): HarnessAccountFolder => ({
        ...folder,
        project_id: null,
      })),
    ];
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
