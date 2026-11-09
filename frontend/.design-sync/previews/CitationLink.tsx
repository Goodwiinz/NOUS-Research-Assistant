import { CitationLink } from '@nous/design-system';

// CitationLink is the inline footnote marker inside assistant prose. Its
// hover card (title, relevance badge, preview, "View Document") opens on
// pointer hover, so the static card shows the markers in their prose context.

const flashAttention = {
  documentId: 'doc-flash-attn',
  chunkId: 'chunk-0012',
  title: 'FlashAttention: Fast and Memory-Efficient Exact Attention with IO-Awareness',
  score: 0.94,
  source: 'flash_attn.pdf',
  pageNumber: 3,
  content:
    'Abstract: We propose FlashAttention, an IO-aware exact attention algorithm that uses tiling to reduce the number of memory reads/writes between GPU high bandwidth memory and on-chip SRAM.',
};

const longformer = {
  documentId: 'doc-longformer',
  title: 'Longformer: The Long-Document Transformer',
  score: 0.72,
  source: 'longformer.pdf',
};

const reformer = {
  externalReferenceId: 'arXiv:2001.04451',
  title: 'Reformer: The Efficient Transformer',
  score: 0.55,
  source: 'arXiv',
};

export const InProse = () => (
  <p className="nous-prose font-nous-body text-[15px] leading-relaxed text-(--nous-fg-1)" style={{ maxWidth: '64ch' }}>
    IO-aware tiling keeps attention exact while avoiding the full score matrix
    in HBM
    <CitationLink citationNumber={1} citation={flashAttention} />, whereas
    sliding-window patterns trade exactness for linear cost
    <CitationLink citationNumber={2} citation={longformer} />
    <CitationLink citationNumber={3} citation={reformer} />.
  </p>
);

export const ActiveCitation = () => (
  <p className="nous-prose font-nous-body text-[15px] leading-relaxed text-(--nous-fg-1)" style={{ maxWidth: '64ch' }}>
    The selected footnote is underlined while its source is open in the
    document panel
    <CitationLink citationNumber={1} citation={flashAttention} isActive />,
    the others keep the accent colour
    <CitationLink citationNumber={2} citation={longformer} />.
  </p>
);

export const UnresolvedCitation = () => (
  <p className="nous-prose font-nous-body text-[15px] leading-relaxed text-(--nous-fg-1)" style={{ maxWidth: '64ch' }}>
    A marker whose source was not returned with the answer renders muted and
    disabled
    <CitationLink citationNumber={4} />, next to a resolved one
    <CitationLink citationNumber={1} citation={flashAttention} />.
  </p>
);
