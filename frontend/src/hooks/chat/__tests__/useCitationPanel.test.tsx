import { act, renderHook } from '@testing-library/react';
import { beforeEach, describe, expect, it } from 'vitest';

import { useCitationPanel } from '@/hooks/chat/useCitationPanel';
import { useArtifactPanelStore } from '@/store/artifactPanelStore';
import type { Citation } from '@/utils/citationParser';

describe('useCitationPanel', () => {
  beforeEach(() => {
    act(() => {
      useArtifactPanelStore.setState({
        artifact: null,
        isOpen: false,
        pinned: false,
      });
    });
  });

  it('focuses the clicked citations in the artifact panel with the trace id', () => {
    const { result } = renderHook(() => useCitationPanel());
    const citations: Citation[] = [
      { title: 'Paper A', documentId: 'doc-1' } as Citation,
    ];

    act(() => {
      result.current.handleCitationClick(citations, citations[0], 'trace-1');
    });

    const s = useArtifactPanelStore.getState();
    expect(s.isOpen).toBe(true);
    expect(s.artifact).toEqual({
      kind: 'citations',
      citations,
      activeCitationId: 'doc-1',
      traceId: 'trace-1',
    });
  });

  it('uses the external reference id when the citation has no document id', () => {
    const { result } = renderHook(() => useCitationPanel());
    const citation = {
      title: 'arXiv paper',
      externalReferenceId: '2310.08419',
    } as Citation;

    act(() => {
      result.current.handleCitationClick([citation], citation);
    });

    const artifact = useArtifactPanelStore.getState().artifact;
    expect(artifact?.kind).toBe('citations');
    expect(
      artifact?.kind === 'citations' ? artifact.activeCitationId : undefined
    ).toBe('2310.08419');
  });

  it('retains the clicked physical page while opening the sources panel', () => {
    const { result } = renderHook(() => useCitationPanel());
    const citation = {
      title: 'PDF',
      documentId: 'doc-1',
      pageNumber: 3,
      score: 1,
    };
    act(() => result.current.handleCitationClick([citation], citation));
    expect(useArtifactPanelStore.getState().artifact).toMatchObject({
      activeCitationId: 'doc-1',
      activeCitationPage: 3,
    });
  });
});
