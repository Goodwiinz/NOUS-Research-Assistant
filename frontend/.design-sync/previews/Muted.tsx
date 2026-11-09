import { Large, Muted, P } from '@nous/design-system';

export const HelperText = () => (
  <div className="max-w-[480px] space-y-1">
    <Large>Upload documents</Large>
    <Muted>PDF, DOCX or Markdown up to 50 MB. Files are indexed in about a minute.</Muted>
  </div>
);

export const Caption = () => (
  <div className="max-w-[560px]">
    <P>
      Graph-based retrieval improved multi-hop answer accuracy by 9 points over
      flat vector search.
    </P>
    <Muted className="mt-2">Source: GraphRAG, Edge et al. 2024 · page 7</Muted>
  </div>
);
