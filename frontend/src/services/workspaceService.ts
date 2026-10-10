/**
 * Workspace Service for NOUS thread-centric chat system
 * Communicates with backend /api/v2/workspaces/* endpoints
 */

// Pure passthrough shapes: sourced directly from the generated OpenAPI
// contract, no view-model narrowing needed (see types/workspace.ts for the
// handful that DO need narrowing, imported below).
import type {
  ApiBulkThreadResponse,
  ApiCollection,
  ApiCollectionCreate,
  ApiCollectionDetail,
  ApiCollectionListResponse,
  ApiCollectionUpdate,
  ApiConversation,
  ApiConversationCreate,
  ApiConversationListResponse,
  ApiConversationUpdate,
  ApiMessageUpdate,
  ApiThread,
  ApiThreadCreate,
  ApiThreadList,
  ApiThreadUpdate,
  ApiWorkspace,
  ApiWorkspaceDetail,
  ApiWorkspaceMember,
  ApiWorkspaceMemberCreate,
  ApiWorkspaceMemberUpdate,
  ApiWorkspaceThreadList,
  ApiWorkspaceUpdate,
} from '@/types/api/workspace-contract';
// View-model-aware shapes: these narrow/relax the generated contract (JSONB
// passthrough fields typed concretely, spuriously-required defaulted fields
// relaxed back to optional) — see the comments in types/workspace.ts.
import type {
  ChatMessage,
  ChatMessageCreate,
  ChatMessageListResponse,
  ThreadDetail,
  WorkspaceCreate,
} from '@/types/workspace';
import { api } from '@/services/api-client';

const API_PREFIX = '/api/v2';

const WS_CACHE_KEY = 'default-workspace-object';
const WS_CACHE_VERSION = 1;
const WS_CACHE_TTL_MS = 24 * 60 * 60 * 1000; // 24 hours
// Loose keys older builds wrote next to the record. Never read; removed
// whenever the record is removed or rewritten.
const LEGACY_WS_CACHE_KEYS = [
  'default-workspace-cached-at',
  'default-workspace-id',
  'default-conversation-id',
];

/**
 * The cached default workspace names the account it was fetched for. A
 * record for another account, or one without an owner (older builds), is
 * discarded unread: a lagging tab still signed in as A can write A's
 * workspace after this origin has moved to B, and B must neither request nor
 * display it. Without a known account the cache is neither read nor written.
 */
interface CachedWorkspaceRecord {
  version: typeof WS_CACHE_VERSION;
  ownerUserId: string;
  cachedAt: number;
  workspace: ApiWorkspace;
}

// Storage can be denied outright (SecurityError) or refuse writes (quota).
// The cache is an optimisation and the clear sequence must never stall on
// it, so every access below is guarded like lib/client-owner.ts.
function cacheStorage(): Storage | null {
  if (typeof window === 'undefined') return null;
  try {
    return window.localStorage;
  } catch {
    return null;
  }
}

function removeStorageKeys(keys: readonly string[]): void {
  const storage = cacheStorage();
  if (!storage) return;
  for (const key of keys) {
    try {
      storage.removeItem(key);
    } catch {
      // Best effort: a record that cannot be removed is still never used by
      // another account, because every reader checks the owner first.
    }
  }
}

function removeCachedWorkspace(): void {
  removeStorageKeys([WS_CACHE_KEY, ...LEGACY_WS_CACHE_KEYS]);
}

function readCachedWorkspace(
  ownerUserId: string,
  now = Date.now()
): ApiWorkspace | null {
  const storage = cacheStorage();
  if (!storage) return null;
  try {
    const raw = storage.getItem(WS_CACHE_KEY);
    if (!raw) return null;
    const record = JSON.parse(raw) as Partial<CachedWorkspaceRecord> | null;
    const owned =
      !!record &&
      record.version === WS_CACHE_VERSION &&
      record.ownerUserId === ownerUserId;
    const fresh =
      owned &&
      typeof record.cachedAt === 'number' &&
      now - record.cachedAt < WS_CACHE_TTL_MS;
    const workspace = fresh ? record.workspace : undefined;
    if (workspace && typeof workspace.id === 'string' && workspace.id) {
      return workspace;
    }
  } catch {
    // Unreadable or corrupted: treated like a foreign record below.
  }
  removeCachedWorkspace();
  return null;
}

