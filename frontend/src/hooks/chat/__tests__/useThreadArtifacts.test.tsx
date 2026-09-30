import { act, renderHook, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

vi.mock('@/services/artifactService', () => ({
  artifactService: { listThreadArtifacts: vi.fn() },
}));

import { artifactService } from '@/services/artifactService';
import { useChatStore } from '@/store/chat-store';
import { AllProviders } from '@/test/test-utils';
import { useThreadArtifacts } from '../useThreadArtifacts';

describe('useThreadArtifacts polling', () => {
  beforeEach(() => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    vi.mocked(artifactService.listThreadArtifacts).mockResolvedValue([]);
  });
  afterEach(() => {
    vi.useRealTimers();
    act(() =>
      useChatStore.setState({ isStreaming: false, streamingThreadId: null })
    );
  });

  it('keeps polling after a stream ends so late publications still land', async () => {
    act(() =>
      useChatStore.setState({ isStreaming: true, streamingThreadId: 't1' })
    );
    renderHook(() => useThreadArtifacts('t1'), { wrapper: AllProviders });
    await waitFor(() =>
      expect(artifactService.listThreadArtifacts).toHaveBeenCalledTimes(1)
    );
    act(() =>
      useChatStore.setState({ isStreaming: false, streamingThreadId: null })
    );
    await act(async () => {
      await vi.advanceTimersByTimeAsync(16_000);
    });
    // Empty result + no stream: the old predicate stopped here.
    expect(artifactService.listThreadArtifacts).toHaveBeenCalledTimes(2);
  });

  it('does not poll a thread that never streamed and has no pending files', async () => {
    renderHook(() => useThreadArtifacts('t2'), { wrapper: AllProviders });
    await waitFor(() =>
      expect(artifactService.listThreadArtifacts).toHaveBeenCalledTimes(1)
    );
    await act(async () => {
      await vi.advanceTimersByTimeAsync(16_000);
    });
    expect(artifactService.listThreadArtifacts).toHaveBeenCalledTimes(1);
  });
});
