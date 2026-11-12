/**
 * Chat Store Streaming Tests
 *
 * Tests for the streaming state and actions in chat-store.ts:
 * - Streaming state properties exist with correct defaults
 * - stopStreaming action exists as a function
 * - stopStreaming resets streaming state
 */

import { beforeEach, describe, expect, it, vi } from 'vitest';
import { act } from '@testing-library/react';
import { enableMapSet } from 'immer';

// Enable Immer MapSet plugin before store import
enableMapSet();

// Mock workspace service (required by store module)
vi.mock('@/services/workspaceService', () => ({
  workspaceService: {
    listWorkspaces: vi.fn(),
    createWorkspace: vi.fn(),
    updateWorkspace: vi.fn(),
    deleteWorkspace: vi.fn(),
    getOrCreateDefaultWorkspace: vi.fn(),
    listConversations: vi.fn(),
    createConversation: vi.fn(),
    updateConversation: vi.fn(),
    deleteConversation: vi.fn(),
    listThreads: vi.fn(),
    createThread: vi.fn(),
    updateThread: vi.fn(),
    deleteThread: vi.fn(),
    bulkResolveThreads: vi.fn(),
    bulkArchiveThreads: vi.fn(),
    bulkSummarizeThreads: vi.fn(),
    bulkDeleteThreads: vi.fn(),
    listMessages: vi.fn(),
    createMessage: vi.fn(),
    updateMessage: vi.fn(),
    deleteMessage: vi.fn(),
  },
}));

// Must import after mocks are set up
import { useChatStore } from '@/store/chat-store';

describe('Chat Store Streaming State and Actions', () => {
  beforeEach(() => {
    // Reset store to initial state before each test
    act(() => {
      useChatStore.getState().reset();
    });
  });

  describe('initial streaming state', () => {
    it('should have isStreaming set to false', () => {
      expect(useChatStore.getState().isStreaming).toBe(false);
    });

    it('should have streamingContent set to empty string', () => {
      expect(useChatStore.getState().streamingContent).toBe('');
    });

    it('should have streamingMessageId set to null', () => {
      expect(useChatStore.getState().streamingMessageId).toBeNull();
    });

    it('should have streamingCitations set to empty array', () => {
      expect(useChatStore.getState().streamingCitations).toEqual([]);
    });

    it('should not have abortController in state (moved to module level)', () => {
      expect('abortController' in useChatStore.getState()).toBe(false);
    });
  });

  describe('stopStreaming action', () => {
    it('should exist as a function', () => {
      expect(typeof useChatStore.getState().stopStreaming).toBe('function');
    });

    it('should reset all streaming state', () => {
      // Manually set streaming state to non-default values
      act(() => {
        useChatStore.setState({
          isStreaming: true,
          streamingContent: 'partial response...',
          streamingMessageId: 'msg-123',
          streamingCitations: [{ source: 'test' }],
          abortController: new AbortController(),
        });
      });

      // Verify state was set
      expect(useChatStore.getState().isStreaming).toBe(true);
      expect(useChatStore.getState().streamingContent).toBe(
        'partial response...'
      );
      expect(useChatStore.getState().streamingMessageId).toBe('msg-123');
      expect(useChatStore.getState().streamingCitations).toHaveLength(1);
      // Call stopStreaming
      act(() => {
        useChatStore.getState().stopStreaming();
      });

      // Verify streaming state is reset
      expect(useChatStore.getState().isStreaming).toBe(false);
      expect(useChatStore.getState().streamingContent).toBe('');
      expect(useChatStore.getState().streamingMessageId).toBeNull();
      expect(useChatStore.getState().streamingCitations).toEqual([]);
    });

    it('should handle stopStreaming when not streaming', () => {
      act(() => {
        useChatStore.setState({
          isStreaming: true,
          streamingContent: 'some content',
        });
      });

      // Should not throw
      act(() => {
        useChatStore.getState().stopStreaming();
      });

      expect(useChatStore.getState().isStreaming).toBe(false);
      expect(useChatStore.getState().streamingContent).toBe('');
    });
  });
});