function writeCachedWorkspace(
  ownerUserId: string,
  workspace: ApiWorkspace
): void {
  const storage = cacheStorage();
  if (!storage) return;
  const record: CachedWorkspaceRecord = {
    version: WS_CACHE_VERSION,
    ownerUserId,
    cachedAt: Date.now(),
    workspace,
  };
  try {
    storage.setItem(WS_CACHE_KEY, JSON.stringify(record));
  } catch {
    // Quota or policy: the next load lists workspaces again.
    return;
  }
  removeStorageKeys(LEGACY_WS_CACHE_KEYS);
}

// Collapses concurrent getOrCreateDefaultWorkspace callers onto one promise.
// The chat layout (useChatPersistence) and the chat page (useChatSession)
// both bootstrap the workspace on mount; without this, each does an
// independent listWorkspaces()->[]->createWorkspace() and a fresh user gets
// two "My Workspace" rows plus duplicate conversations.
let _defaultWorkspaceInFlight: Promise<ApiWorkspace> | null = null;
let cacheGeneration = 0;

function captureCacheGeneration(): () => void {
  const generation = cacheGeneration;
  return () => {
    if (generation !== cacheGeneration) {
      throw new DOMException('Chat session ended', 'AbortError');
    }
  };
}

// Collapses concurrent getOrCreateDefaultConversation callers (per workspace)
// onto one promise. useChatSession and useChatPersistence both bootstrap the
// default conversation on mount; without this, each does an independent
// listConversations()->[]->createConversation() and a fresh user gets two
// "New Chat" conversations plus divergent hook state.
const _defaultConversationInFlight = new Map<
  string,
  Promise<ApiConversation>
>();

/** Clear all workspace service caches (localStorage). Called on auth errors. */
export function clearWorkspaceServiceCache(): void {
  cacheGeneration += 1;
  removeCachedWorkspace();
  removeStorageKeys(['chat-storage']);
  _defaultWorkspaceInFlight = null;
  _defaultConversationInFlight.clear();
}

// ============================================================================
// Workspace Operations
// ============================================================================

