/**
 * Unit tests for Project-Chat Store
 *
 * Tests Zustand store state management, actions, and selectors
 */

import { beforeEach, describe, expect, it, vi } from 'vitest';
import type { Mocked } from 'vitest';
import { act } from '@testing-library/react';
import {
  useProjectChatStore,
  selectProjectThreads,
  selectProjectLoading,
  selectProjectError,
} from '../projectChatStore';
import { projectChatService } from '@/services/projectChatService';
import type {
  ProjectThread,
  StartChatFromProjectResponse,
  ProjectThreadListResponse,
} from '@/types/project-chat';

// Mock the service
vi.mock('@/services/projectChatService', () => ({
  projectChatService: {
    listProjectThreads: vi.fn(),
    startChatFromProject: vi.fn(),
    linkThreadToProject: vi.fn(),
    unlinkThreadFromProject: vi.fn(),
    saveThreadToNote: vi.fn(),
  },
}));

const mockService = projectChatService as Mocked<typeof projectChatService>;

// ============================================================================
// Test Data Factories
// ============================================================================

const createMockProjectThread = (overrides: Partial<ProjectThread> = {}): ProjectThread => ({
  id: 'pt-123',
  project_id: 'proj-456',
  thread_id: 'thread-789',
  thread_title: 'Test Discussion',
  conversation_id: 'conv-101',
  link_type: 'manual',
  linked_at: new Date().toISOString(),
  linked_by_id: 'user-001',
  context_note: 'Test context',
  message_count: 5,
  last_message_at: new Date().toISOString(),
  ...overrides,
});

const createMockStartResponse = (): StartChatFromProjectResponse => ({
  thread_id: 'thread-new-123',
  conversation_id: 'conv-new-456',
  project_thread_id: 'pt-new-789',
  document_scope: ['doc-1', 'doc-2'],
});

// ============================================================================
// Test Suite
// ============================================================================

