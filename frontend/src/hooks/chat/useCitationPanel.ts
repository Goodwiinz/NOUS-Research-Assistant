import { useCallback } from 'react';

import { useArtifactPanelStore } from '@/store/artifactPanelStore';
import { Citation } from '@/utils/citationParser';

export interface UseCitationPanelReturn {
  handleCitationClick: (
    citations: Citation[],
    clickedCitation: Citation,
    traceId?: string
  ) => void;
}

/**
 * Transcript citation clicks focus the sources in the split-view artifact
 * panel (kind: 'citations'). The panel itself lives in the chat layout and
 * reads the artifact store directly — the old 'open-citation-panel' window
 * event bridge and page-local panel state are gone.
 */
export function useCitationPanel(): UseCitationPanelReturn {
  const openArtifact = useArtifactPanelStore((s) => s.openArtifact);

  const handleCitationClick = useCallback(
    (citations: Citation[], clickedCitation: Citation, traceId?: string) => {
      openArtifact({
        kind: 'citations',
        citations,
        activeCitationId:
          clickedCitation.documentId || clickedCitation.externalReferenceId,
        ...(clickedCitation.pageNumber
          ? { activeCitationPage: clickedCitation.pageNumber }
          : {}),
        traceId,
      });
    },
    [openArtifact]
  );

  return { handleCitationClick };
}
