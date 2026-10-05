import { fireEvent, screen } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';

import type { ThreadArtifact } from '@/services/artifactService';
import { render } from '@/test/test-utils';
import type { ApiHandoff } from '@/types/api/integration-handoff-contract';

import { HandoffCard } from '../HandoffCard';

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