describe('useProjectChatStore', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    // Reset store to initial state
    act(() => {
      useProjectChatStore.getState().reset();
    });
  });

  // ==========================================================================
  // Initial State
  // ==========================================================================

  describe('initial state', () => {
    it('should have empty initial state', () => {
      const state = useProjectChatStore.getState();

      expect(state.linkedThreads).toEqual({});
      expect(state.loadingThreads).toEqual({});
      expect(state.startingChat).toEqual({});
      expect(state.linkingThread).toEqual({});
      expect(state.unlinkingThread).toEqual({});
      expect(state.savingToNote).toEqual({});
      expect(state.errors).toEqual({});
    });
  });

  // ==========================================================================
  // fetchProjectThreads
  // ==========================================================================

  describe('fetchProjectThreads', () => {
    const projectId = 'proj-123';

    it('should set loading state while fetching', async () => {
      const mockResponse: ProjectThreadListResponse = {
        threads: [],
        total: 0,
      };
      mockService.listProjectThreads.mockResolvedValue(mockResponse);

      const promise = useProjectChatStore.getState().fetchProjectThreads(projectId);

      // Loading should be true during fetch
      expect(useProjectChatStore.getState().loadingThreads[projectId]).toBe(true);

      await promise;
    });

    it('should store threads on success', async () => {
      const mockThreads = [
        createMockProjectThread({ id: 'pt-1' }),
        createMockProjectThread({ id: 'pt-2' }),
      ];
      const mockResponse: ProjectThreadListResponse = {
        threads: mockThreads,
        total: 2,
      };
      mockService.listProjectThreads.mockResolvedValue(mockResponse);

      await act(async () => {
        await useProjectChatStore.getState().fetchProjectThreads(projectId);
      });

      const state = useProjectChatStore.getState();
      expect(state.linkedThreads[projectId]).toHaveLength(2);
      expect(state.loadingThreads[projectId]).toBe(false);
      expect(state.errors[projectId]).toBeNull();
    });

    it('should set error state on failure', async () => {
      const error = new Error('Network error');
      mockService.listProjectThreads.mockRejectedValue(error);

      await act(async () => {
        await useProjectChatStore.getState().fetchProjectThreads(projectId);
      });

      const state = useProjectChatStore.getState();
      expect(state.errors[projectId]).toBe('Network error');
      expect(state.loadingThreads[projectId]).toBe(false);
    });

    it('should guard against invalid project ID', async () => {
      const consoleSpy = vi.spyOn(console, 'warn').mockImplementation();

      await act(async () => {
        await useProjectChatStore.getState().fetchProjectThreads('');
      });

      expect(mockService.listProjectThreads).not.toHaveBeenCalled();
      expect(consoleSpy).toHaveBeenCalledWith(
        '[ProjectChatStore] fetchProjectThreads called with invalid projectId:',
        ''
      );

      consoleSpy.mockRestore();
    });

    it('should guard against undefined project ID', async () => {
      const consoleSpy = vi.spyOn(console, 'warn').mockImplementation();

      await act(async () => {
        await useProjectChatStore.getState().fetchProjectThreads('undefined');
      });

      expect(mockService.listProjectThreads).not.toHaveBeenCalled();
      consoleSpy.mockRestore();
    });
  });

  // ==========================================================================
  // startChatFromProject
  // ==========================================================================

  describe('startChatFromProject', () => {
    const projectId = 'proj-123';
    const request = { initial_message: 'Hello' };

    it('should return response on success', async () => {
      const mockResponse = createMockStartResponse();
      mockService.startChatFromProject.mockResolvedValue(mockResponse);
      mockService.listProjectThreads.mockResolvedValue({ threads: [], total: 0 });

      let result: StartChatFromProjectResponse | null = null;
      await act(async () => {
        result = await useProjectChatStore.getState().startChatFromProject(projectId, request);
      });

      expect(result).toEqual(mockResponse);
    });

    it('should refresh threads after starting chat', async () => {
      mockService.startChatFromProject.mockResolvedValue(createMockStartResponse());
      mockService.listProjectThreads.mockResolvedValue({ threads: [], total: 0 });

      await act(async () => {
        await useProjectChatStore.getState().startChatFromProject(projectId, request);
      });

      expect(mockService.listProjectThreads).toHaveBeenCalledWith(projectId);
    });

    it('should set starting state while processing', async () => {
      mockService.startChatFromProject.mockResolvedValue(createMockStartResponse());
      mockService.listProjectThreads.mockResolvedValue({ threads: [], total: 0 });

      const promise = useProjectChatStore.getState().startChatFromProject(projectId, request);

      expect(useProjectChatStore.getState().startingChat[projectId]).toBe(true);

      await promise;

      expect(useProjectChatStore.getState().startingChat[projectId]).toBe(false);
    });

    it('should return null on failure', async () => {
      mockService.startChatFromProject.mockRejectedValue(new Error('Failed'));

      let result: StartChatFromProjectResponse | null = null;
      await act(async () => {
        result = await useProjectChatStore.getState().startChatFromProject(projectId, request);
      });

      expect(result).toBeNull();
      expect(useProjectChatStore.getState().errors[projectId]).toBe('Failed');
    });

    it('should guard against invalid project ID', async () => {
      const result = await useProjectChatStore.getState().startChatFromProject('', request);

      expect(result).toBeNull();
      expect(mockService.startChatFromProject).not.toHaveBeenCalled();
    });
  });

  // ==========================================================================
  // linkThreadToProject
  // ==========================================================================

  describe('linkThreadToProject', () => {
    const projectId = 'proj-123';
    const request = { thread_id: 'thread-456' };

    it('should add thread optimistically on success', async () => {
      const mockThread = createMockProjectThread();
      mockService.linkThreadToProject.mockResolvedValue(mockThread);

      await act(async () => {
        await useProjectChatStore.getState().linkThreadToProject(projectId, request);
      });

      const state = useProjectChatStore.getState();
      expect(state.linkedThreads[projectId]).toContainEqual(mockThread);
    });

    it('should initialize array if project has no threads', async () => {
      const mockThread = createMockProjectThread();
      mockService.linkThreadToProject.mockResolvedValue(mockThread);

      await act(async () => {
        await useProjectChatStore.getState().linkThreadToProject(projectId, request);
      });

      expect(useProjectChatStore.getState().linkedThreads[projectId]).toBeDefined();
    });

    it('should prepend new thread to existing list', async () => {
      // Set up existing threads
      act(() => {
        useProjectChatStore.setState({
          linkedThreads: {
            [projectId]: [createMockProjectThread({ id: 'existing' })],
          },
        });
      });

      const newThread = createMockProjectThread({ id: 'new' });
      mockService.linkThreadToProject.mockResolvedValue(newThread);

      await act(async () => {
        await useProjectChatStore.getState().linkThreadToProject(projectId, request);
      });

      const threads = useProjectChatStore.getState().linkedThreads[projectId];
      expect(threads[0].id).toBe('new'); // New thread should be first
    });

    it('should return null on failure', async () => {
      mockService.linkThreadToProject.mockRejectedValue(new Error('Duplicate'));

      let result: ProjectThread | null = null;
      await act(async () => {
        result = await useProjectChatStore.getState().linkThreadToProject(projectId, request);
      });

      expect(result).toBeNull();
    });
  });

  // ==========================================================================
  // unlinkThreadFromProject
  // ==========================================================================

  describe('unlinkThreadFromProject', () => {
    const projectId = 'proj-123';
    const threadId = 'thread-456';

    it('should remove thread optimistically on success', async () => {
      // Set up existing threads
      act(() => {
        useProjectChatStore.setState({
          linkedThreads: {
            [projectId]: [
              createMockProjectThread({ thread_id: threadId }),
              createMockProjectThread({ thread_id: 'other' }),
            ],
          },
        });
      });

      mockService.unlinkThreadFromProject.mockResolvedValue();

      await act(async () => {
        await useProjectChatStore.getState().unlinkThreadFromProject(projectId, threadId);
      });

      const threads = useProjectChatStore.getState().linkedThreads[projectId];
      expect(threads).toHaveLength(1);
      expect(threads.find(t => t.thread_id === threadId)).toBeUndefined();
    });

    it('should handle non-existent thread gracefully', async () => {
      act(() => {
        useProjectChatStore.setState({
          linkedThreads: {
            [projectId]: [],
          },
        });
      });

      mockService.unlinkThreadFromProject.mockResolvedValue();

      await act(async () => {
        await useProjectChatStore.getState().unlinkThreadFromProject(projectId, 'nonexistent');
      });

      // Should not throw
      expect(useProjectChatStore.getState().linkedThreads[projectId]).toEqual([]);
    });

    it('should set error on failure', async () => {
      mockService.unlinkThreadFromProject.mockRejectedValue(new Error('Not found'));

      await act(async () => {
        await useProjectChatStore.getState().unlinkThreadFromProject(projectId, threadId);
      });

      expect(useProjectChatStore.getState().errors[projectId]).toBe('Not found');
    });

    it('should resolve true on success so callers do not infer the outcome from the shared error slot', async () => {
      act(() => {
        useProjectChatStore.setState({
          linkedThreads: {
            [projectId]: [createMockProjectThread({ thread_id: threadId })],
          },
        });
      });
      mockService.unlinkThreadFromProject.mockResolvedValue();

      let result: boolean | undefined;
      await act(async () => {
        result = await useProjectChatStore
          .getState()
          .unlinkThreadFromProject(projectId, threadId);
      });

      expect(result).toBe(true);
    });

    it('should resolve false when the service rejects', async () => {
      mockService.unlinkThreadFromProject.mockRejectedValue(new Error('Not found'));

      let result: boolean | undefined;
      await act(async () => {
        result = await useProjectChatStore
          .getState()
          .unlinkThreadFromProject(projectId, threadId);
      });

      expect(result).toBe(false);
      expect(useProjectChatStore.getState().errors[projectId]).toBe('Not found');
    });
  });

  // ==========================================================================
  // saveThreadToNote
  // ==========================================================================

  describe('saveThreadToNote', () => {
    const projectId = 'proj-123';
    const request = {
      thread_id: 'thread-456',
      note_title: 'Summary',
      include_citations: true,
    };

    it('should return note on success', async () => {
      const mockNote = { id: 'note-123', title: 'Summary', content: '# Content' };
      mockService.saveThreadToNote.mockResolvedValue(mockNote as any);

      let result: any = null;
      await act(async () => {
        result = await useProjectChatStore.getState().saveThreadToNote(projectId, request);
      });

      expect(result).toEqual(mockNote);
    });

    it('should return null on failure', async () => {
      mockService.saveThreadToNote.mockRejectedValue(new Error('Failed'));

      let result: any = null;
      await act(async () => {
        result = await useProjectChatStore.getState().saveThreadToNote(projectId, request);
      });

      expect(result).toBeNull();
    });

    it('should set saving state while processing', async () => {
      mockService.saveThreadToNote.mockResolvedValue({ id: 'note' } as any);

      const promise = useProjectChatStore.getState().saveThreadToNote(projectId, request);

      expect(useProjectChatStore.getState().savingToNote[projectId]).toBe(true);

      await promise;

      expect(useProjectChatStore.getState().savingToNote[projectId]).toBe(false);
    });
  });

  // ==========================================================================
  // clearError
  // ==========================================================================

  describe('clearError', () => {
    it('should clear error for specific project', () => {
      act(() => {
        useProjectChatStore.setState({
          errors: {
            'proj-1': 'Error 1',
            'proj-2': 'Error 2',
          },
        });
      });

      act(() => {
        useProjectChatStore.getState().clearError('proj-1');
      });

      const state = useProjectChatStore.getState();
      expect(state.errors['proj-1']).toBeNull();
      expect(state.errors['proj-2']).toBe('Error 2');
    });
  });

  // ==========================================================================
  // reset
  // ==========================================================================

  describe('reset', () => {
    it('should reset store to initial state', () => {
      act(() => {
        useProjectChatStore.setState({
          linkedThreads: { 'proj-1': [createMockProjectThread()] },
          errors: { 'proj-1': 'Error' },
          loadingThreads: { 'proj-1': true },
        });
      });

      act(() => {
        useProjectChatStore.getState().reset();
      });

      const state = useProjectChatStore.getState();
      expect(state.linkedThreads).toEqual({});
      expect(state.errors).toEqual({});
      expect(state.loadingThreads).toEqual({});
    });
  });

  // ==========================================================================
  // Selectors
  // ==========================================================================

  describe('selectors', () => {
    describe('selectProjectThreads', () => {
      it('should return threads for project', () => {
        const threads = [createMockProjectThread()];
        act(() => {
          useProjectChatStore.setState({
            linkedThreads: { 'proj-1': threads },
          });
        });

        const state = useProjectChatStore.getState();
        const result = selectProjectThreads('proj-1')(state);

        expect(result).toEqual(threads);
      });

      it('should return empty array for unknown project', () => {
        const state = useProjectChatStore.getState();
        const result = selectProjectThreads('unknown')(state);

        expect(result).toEqual([]);
      });
    });

    describe('selectProjectLoading', () => {
      it('should return true if any loading state is true', () => {
        act(() => {
          useProjectChatStore.setState({
            loadingThreads: { 'proj-1': false },
            startingChat: { 'proj-1': true },
            linkingThread: { 'proj-1': false },
          });
        });

        const state = useProjectChatStore.getState();
        const result = selectProjectLoading('proj-1')(state);

        expect(result).toBe(true);
      });

      it('should return false if all loading states are false', () => {
        act(() => {
          useProjectChatStore.setState({
            loadingThreads: { 'proj-1': false },
            startingChat: { 'proj-1': false },
            linkingThread: { 'proj-1': false },
          });
        });

        const state = useProjectChatStore.getState();
        const result = selectProjectLoading('proj-1')(state);

        expect(result).toBe(false);
      });
    });

    describe('selectProjectError', () => {
      it('should return error for project', () => {
        act(() => {
          useProjectChatStore.setState({
            errors: { 'proj-1': 'Test error' },
          });
        });

        const state = useProjectChatStore.getState();
        const result = selectProjectError('proj-1')(state);

        expect(result).toBe('Test error');
      });

      it('should return null for project without error', () => {
        const state = useProjectChatStore.getState();
        const result = selectProjectError('proj-1')(state);

        expect(result).toBeNull();
      });
    });
  });

  // ==========================================================================
  // Per-Project Isolation
  // ==========================================================================

  describe('per-project isolation', () => {
    it('should maintain separate state for different projects', async () => {
      const threads1 = [createMockProjectThread({ id: 'pt-1' })];
      const threads2 = [createMockProjectThread({ id: 'pt-2' })];

      mockService.listProjectThreads
        .mockResolvedValueOnce({ threads: threads1, total: 1 })
        .mockResolvedValueOnce({ threads: threads2, total: 1 });

      await act(async () => {
        await useProjectChatStore.getState().fetchProjectThreads('proj-1');
        await useProjectChatStore.getState().fetchProjectThreads('proj-2');
      });

      const state = useProjectChatStore.getState();
      expect(state.linkedThreads['proj-1']).toEqual(threads1);
      expect(state.linkedThreads['proj-2']).toEqual(threads2);
    });

    it('should maintain separate error state for different projects', async () => {
      mockService.listProjectThreads
        .mockResolvedValueOnce({ threads: [], total: 0 })
        .mockRejectedValueOnce(new Error('Project 2 error'));

      await act(async () => {
        await useProjectChatStore.getState().fetchProjectThreads('proj-1');
        await useProjectChatStore.getState().fetchProjectThreads('proj-2');
      });

      const state = useProjectChatStore.getState();
      expect(state.errors['proj-1']).toBeNull();
      expect(state.errors['proj-2']).toBe('Project 2 error');
    });
  });
});
