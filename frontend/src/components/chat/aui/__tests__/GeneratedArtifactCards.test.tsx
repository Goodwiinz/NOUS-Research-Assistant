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

type Row = { id: string; role: 'user' | 'assistant' };
const row = (id: string, role: Row['role']): Row => ({ id, role });

function seedThread(messages: Row[]): void {
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
    const rows = [
      row('m1', 'assistant'),
      row('u2', 'user'),
      row('m3', 'assistant'),
    ];
    seedThread(rows);
    vi.mocked(artifactService.listThreadArtifacts).mockResolvedValue([
      item('v1', 'm1', 'first.md'),
      item('v2', null, 'late.png'),
      item('v3', 'm3', 'third.md'),
      item('v4', 'm-not-loaded', 'older.csv'),
    ]);
    const { user, rerender } = render(
      <GeneratedArtifactCards message={rows[0]} />
    );
    expect(
      await screen.findByRole('button', { name: /Open first\.md/ })
    ).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /Open late\.png/ })).toBeNull();
    expect(screen.queryByRole('button', { name: /Open third\.md/ })).toBeNull();

    rerender(<GeneratedArtifactCards message={rows[2]} isLatestAssistant />);
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
    const rows = [row('m1', 'assistant')];
    seedThread(rows);
    vi.mocked(artifactService.listThreadArtifacts).mockResolvedValue([]);
    const { container } = render(
      <GeneratedArtifactCards message={undefined} />
    );
    expect(container).toBeEmptyDOMElement();
  });

  it('attaches thread outputs to a failed (id-less) latest assistant row', async () => {
    const rows = [row('m1', 'assistant'), row('u2', 'user')];
    seedThread(rows);
    vi.mocked(artifactService.listThreadArtifacts).mockResolvedValue([
      item('v2', null, 'late.png'),
    ]);
    const { rerender } = render(
      <GeneratedArtifactCards message={{ id: undefined }} isLatestAssistant />
    );
    expect(
      await screen.findByRole('button', { name: /Open late\.png/ })
    ).toBeInTheDocument();
    rerender(<GeneratedArtifactCards message={rows[0]} />);
    expect(screen.queryByRole('button', { name: /Open late\.png/ })).toBeNull();
  });

  it('surfaces a list failure with retry on the latest assistant row only', async () => {
    const rows = [row('m1', 'assistant'), row('m3', 'assistant')];
    seedThread(rows);
    vi.mocked(artifactService.listThreadArtifacts)
      .mockRejectedValueOnce(new Error('boom'))
      .mockResolvedValueOnce([item('v3', 'm3', 'third.md')]);
    const { user, rerender } = render(
      <GeneratedArtifactCards message={rows[1]} isLatestAssistant />
    );
    const alert = await screen.findByRole('alert');
    expect(alert).toHaveTextContent(/Could not load generated files/);
    rerender(<GeneratedArtifactCards message={rows[0]} />);
    expect(screen.queryByRole('alert')).toBeNull();
    rerender(<GeneratedArtifactCards message={rows[1]} isLatestAssistant />);
    await user.click(screen.getByRole('button', { name: /Retry/ }));
    expect(
      await screen.findByRole('button', { name: /Open third\.md/ })
    ).toBeInTheDocument();
  });
});
