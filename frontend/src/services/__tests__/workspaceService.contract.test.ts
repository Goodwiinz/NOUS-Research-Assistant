/**
 * Characterization tests for the workspaceService HTTP boundary.
 *
 * These pin CURRENT behavior (exact URL, request body, and response
 * passthrough shape) before workspaceService.ts is migrated to the
 * generated-OpenAPI type aliases (frontend/src/types/api/workspace-contract.ts).
 * The service does no runtime normalization today — it forwards whatever the
 * caller passes and returns whatever the backend sends — so these tests must
 * keep passing unchanged after the type-only migration (Task 3.2).
 */
import { beforeEach, describe, expect, it, vi } from 'vitest';

import { api } from '@/services/api-client';
import { workspaceService } from '@/services/workspaceService';

vi.mock('@/services/api-client', () => ({
  api: {
    get: vi.fn(),
    post: vi.fn(),
    patch: vi.fn(),
    delete: vi.fn(),
    request: vi.fn(),
  },
}));

describe('workspaceService contract (characterization)', () => {
  beforeEach(() => vi.clearAllMocks());

  describe('workspace', () => {
    it('listWorkspaces: GETs the collection and returns the array as-is', async () => {
      const workspaces = [
        {
          id: 'ws-1',
          name: 'Research',
          description: null,
          is_public: false,
          owner_id: 'user-1',
          organization_id: null,
          is_archived: false,
          member_count: null,
          conversation_count: 3,
          collection_count: null,
          created_at: '2026-07-01T00:00:00Z',
          updated_at: '2026-07-01T00:00:00Z',
        },
      ];
      vi.mocked(api.get).mockResolvedValue(workspaces);

      const result = await workspaceService.listWorkspaces();

      expect(api.get).toHaveBeenCalledWith('/api/v2/workspaces');
      // Nullable fields (description, organization_id, member_count,
      // collection_count) round-trip as `null`, not coerced to `undefined`.
      expect(result).toEqual(workspaces);
      expect(result[0].description).toBeNull();
    });

    it('createWorkspace: POSTs the exact payload and returns the response as-is', async () => {
      const payload = { name: 'My Workspace', description: undefined, is_public: false };
      const created = {
        id: 'ws-2',
        name: 'My Workspace',
        description: null,
        is_public: false,
        owner_id: 'user-1',
        is_archived: false,
        created_at: '2026-07-15T00:00:00Z',
        updated_at: '2026-07-15T00:00:00Z',
      };
      vi.mocked(api.post).mockResolvedValue(created);

      const result = await workspaceService.createWorkspace(payload);

      expect(api.post).toHaveBeenCalledWith('/api/v2/workspaces', payload);
      expect(result).toEqual(created);
    });

    it('getWorkspace: GETs by id and returns the detail response (with members) as-is', async () => {
      const detail = {
        id: 'ws-1',
        name: 'Research',
        is_public: false,
        owner_id: 'user-1',
        is_archived: false,
        created_at: '2026-07-01T00:00:00Z',
        updated_at: '2026-07-01T00:00:00Z',
        members: [
          {
            id: 'mem-1',
            workspace_id: 'ws-1',
            user_id: 'user-1',
            role: 'owner',
            joined_at: '2026-07-01T00:00:00Z',
            created_at: '2026-07-01T00:00:00Z',
            updated_at: '2026-07-01T00:00:00Z',
          },
        ],
      };
      vi.mocked(api.get).mockResolvedValue(detail);

      const result = await workspaceService.getWorkspace('ws-1');

      expect(api.get).toHaveBeenCalledWith('/api/v2/workspaces/ws-1');
      expect(result).toEqual(detail);
    });

    it('updateWorkspace: PATCHes the exact payload to the workspace URL', async () => {
      const patch = { name: 'Renamed' };
      const updated = {
        id: 'ws-1',
        name: 'Renamed',
        is_public: false,
        owner_id: 'user-1',
        is_archived: false,
        created_at: '2026-07-01T00:00:00Z',
        updated_at: '2026-07-15T00:00:00Z',
      };
      vi.mocked(api.patch).mockResolvedValue(updated);

      const result = await workspaceService.updateWorkspace('ws-1', patch);

      expect(api.patch).toHaveBeenCalledWith('/api/v2/workspaces/ws-1', patch);
      expect(result).toEqual(updated);
    });
  });

  describe('conversation', () => {
    it('listConversations: builds the query string from page/limit/search', async () => {
      const response = {
        conversations: [],
        total: 0,
        page: 1,
        limit: 20,
        has_more: false,
      };
      vi.mocked(api.get).mockResolvedValue(response);

      const result = await workspaceService.listConversations('ws-1', {
        page: 1,
        limit: 20,
        search: 'arxiv',
      });

      expect(api.get).toHaveBeenCalledWith(
        '/api/v2/workspaces/ws-1/conversations?page=1&limit=20&search=arxiv'
      );
      expect(result).toEqual(response);
    });

    it('createConversation: POSTs under the workspace_id embedded in the payload', async () => {
      const payload = {
        workspace_id: 'ws-1',
        title: 'New Chat',
        description: 'A new conversation',
      };
      const created = {
        id: 'conv-1',
        workspace_id: 'ws-1',
        created_by_id: 'user-1',
        title: 'New Chat',
        description: 'A new conversation',
        is_archived: false,
        is_pinned: false,
        last_activity_at: '2026-07-15T00:00:00Z',
        created_at: '2026-07-15T00:00:00Z',
        updated_at: '2026-07-15T00:00:00Z',
      };
      vi.mocked(api.post).mockResolvedValue(created);

      const result = await workspaceService.createConversation(payload);

      expect(api.post).toHaveBeenCalledWith(
        '/api/v2/workspaces/ws-1/conversations',
        payload
      );
      expect(result).toEqual(created);
    });
  });

  describe('thread', () => {
    it('listWorkspaceThreads: GETs the globally paginated workspace thread page', async () => {
      const response = {
        threads: [
          {
            id: 'thread-1',
            conversation_id: 'conv-historical',
            status: 'active',
            last_message_at: '2026-09-12T12:00:00Z',
            message_count: 2,
            token_count: 10,
            created_at: '2026-09-12T11:00:00Z',
            updated_at: '2026-09-12T12:00:00Z',
          },
        ],
        total: 1,
        page: 1,
        limit: 50,
        has_more: false,
      };
      vi.mocked(api.get).mockResolvedValue(response);

      const result = await workspaceService.listWorkspaceThreads('ws-1', {
        page: 1,
        limit: 50,
        statusFilter: 'active',
      });

      expect(api.get).toHaveBeenCalledWith(
        '/api/v2/workspaces/ws-1/threads?page=1&limit=50&status_filter=active'
      );
      expect(result).toEqual(response);
      expect(result.threads[0].conversation_id).toBe('conv-historical');
    });

    it('listThreads: returns the qualified chat ThreadListResponse shape as-is', async () => {
      const response = {
        threads: [
          {
            id: 'thread-1',
            conversation_id: 'conv-1',
            status: 'active',
            last_message_at: '2026-07-15T00:00:00Z',
            message_count: 2,
            token_count: 10,
            created_at: '2026-07-15T00:00:00Z',
            updated_at: '2026-07-15T00:00:00Z',
            // Bound to a research project via ThreadCreate.project_id at
            // creation time; the read side reports it under a different
            // field name, source_project_id.
            source_project_id: 'proj-1',
          },
        ],
        total: 1,
        page: 1,
        limit: 20,
        has_more: false,
      };
      vi.mocked(api.get).mockResolvedValue(response);

      const result = await workspaceService.listThreads('conv-1', {
        page: 1,
        limit: 20,
      });

      expect(api.get).toHaveBeenCalledWith(
        '/api/v2/conversations/conv-1/threads?page=1&limit=20'
      );
      expect(result).toEqual(response);
      expect(result.threads[0].source_project_id).toBe('proj-1');
    });

    it('createThread: forwards project_id verbatim (the create-side name for source_project_id)', async () => {
      const payload = { conversation_id: 'conv-1', project_id: 'proj-1' };
      const created = {
        id: 'thread-2',
        conversation_id: 'conv-1',
        status: 'active',
        last_message_at: '2026-07-15T00:00:00Z',
        message_count: 0,
        token_count: 0,
        created_at: '2026-07-15T00:00:00Z',
        updated_at: '2026-07-15T00:00:00Z',
        source_project_id: 'proj-1',
      };
      vi.mocked(api.post).mockResolvedValue(created);

      const result = await workspaceService.createThread(payload);

      // Exact payload: only what the caller supplied, no title/initial_message
      // keys synthesized, and the field is still named project_id on write.
      expect(api.post).toHaveBeenCalledWith('/api/v2/threads', payload);
      expect(result.source_project_id).toBe('proj-1');
    });

    it('getThread: appends include_messages and returns nested messages as-is', async () => {
      const detail = {
        id: 'thread-1',
        conversation_id: 'conv-1',
        status: 'active',
        last_message_at: '2026-07-15T00:00:00Z',
        message_count: 1,
        token_count: 5,
        created_at: '2026-07-15T00:00:00Z',
        updated_at: '2026-07-15T00:00:00Z',
        source_project_id: null,
        messages: [],
      };
      vi.mocked(api.get).mockResolvedValue(detail);

      const result = await workspaceService.getThread('thread-1', {
        includeMessages: true,
      });

      expect(api.get).toHaveBeenCalledWith(
        '/api/v2/threads/thread-1?include_messages=true'
      );
      expect(result).toEqual(detail);
      expect(result.source_project_id).toBeNull();
    });
  });

  describe('message', () => {
    it('listMessages: builds the query string and forwards the abort signal', async () => {
      const response = { messages: [], total: 0, page: 1, limit: 50, has_more: false };
      vi.mocked(api.get).mockResolvedValue(response);
      const controller = new AbortController();

      const result = await workspaceService.listMessages('thread-1', {
        limit: 50,
        order: 'asc',
        signal: controller.signal,
      });

      expect(api.get).toHaveBeenCalledWith(
        '/api/v2/threads/thread-1/messages?limit=50&order=asc',
        { signal: controller.signal }
      );
      expect(result).toEqual(response);
    });

    it('createMessage: forwards the payload verbatim without a default role', async () => {
      const payload = { thread_id: 'thread-1', content: 'hello' };
      const created = {
        id: 'msg-1',
        client_message_id: null,
        thread_id: 'thread-1',
        content: 'hello',
        role: 'user',
        token_count: 1,
        citations: [],
        attachments: [],
        created_at: '2026-07-15T00:00:00Z',
        updated_at: '2026-07-15T00:00:00Z',
      };
      vi.mocked(api.post).mockResolvedValue(created);

      const result = await workspaceService.createMessage(payload);

      // No `role` key synthesized client-side — the backend default applies.
      expect(api.post).toHaveBeenCalledWith('/api/v2/messages', payload);
      expect(result).toEqual(created);
      expect(result.client_message_id).toBeNull();
    });
  });
});