export const workspaceService = {
  // Workspace CRUD
  async listWorkspaces(): Promise<ApiWorkspace[]> {
    return api.get<ApiWorkspace[]>(`${API_PREFIX}/workspaces`);
  },

  async createWorkspace(data: WorkspaceCreate): Promise<ApiWorkspace> {
    return api.post<ApiWorkspace>(`${API_PREFIX}/workspaces`, data);
  },

  async getWorkspace(workspaceId: string): Promise<ApiWorkspaceDetail> {
    return api.get<ApiWorkspaceDetail>(
      `${API_PREFIX}/workspaces/${workspaceId}`
    );
  },

  async updateWorkspace(
    workspaceId: string,
    data: ApiWorkspaceUpdate
  ): Promise<ApiWorkspace> {
    return api.patch<ApiWorkspace>(
      `${API_PREFIX}/workspaces/${workspaceId}`,
      data
    );
  },

  async deleteWorkspace(workspaceId: string): Promise<void> {
    await api.delete(`${API_PREFIX}/workspaces/${workspaceId}`);
  },

  async addWorkspaceMember(
    workspaceId: string,
    data: ApiWorkspaceMemberCreate
  ): Promise<ApiWorkspaceMember> {
    return api.post<ApiWorkspaceMember>(
      `${API_PREFIX}/workspaces/${workspaceId}/members`,
      data
    );
  },

  async updateWorkspaceMember(
    workspaceId: string,
    userId: string,
    data: ApiWorkspaceMemberUpdate
  ): Promise<ApiWorkspaceMember> {
    return api.patch<ApiWorkspaceMember>(
      `${API_PREFIX}/workspaces/${workspaceId}/members/${userId}`,
      data
    );
  },

  async removeWorkspaceMember(
    workspaceId: string,
    userId: string
  ): Promise<void> {
    await api.delete(
      `${API_PREFIX}/workspaces/${workspaceId}/members/${userId}`
    );
  },

  // ============================================================================
  // Conversation Operations
  // ============================================================================

  async listConversations(
    workspaceId: string,
    options: { page?: number; limit?: number; search?: string } = {}
  ): Promise<ApiConversationListResponse> {
    const params = new URLSearchParams();
    if (options.page) params.append('page', options.page.toString());
    if (options.limit) params.append('limit', options.limit.toString());
    if (options.search) params.append('search', options.search);

    const queryString = params.toString();
    const url = `${API_PREFIX}/workspaces/${workspaceId}/conversations${queryString ? `?${queryString}` : ''}`;
    return api.get<ApiConversationListResponse>(url);
  },

  async createConversation(
    data: ApiConversationCreate
  ): Promise<ApiConversation> {
    return api.post<ApiConversation>(
      `${API_PREFIX}/workspaces/${data.workspace_id}/conversations`,
      data
    );
  },

  async getConversation(conversationId: string): Promise<ApiConversation> {
    return api.get<ApiConversation>(
      `${API_PREFIX}/conversations/${conversationId}`
    );
  },

  async updateConversation(
    conversationId: string,
    data: ApiConversationUpdate
  ): Promise<ApiConversation> {
    return api.patch<ApiConversation>(
      `${API_PREFIX}/conversations/${conversationId}`,
      data
    );
  },

  async deleteConversation(conversationId: string): Promise<void> {
    await api.delete(`${API_PREFIX}/conversations/${conversationId}`);
  },

  // ============================================================================
  // Thread Operations
  // ============================================================================

  async listWorkspaceThreads(
    workspaceId: string,
    options: { page?: number; limit?: number; statusFilter?: string } = {}
  ): Promise<ApiWorkspaceThreadList> {
    const params = new URLSearchParams();
    if (options.page) params.append('page', options.page.toString());
    if (options.limit) params.append('limit', options.limit.toString());
    if (options.statusFilter) {
      params.append('status_filter', options.statusFilter);
    }

    const queryString = params.toString();
    const url = `${API_PREFIX}/workspaces/${workspaceId}/threads${queryString ? `?${queryString}` : ''}`;
    return api.get<ApiWorkspaceThreadList>(url);
  },

  async listThreads(
    conversationId: string,
    options: { page?: number; limit?: number } = {}
  ): Promise<ApiThreadList> {
    const params = new URLSearchParams();
    if (options.page) params.append('page', options.page.toString());
    if (options.limit) params.append('limit', options.limit.toString());

    const queryString = params.toString();
    const url = `${API_PREFIX}/conversations/${conversationId}/threads${queryString ? `?${queryString}` : ''}`;
    return api.get<ApiThreadList>(url);
  },

  async createThread(data: ApiThreadCreate): Promise<ApiThread> {
    return api.post<ApiThread>(`${API_PREFIX}/threads`, data);
  },

  async getThread(
    threadId: string,
    options: { includeMessages?: boolean } = {}
  ): Promise<ThreadDetail> {
    const params = new URLSearchParams({
      include_messages: String(options.includeMessages ?? true),
    });
    return api.get<ThreadDetail>(
      `${API_PREFIX}/threads/${threadId}?${params.toString()}`
    );
  },

  async updateThread(
    threadId: string,
    data: ApiThreadUpdate
  ): Promise<ApiThread> {
    return api.patch<ApiThread>(`${API_PREFIX}/threads/${threadId}`, data);
  },

  async deleteThread(threadId: string): Promise<void> {
    await api.delete(`${API_PREFIX}/threads/${threadId}`);
  },

  async regenerateThreadSummary(threadId: string): Promise<ApiThread> {
    return api.post<ApiThread>(`${API_PREFIX}/threads/${threadId}/summarize`);
  },

  // ============================================================================
  // Bulk Thread Operations
  // ============================================================================

  async bulkResolveThreads(
    threadIds: string[]
  ): Promise<ApiBulkThreadResponse> {
    return api.post<ApiBulkThreadResponse>(
      `${API_PREFIX}/threads/bulk/resolve`,
      {
        thread_ids: threadIds,
      }
    );
  },

  async bulkArchiveThreads(
    threadIds: string[]
  ): Promise<ApiBulkThreadResponse> {
    return api.post<ApiBulkThreadResponse>(
      `${API_PREFIX}/threads/bulk/archive`,
      {
        thread_ids: threadIds,
      }
    );
  },

  async bulkSummarizeThreads(
    threadIds: string[]
  ): Promise<ApiBulkThreadResponse> {
    return api.post<ApiBulkThreadResponse>(
      `${API_PREFIX}/threads/bulk/summarize`,
      { thread_ids: threadIds }
    );
  },

  async bulkDeleteThreads(threadIds: string[]): Promise<ApiBulkThreadResponse> {
    return api.request<ApiBulkThreadResponse>(`${API_PREFIX}/threads/bulk`, {
      method: 'DELETE',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ thread_ids: threadIds }),
    });
  },

  // ============================================================================
  // Message Operations
  // ============================================================================

  async listMessages(
    threadId: string,
    options: {
      page?: number;
      limit?: number;
      offset?: number;
      before_id?: string;
      order?: 'asc' | 'desc';
      signal?: AbortSignal;
    } = {}
  ): Promise<ChatMessageListResponse> {
    const params = new URLSearchParams();
    if (options.page) params.append('page', options.page.toString());
    if (options.limit) params.append('limit', options.limit.toString());
    if (options.offset !== undefined) {
      params.append('offset', options.offset.toString());
    }
    if (options.before_id) {
      params.append('before_id', options.before_id);
    }
    if (options.order) {
      params.append('order', options.order);
    }

    const queryString = params.toString();
    const url = `${API_PREFIX}/threads/${threadId}/messages${queryString ? `?${queryString}` : ''}`;
    return api.get<ChatMessageListResponse>(url, { signal: options.signal });
  },

  async createMessage(data: ChatMessageCreate): Promise<ChatMessage> {
    return api.post<ChatMessage>(`${API_PREFIX}/messages`, data);
  },

  async getMessage(messageId: string): Promise<ChatMessage> {
    return api.get<ChatMessage>(`${API_PREFIX}/messages/${messageId}`);
  },

  async updateMessage(
    messageId: string,
    data: ApiMessageUpdate
  ): Promise<ChatMessage> {
    return api.patch<ChatMessage>(`${API_PREFIX}/messages/${messageId}`, data);
  },

  async deleteMessage(messageId: string): Promise<void> {
    await api.delete(`${API_PREFIX}/messages/${messageId}`);
  },

  // ============================================================================
  // Collection Operations
  // ============================================================================

  async listCollections(
    workspaceId: string,
    options: { page?: number; limit?: number } = {}
  ): Promise<ApiCollectionListResponse> {
    const params = new URLSearchParams();
    if (options.page) params.append('page', options.page.toString());
    if (options.limit) params.append('limit', options.limit.toString());

    const queryString = params.toString();
    const url = `${API_PREFIX}/workspaces/${workspaceId}/collections${queryString ? `?${queryString}` : ''}`;
    return api.get<ApiCollectionListResponse>(url);
  },

  async createCollection(data: ApiCollectionCreate): Promise<ApiCollection> {
    return api.post<ApiCollection>(`${API_PREFIX}/collections`, data);
  },

  async getCollection(collectionId: string): Promise<ApiCollectionDetail> {
    return api.get<ApiCollectionDetail>(
      `${API_PREFIX}/collections/${collectionId}`
    );
  },

  async updateCollection(
    collectionId: string,
    data: ApiCollectionUpdate
  ): Promise<ApiCollection> {
    return api.patch<ApiCollection>(
      `${API_PREFIX}/collections/${collectionId}`,
      data
    );
  },

  async deleteCollection(collectionId: string): Promise<void> {
    await api.delete(`${API_PREFIX}/collections/${collectionId}`);
  },

  async addDocumentsToCollection(
    collectionId: string,
    documentIds: string[]
  ): Promise<ApiCollection> {
    return api.post<ApiCollection>(
      `${API_PREFIX}/collections/${collectionId}/documents`,
      { document_ids: documentIds }
    );
  },

  async removeDocumentsFromCollection(
    collectionId: string,
    documentIds: string[]
  ): Promise<void> {
    await api.request(`${API_PREFIX}/collections/${collectionId}/documents`, {
      method: 'DELETE',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ document_ids: documentIds }),
    });
  },

  // ============================================================================
  // Helper: Get or Create Default Workspace
  // ============================================================================

  /**
   * `ownerUserId` is the verified account making the request (null while it
   * is unknown). It decides whether the persisted cache may be read or
   * written; the request itself is authorized by the backend as always.
   */
  async getOrCreateDefaultWorkspace(
    ownerUserId: string | null
  ): Promise<ApiWorkspace> {
    if (_defaultWorkspaceInFlight) return _defaultWorkspaceInFlight;
    const inFlight = this._resolveDefaultWorkspace(ownerUserId).finally(() => {
      if (_defaultWorkspaceInFlight === inFlight)
        _defaultWorkspaceInFlight = null;
    });
    _defaultWorkspaceInFlight = inFlight;
    return inFlight;
  },

  async _resolveDefaultWorkspace(
    ownerUserId: string | null
  ): Promise<ApiWorkspace> {
    const assertCurrentSession = captureCacheGeneration();
    const cacheWorkspace = (ws: ApiWorkspace): void => {
      assertCurrentSession();
      // No account, no cache: an unstamped record could be read by anyone.
      if (ownerUserId) writeCachedWorkspace(ownerUserId, ws);
    };

    // Warm hit: a fresh record this account wrote, revalidated by one read
    // of the workspace itself instead of a list request.
    const cached = ownerUserId ? readCachedWorkspace(ownerUserId) : null;
    if (cached) {
      try {
        const workspace = await this.getWorkspace(cached.id);
        cacheWorkspace(workspace);
        return workspace;
      } catch (error: unknown) {
        assertCurrentSession();
        const apiError = error as { error?: { status_code?: number } };
        const status = apiError?.error?.status_code;
        if (status === 403 || status === 404) removeCachedWorkspace();
      }
    }

    // Try to get existing workspaces with retry for transient errors
    let retries = 2;
    while (retries > 0) {
      assertCurrentSession();
      try {
        const workspaces = await this.listWorkspaces();
        assertCurrentSession();
        if (workspaces.length > 0) {
          const bestWorkspace = workspaces.reduce((best, current) => {
            const bestScore =
              (best.collection_count ?? 0) + (best.conversation_count ?? 0);
            const currentScore =
              (current.collection_count ?? 0) +
              (current.conversation_count ?? 0);
            return currentScore > bestScore ? current : best;
          }, workspaces[0]);

          cacheWorkspace(bestWorkspace);
          return bestWorkspace;
        }
        // Successfully got an empty list — no workspaces exist yet
        break;
      } catch (error: unknown) {
        assertCurrentSession();
        retries--;
        if (retries > 0) {
          console.warn(
            '[WorkspaceService] Error listing workspaces, retrying...',
            error
          );
          await new Promise((resolve) => setTimeout(resolve, 500));
        } else {
          console.error(
            '[WorkspaceService] Failed to list workspaces after retries:',
            error
          );
          throw error;
        }
      }
    }

    console.log(
      '[WorkspaceService] No workspaces found, creating default workspace'
    );
    const newWorkspace = await this.createWorkspace({
      name: 'My Workspace',
      description: 'Default workspace',
      is_public: false,
    });

    cacheWorkspace(newWorkspace);
    return newWorkspace;
  },

  // ============================================================================
  // Helper: Get or Create Default Conversation
  // ============================================================================

  async getOrCreateDefaultConversation(
    workspaceId: string
  ): Promise<ApiConversation> {
    const existing = _defaultConversationInFlight.get(workspaceId);
    if (existing) return existing;
    const inFlight = this._resolveDefaultConversation(workspaceId).finally(
      () => {
        if (_defaultConversationInFlight.get(workspaceId) === inFlight) {
          _defaultConversationInFlight.delete(workspaceId);
        }
      }
    );
    _defaultConversationInFlight.set(workspaceId, inFlight);
    return inFlight;
  },

  async _resolveDefaultConversation(
    workspaceId: string
  ): Promise<ApiConversation> {
    const assertCurrentSession = captureCacheGeneration();

    try {
      const response = await this.listConversations(workspaceId, { limit: 1 });
      assertCurrentSession();
      if (response.conversations.length > 0) {
        return response.conversations[0];
      }
    } catch (error: unknown) {
      assertCurrentSession();
      const apiError = error as { error?: { status_code?: number } };
      const status = apiError?.error?.status_code;
      if (status === 404) {
        console.warn(
          '[WorkspaceService] Workspace not found (404), clearing stale data'
        );
        removeCachedWorkspace();
        throw error;
      }
      console.warn('[WorkspaceService] Error listing conversations:', error);
    }

    console.log('[WorkspaceService] Creating default conversation');
    return this.createConversation({
      workspace_id: workspaceId,
      title: 'New Chat',
      description: 'A new conversation',
    });
  },
};

export default workspaceService;
