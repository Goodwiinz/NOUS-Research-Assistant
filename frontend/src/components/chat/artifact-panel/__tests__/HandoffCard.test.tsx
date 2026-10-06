import { QueryClientProvider } from '@tanstack/react-query';
import { act, fireEvent, screen, waitFor } from '@testing-library/react';
import { renderHook } from '@testing-library/react';
import type { ReactElement, ReactNode } from 'react';
import { beforeEach, describe, expect, it, vi } from 'vitest';

vi.mock('@/services/api-client', () => ({ api: { get: vi.fn() } }));
vi.mock('@/services/artifactService', () => ({
  artifactService: { listThreadArtifacts: vi.fn().mockResolvedValue([]) },
}));

import {
  HANDOFF_POLL_MS,
  threadHandoffKey,
  useThreadHandoff,
} from '@/hooks/chat/useThreadHandoff';
import { api } from '@/services/api-client';
import type { ThreadArtifact } from '@/services/artifactService';
import { useArtifactPanelStore } from '@/store/artifactPanelStore';
import { createTestQueryClient, render } from '@/test/test-utils';
import type { ApiHandoff } from '@/types/api/integration-handoff-contract';

import {
  HandoffCard,
  HandoffEntryButton,
  ThreadHandoffCard,
} from '../HandoffCard';

const get = vi.mocked(api.get);
const THREAD = '22222222-2222-4222-8222-222222222222';

const VERSION = '55555555-5555-4555-8555-555555555555';
const OTHER = '66666666-6666-4666-8666-666666666666';

const handoff: ApiHandoff = {
  id: '11111111-1111-4111-8111-111111111111',
  thread_id: '22222222-2222-4222-8222-222222222222',
  project_id: '33333333-3333-4333-8333-333333333333',
  version: 3,
  handoff_id: '44444444-4444-4444-8444-444444444444',
  goal: 'Finish the literature review',
  decisions: ['Use PRISMA 2020'],
  remaining: ['Screen 40 abstracts'],
  results: [
    { artifact_version_id: VERSION, summary: 'Search log' },
    { artifact_version_id: OTHER, summary: 'Older draft' },
  ],
  harness_name: 'codex',
  harness_session_id: null,
  created_at: '2026-10-05T12:00:00Z',
};

const artifact: ThreadArtifact = {
  version: {
    artifactId: 'a1',
    versionId: VERSION,
    parentVersionId: null,
    title: 'search-log.md',
    mimeType: 'text/markdown',
    byteSize: 10,
    sha256: 'x',
    createdAt: '2026-10-05T11:00:00Z',
    producer: 'harness',
    sourceIds: [],
  },
  reference: {
    artifactId: 'a1',
    versionId: VERSION,
    runId: null,
    threadId: handoff.thread_id,
    messageId: null,
  },
};

describe('HandoffCard', () => {
  it('renders goal, decisions, results, remaining and the version footer', () => {
    const onOpenVersion = vi.fn();
    render(
      <HandoffCard
        handoff={handoff}
        artifacts={[artifact]}
        onOpenVersion={onOpenVersion}
      />
    );
    expect(
      screen.getByRole('region', { name: 'Session handoff' })
    ).toBeInTheDocument();
    expect(
      screen.getByText('Finish the literature review')
    ).toBeInTheDocument();
    expect(screen.getByText('Use PRISMA 2020')).toBeInTheDocument();
    expect(screen.getByText('Screen 40 abstracts')).toBeInTheDocument();
    expect(screen.getByText(/Search log/)).toBeInTheDocument();
    expect(screen.getByText(/^v3 · codex ·/)).toBeInTheDocument();
    // A result whose version is in this chat opens it; an unknown one is text.
    fireEvent.click(screen.getByRole('button', { name: 'search-log.md' }));
    expect(onOpenVersion).toHaveBeenCalledWith(artifact);
    expect(screen.getByText(OTHER.slice(0, 8))).toBeInTheDocument();
  });

  it('renders nothing without a handoff', () => {
    const { container } = render(<HandoffCard handoff={null} />);
    expect(container).toBeEmptyDOMElement();
  });
});

describe('ThreadHandoffCard', () => {
  beforeEach(() => {
    get.mockReset();
    act(() => useArtifactPanelStore.getState().reset());
  });

  it('shows a stable failure notice with retry, never the raw error', async () => {
    get.mockRejectedValueOnce({ error: { status_code: 500, message: 'boom' } });
    render(<ThreadHandoffCard threadId={THREAD} />);
    const alert = await screen.findByRole('alert');
    expect(alert).toHaveTextContent("Couldn't load the chat handoff.");
    expect(alert).not.toHaveTextContent('boom');
    get.mockResolvedValueOnce(handoff);
    fireEvent.click(screen.getByRole('button', { name: 'Retry' }));
    expect(
      await screen.findByText('Finish the literature review')
    ).toBeInTheDocument();
  });

  it('renders nothing when the chat has no handoff (404)', async () => {
    get.mockRejectedValueOnce({ error: { status_code: 404 } });
    const { container } = render(<ThreadHandoffCard threadId={THREAD} />);
    await waitFor(() => expect(get).toHaveBeenCalled());
    await waitFor(() => expect(container).toBeEmptyDOMElement());
  });
});

describe('handoff-only view', () => {
  it('says there is no handoff instead of rendering a blank panel', async () => {
    get.mockReset();
    get.mockRejectedValueOnce({ error: { status_code: 404 } });
    render(<ThreadHandoffCard threadId={THREAD} showEmpty />);
    expect(
      await screen.findByText('No handoff in this chat yet.')
    ).toBeInTheDocument();
  });
});

describe('useThreadHandoff', () => {
  it('polls while mounted and refetches on focus', async () => {
    get.mockResolvedValue(handoff);
    const client = createTestQueryClient();
    const wrapper = ({ children }: { children: ReactNode }): ReactElement => (
      <QueryClientProvider client={client}>{children}</QueryClientProvider>
    );
    const { result } = renderHook(() => useThreadHandoff(THREAD), { wrapper });
    await waitFor(() => expect(result.current.data).toEqual(handoff));
    const query = client
      .getQueryCache()
      .find({ queryKey: threadHandoffKey(THREAD) });
    const options = query?.observers[0]?.options;
    expect(HANDOFF_POLL_MS).toBe(30_000);
    expect(options?.refetchInterval).toBe(HANDOFF_POLL_MS);
    expect(options?.refetchOnWindowFocus).toBe(true);
  });
});

describe('HandoffEntryButton', () => {
  beforeEach(() => {
    get.mockReset();
    act(() => useArtifactPanelStore.getState().reset());
  });

  it('appears with a handoff and opens the handoff-only panel view', async () => {
    get.mockResolvedValue(handoff);
    render(<HandoffEntryButton threadId={THREAD} />);
    fireEvent.click(await screen.findByRole('button', { name: /Handoff v3/ }));
    const state = useArtifactPanelStore.getState();
    expect(state.isOpen).toBe(true);
    expect(state.artifact).toEqual({ kind: 'handoff', title: 'Handoff' });
    // The panel now shows the handoff; the header button steps aside.
    await waitFor(() =>
      expect(screen.queryByRole('button', { name: /Handoff v3/ })).toBeNull()
    );
  });

  it('is absent when the chat has no handoff', async () => {
    get.mockRejectedValue({ error: { status_code: 404 } });
    const { container } = render(<HandoffEntryButton threadId={THREAD} />);
    await waitFor(() => expect(get).toHaveBeenCalled());
    expect(container).toBeEmptyDOMElement();
  });
});
