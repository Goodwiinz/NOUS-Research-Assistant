import { act, screen } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';

vi.mock('@/services/artifactService', () => ({
  artifactService: { listThreadArtifacts: vi.fn() },
}));

import {
  artifactService,
  type ThreadArtifact,
} from '@/services/artifactService';
import { useArtifactPanelStore } from '@/store/artifactPanelStore';
import { useChatStore } from '@/store/chat-store';
import { render } from '@/test/test-utils';
import { GeneratedArtifactCards } from '../GeneratedArtifactCards';

const item = (
  versionId: string,
  messageId: string | null,
  title = 'report.md'
): ThreadArtifact => ({
  version: {
    artifactId: 'a-' + versionId,
    versionId,
    parentVersionId: null,
    title,
    mimeType: 'text/markdown',
    byteSize: 7,
    sha256: 'x',
    createdAt: '2026-09-30T00:00:00Z',
    producer: 'harness',
    sourceIds: [],
  },
  reference: {
    artifactId: 'a-' + versionId,
    versionId,
    runId: 'r1',
    threadId: 't1',
    messageId,
  },
});

function seedThread(
  messages: Array<{ id: string; role: 'user' | 'assistant' }>
): void {
  act(() =>
    useChatStore.setState((s) => ({
      ...s,
      currentThreadId: 't1',
      messages: { ...s.messages, t1: messages as never },
    }))
  );
}

describe('GeneratedArtifactCards', () => {
  it('shows cards for this message, thread outputs only under the latest assistant message, and opens the panel', async () => {
    act(() =>
      useArtifactPanelStore.setState({
        artifact: null,
        isOpen: false,
        pinned: false,
      })
    );
    seedThread([
      { id: 'm1', role: 'assistant' },
      { id: 'u2', role: 'user' },
      { id: 'm3', role: 'assistant' },
    ]);
    vi.mocked(artifactService.listThreadArtifacts).mockResolvedValue([
      item('v1', 'm1', 'first.md'),
      item('v2', null, 'late.png'),
      item('v3', 'm3', 'third.md'),
      item('v4', 'm-not-loaded', 'older.csv'),
    ]);
    const { user, rerender } = render(
      <GeneratedArtifactCards messageId="m1" />
    );
    expect(
      await screen.findByRole('button', { name: /Open first\.md/ })
    ).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /Open late\.png/ })).toBeNull();
    expect(screen.queryByRole('button', { name: /Open third\.md/ })).toBeNull();

    rerender(<GeneratedArtifactCards messageId="m3" />);
    expect(
      await screen.findByRole('button', { name: /Open third\.md/ })
    ).toBeInTheDocument();
    expect(
      screen.getByRole('button', { name: /Open late\.png/ })
    ).toHaveTextContent('thread output');

    await user.click(screen.getByRole('button', { name: /Open third\.md/ }));
    expect(useArtifactPanelStore.getState().isOpen).toBe(true);
    expect(useArtifactPanelStore.getState().artifact).toEqual({
      kind: 'generated',
      artifactId: 'a-v3',
      versionId: 'v3',
      title: 'third.md',
    });
    expect(artifactService.listThreadArtifacts).toHaveBeenCalledWith('t1');
  });

  it('renders nothing for an unpersisted message or an empty thread', async () => {
    seedThread([{ id: 'm1', role: 'assistant' }]);
    vi.mocked(artifactService.listThreadArtifacts).mockResolvedValue([]);
    const { container } = render(
      <GeneratedArtifactCards messageId={undefined} />
    );
    expect(container).toBeEmptyDOMElement();
  });
});
