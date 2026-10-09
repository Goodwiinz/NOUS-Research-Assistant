import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import type { Workspace } from '@/types/workspace';
describe('workspaceService default workspace cache', () => {
  beforeEach(() => {
    vi.resetModules();
    localStorage.clear();
  });

  afterEach(() => {
    localStorage.clear();
  });

  it('revalidates a cached workspace before returning it', async () => {
    vi.resetModules();
    const { workspaceService } = await vi.importActual<
      typeof import('@/services/workspaceService')
    >('@/services/workspaceService');

    const cachedWorkspace = {
      id: 'ws-1',
      name: 'Cached Workspace',
      created_at: '2026-04-01T00:00:00Z',
      updated_at: '2026-04-01T00:00:00Z',
    };
    const freshWorkspace = {
      ...cachedWorkspace,
      name: 'Fresh Workspace',
      collection_count: 4,
      conversation_count: 9,
    };

    localStorage.setItem(
      'default-workspace-object',
      JSON.stringify(cachedWorkspace)
    );
    localStorage.setItem('default-workspace-cached-at', String(Date.now()));

    const getWorkspaceSpy = vi
      .spyOn(workspaceService, 'getWorkspace')
      .mockResolvedValue(freshWorkspace as Workspace);
    const listWorkspacesSpy = vi
      .spyOn(workspaceService, 'listWorkspaces')
      .mockResolvedValue([]);

    const result = await workspaceService.getOrCreateDefaultWorkspace();

    expect(getWorkspaceSpy).toHaveBeenCalledWith('ws-1');
    expect(listWorkspacesSpy).not.toHaveBeenCalled();
    expect(result).toEqual(freshWorkspace);
  });

  it('drops a deleted cached workspace and falls back to the server list', async () => {
    vi.resetModules();
    const { workspaceService } = await vi.importActual<
      typeof import('@/services/workspaceService')
    >('@/services/workspaceService');

    localStorage.setItem(
      'default-workspace-object',
      JSON.stringify({
        id: 'deleted-ws',
        name: 'Deleted Workspace',
        created_at: '2026-04-01T00:00:00Z',
        updated_at: '2026-04-01T00:00:00Z',
      })
    );
    localStorage.setItem('default-workspace-cached-at', String(Date.now()));

    vi.spyOn(workspaceService, 'getWorkspace').mockRejectedValue({
      response: { status: 404 },
    });

    const liveWorkspace = {
      id: 'live-ws',
      name: 'Live Workspace',
      collection_count: 2,
      conversation_count: 3,
      created_at: '2026-04-02T00:00:00Z',
      updated_at: '2026-04-02T00:00:00Z',
    };

    const listWorkspacesSpy = vi
      .spyOn(workspaceService, 'listWorkspaces')
      .mockResolvedValue([liveWorkspace as Workspace]);

    const result = await workspaceService.getOrCreateDefaultWorkspace();

    expect(listWorkspacesSpy).toHaveBeenCalledTimes(1);
    expect(result).toEqual(liveWorkspace);
    expect(localStorage.getItem('default-workspace-object')).toContain(
      'live-ws'
    );
  });

  it('cannot restore the old account’s cache after it was cleared', async () => {
    const { workspaceService, clearWorkspaceServiceCache } =
      await import('@/services/workspaceService');
    let finish!: (value: Workspace[]) => void;
    vi.spyOn(workspaceService, 'listWorkspaces').mockReturnValueOnce(
      new Promise((resolve) => {
        finish = resolve;
      })
    );
    const create = vi.spyOn(workspaceService, 'createWorkspace');
    const pending = workspaceService.getOrCreateDefaultWorkspace();
    const rejected = expect(pending).rejects.toMatchObject({
      name: 'AbortError',
    });
    clearWorkspaceServiceCache();
    finish([
      { id: 'private-workspace', name: 'Private workspace' } as Workspace,
    ]);
    await rejected;
    expect(localStorage.getItem('default-workspace-object')).toBeNull();
    expect(localStorage.getItem('default-workspace-id')).toBeNull();
    expect(create).not.toHaveBeenCalled();
  });

  it('leaves the client owner stamp to the auth store', async () => {
    // authStore reads the stamp, compares, clears, then re-stamps. A cache
    // clear that also dropped the stamp would be harmless there, but it must
    // never be the thing that decides ownership.
    const { clearWorkspaceServiceCache } =
      await import('@/services/workspaceService');
    const { CLIENT_OWNER_STORAGE_KEY } = await import('@/lib/client-owner');
    localStorage.setItem(CLIENT_OWNER_STORAGE_KEY, 'user-A');
    localStorage.setItem('chat-storage', '{"state":{}}');

    clearWorkspaceServiceCache();

    expect(localStorage.getItem('chat-storage')).toBeNull();
    expect(localStorage.getItem(CLIENT_OWNER_STORAGE_KEY)).toBe('user-A');
  });
});

describe('workspaceService message cancellation', () => {
  afterEach(() => {
    vi.doUnmock('@/services/api-client');
  });

  it('forwards the caller signal when listing messages', async () => {
    vi.resetModules();
    const get = vi.fn().mockResolvedValue({ messages: [], total: 0 });
    vi.doMock('@/services/api-client', () => ({ api: { get } }));

    const { workspaceService } = await import('@/services/workspaceService');
    const controller = new AbortController();

    await workspaceService.listMessages('thread-a', {
      limit: 50,
      signal: controller.signal,
    });

    expect(get).toHaveBeenCalledWith(
      '/api/v2/threads/thread-a/messages?limit=50',
      { signal: controller.signal }
    );
  });
});
