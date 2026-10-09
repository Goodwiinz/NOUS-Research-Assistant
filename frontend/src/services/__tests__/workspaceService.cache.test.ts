import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import type { Workspace } from '@/types/workspace';

const LEGACY_KEYS = [
  'default-workspace-cached-at',
  'default-workspace-id',
  'default-conversation-id',
];

function stampedRecord(
  ownerUserId: string,
  workspace: Partial<Workspace>,
  cachedAt = Date.now()
): string {
  return JSON.stringify({ version: 1, ownerUserId, cachedAt, workspace });
}

function cachedRecord(): Record<string, unknown> | null {
  const raw = localStorage.getItem('default-workspace-object');
  return raw ? (JSON.parse(raw) as Record<string, unknown>) : null;
}

async function loadService(): Promise<
  typeof import('@/services/workspaceService')
> {
  vi.resetModules();
  return vi.importActual<typeof import('@/services/workspaceService')>(
    '@/services/workspaceService'
  );
}

describe('workspaceService default workspace cache', () => {
  beforeEach(() => {
    vi.resetModules();
    localStorage.clear();
  });

  afterEach(() => {
    localStorage.clear();
  });

  const cachedWorkspace = {
    id: 'ws-1',
    name: 'Cached Workspace',
    created_at: '2026-04-01T00:00:00Z',
    updated_at: '2026-04-01T00:00:00Z',
  };
  const liveWorkspace = {
    id: 'live-ws',
    name: 'Live Workspace',
    collection_count: 2,
    conversation_count: 3,
    created_at: '2026-04-02T00:00:00Z',
    updated_at: '2026-04-02T00:00:00Z',
  };

  it('revalidates a cached workspace owned by the caller before returning it', async () => {
    const { workspaceService } = await loadService();
    const freshWorkspace = {
      ...cachedWorkspace,
      name: 'Fresh Workspace',
      collection_count: 4,
      conversation_count: 9,
    };
    localStorage.setItem(
      'default-workspace-object',
      stampedRecord('user-A', cachedWorkspace)
    );

    const getWorkspaceSpy = vi
      .spyOn(workspaceService, 'getWorkspace')
      .mockResolvedValue(freshWorkspace as Workspace);
    const listWorkspacesSpy = vi
      .spyOn(workspaceService, 'listWorkspaces')
      .mockResolvedValue([]);

    const result = await workspaceService.getOrCreateDefaultWorkspace('user-A');

    expect(getWorkspaceSpy).toHaveBeenCalledWith('ws-1');
    expect(listWorkspacesSpy).not.toHaveBeenCalled();
    expect(result).toEqual(freshWorkspace);
    expect(cachedRecord()).toMatchObject({
      version: 1,
      ownerUserId: 'user-A',
      workspace: { id: 'ws-1', name: 'Fresh Workspace' },
    });
  });

  it('drops a deleted cached workspace and falls back to the server list', async () => {
    const { workspaceService } = await loadService();
    localStorage.setItem(
      'default-workspace-object',
      stampedRecord('user-A', { ...cachedWorkspace, id: 'deleted-ws' })
    );
    vi.spyOn(workspaceService, 'getWorkspace').mockRejectedValue({
      error: { status_code: 404 },
    });
    const listWorkspacesSpy = vi
      .spyOn(workspaceService, 'listWorkspaces')
      .mockResolvedValue([liveWorkspace as Workspace]);

    const result = await workspaceService.getOrCreateDefaultWorkspace('user-A');

    expect(listWorkspacesSpy).toHaveBeenCalledTimes(1);
    expect(result).toEqual(liveWorkspace);
    expect(cachedRecord()).toMatchObject({
      ownerUserId: 'user-A',
      workspace: { id: 'live-ws' },
    });
  });

  it('ignores and removes a cache owned by another account', async () => {
    // A lagging tab still signed in as A can write A's workspace after this
    // origin moved to B. B must neither request A's workspace nor see its
    // name, and the record must be gone before B's own lookup starts.
    const { workspaceService } = await loadService();
    localStorage.setItem(
      'default-workspace-object',
      stampedRecord('user-A', { ...cachedWorkspace, name: 'A private name' })
    );
    const getWorkspaceSpy = vi.spyOn(workspaceService, 'getWorkspace');
    const listWorkspacesSpy = vi
      .spyOn(workspaceService, 'listWorkspaces')
      .mockImplementation(async () => {
        expect(localStorage.getItem('default-workspace-object')).toBeNull();
        return [liveWorkspace as Workspace];
      });

    const result = await workspaceService.getOrCreateDefaultWorkspace('user-B');

    expect(getWorkspaceSpy).not.toHaveBeenCalled();
    expect(listWorkspacesSpy).toHaveBeenCalledTimes(1);
    expect(result).toEqual(liveWorkspace);
    expect(JSON.stringify(cachedRecord())).not.toContain('A private name');
    expect(cachedRecord()).toMatchObject({ ownerUserId: 'user-B' });
  });

  it('discards a legacy unstamped cache once and removes its loose keys', async () => {
    const { workspaceService } = await loadService();
    // What today's deployed code writes: no owner anywhere.
    localStorage.setItem(
      'default-workspace-object',
      JSON.stringify(cachedWorkspace)
    );
    localStorage.setItem('default-workspace-cached-at', String(Date.now()));
    localStorage.setItem('default-workspace-id', 'ws-1');
    localStorage.setItem('default-conversation-id', 'conv-1');
    const getWorkspaceSpy = vi.spyOn(workspaceService, 'getWorkspace');
    vi.spyOn(workspaceService, 'listWorkspaces').mockResolvedValue([
      liveWorkspace as Workspace,
    ]);

    await workspaceService.getOrCreateDefaultWorkspace('user-A');

    expect(getWorkspaceSpy).not.toHaveBeenCalled();
    for (const key of LEGACY_KEYS) expect(localStorage.getItem(key)).toBeNull();
    expect(cachedRecord()).toMatchObject({
      version: 1,
      ownerUserId: 'user-A',
      workspace: { id: 'live-ws' },
    });
  });

  it('neither reads nor writes the cache while the account is unknown', async () => {
    const { workspaceService } = await loadService();
    const record = stampedRecord('user-A', cachedWorkspace);
    localStorage.setItem('default-workspace-object', record);
    const getWorkspaceSpy = vi.spyOn(workspaceService, 'getWorkspace');
    vi.spyOn(workspaceService, 'listWorkspaces').mockResolvedValue([
      liveWorkspace as Workspace,
    ]);

    const result = await workspaceService.getOrCreateDefaultWorkspace(null);

    expect(result).toEqual(liveWorkspace);
    expect(getWorkspaceSpy).not.toHaveBeenCalled();
    expect(localStorage.getItem('default-workspace-object')).toBe(record);
  });

  it('writes only the stamped record, never a loose unstamped id', async () => {
    const { workspaceService } = await loadService();
    vi.spyOn(workspaceService, 'listWorkspaces').mockResolvedValue([
      liveWorkspace as Workspace,
    ]);
    vi.spyOn(workspaceService, 'listConversations').mockResolvedValue({
      conversations: [{ id: 'conv-1', workspace_id: 'live-ws' } as never],
      total: 1,
    } as never);

    await workspaceService.getOrCreateDefaultWorkspace('user-A');
    await workspaceService.getOrCreateDefaultConversation('live-ws');

    for (const key of LEGACY_KEYS) expect(localStorage.getItem(key)).toBeNull();
    expect(cachedRecord()).toMatchObject({
      version: 1,
      ownerUserId: 'user-A',
      workspace: { id: 'live-ws' },
    });
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
    const pending = workspaceService.getOrCreateDefaultWorkspace('user-A');
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

  it('tolerates a storage that refuses removals', async () => {
    // clearWorkspaceServiceCache runs inside the account-change sequence;
    // a SecurityError here must not abort the steps after it.
    const { workspaceService, clearWorkspaceServiceCache } =
      await loadService();
    vi.spyOn(Storage.prototype, 'removeItem').mockImplementation(() => {
      throw new DOMException('Storage access denied', 'SecurityError');
    });
    vi.spyOn(workspaceService, 'listWorkspaces').mockResolvedValue([
      liveWorkspace as Workspace,
    ]);

    expect(() => clearWorkspaceServiceCache()).not.toThrow();
    await expect(
      workspaceService.getOrCreateDefaultWorkspace('user-B')
    ).resolves.toEqual(liveWorkspace);
  });

  it('tolerates a storage that refuses reads and writes', async () => {
    const { workspaceService } = await loadService();
    localStorage.setItem(
      'default-workspace-object',
      stampedRecord('user-A', cachedWorkspace)
    );
    vi.spyOn(Storage.prototype, 'getItem').mockImplementation(() => {
      throw new DOMException('Storage access denied', 'SecurityError');
    });
    vi.spyOn(Storage.prototype, 'setItem').mockImplementation(() => {
      throw new DOMException('Quota exceeded', 'QuotaExceededError');
    });
    const getWorkspaceSpy = vi.spyOn(workspaceService, 'getWorkspace');
    vi.spyOn(workspaceService, 'listWorkspaces').mockResolvedValue([
      liveWorkspace as Workspace,
    ]);

    await expect(
      workspaceService.getOrCreateDefaultWorkspace('user-A')
    ).resolves.toEqual(liveWorkspace);
    expect(getWorkspaceSpy).not.toHaveBeenCalled();
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
