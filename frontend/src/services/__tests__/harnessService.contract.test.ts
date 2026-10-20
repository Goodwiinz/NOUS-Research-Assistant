/**
 * Contract tests for the harnessService folder routes: project folders come
 * from `/workspaces`, account folders from `/account-workspaces`, and
 * `listFolders` merges them with a `project_id: null` tag on account folders.
 */
import { beforeEach, describe, expect, it, vi } from 'vitest';

import { api } from '@/services/api-client';
import { harnessService } from '@/services/harnessService';

vi.mock('@/services/api-client', () => ({
  api: {
    get: vi.fn(),
    post: vi.fn(),
  },
}));

const projectFolder = {
  label: 'paper-code',
  workspace_id: 'folder-1',
  project_id: 'project-1',
};
const accountFolder = { label: 'scratch', workspace_id: 'folder-2' };

function routeGet(responses: Record<string, unknown>): void {
  vi.mocked(api.get).mockImplementation((url: string) => {
    const suffix = Object.keys(responses).find((key) => url.endsWith(key));
    if (!suffix) return Promise.reject(new Error(`unexpected GET ${url}`));
    const response = responses[suffix];
    return response instanceof Error
      ? Promise.reject(response)
      : Promise.resolve(response);
  });
}

describe('harnessService folder contract', () => {
  beforeEach(() => vi.clearAllMocks());

  it('listAccountWorkspaces GETs the account-workspaces route', async () => {
    vi.mocked(api.get).mockResolvedValue([accountFolder]);

    await expect(
      harnessService.listAccountWorkspaces('dev 1')
    ).resolves.toEqual([accountFolder]);
    expect(api.get).toHaveBeenCalledWith(
      '/integrations/devices/dev%201/account-workspaces'
    );
  });

  it('listFolders lists project folders, then tagged account folders', async () => {
    routeGet({
      '/workspaces': [projectFolder],
      '/account-workspaces': [accountFolder],
    });

    await expect(harnessService.listFolders('dev-1')).resolves.toEqual([
      projectFolder,
      { ...accountFolder, project_id: null },
    ]);
  });

  it('listFolders keeps project folders when the account route fails', async () => {
    routeGet({
      '/workspaces': [projectFolder],
      '/account-workspaces': new Error('404 Not Found'),
    });

    await expect(harnessService.listFolders('dev-1')).resolves.toEqual([
      projectFolder,
    ]);
  });

  it('listFolders rejects when the project route fails', async () => {
    routeGet({
      '/workspaces': new Error('500'),
      '/account-workspaces': [accountFolder],
    });

    await expect(harnessService.listFolders('dev-1')).rejects.toThrow('500');
  });
});
