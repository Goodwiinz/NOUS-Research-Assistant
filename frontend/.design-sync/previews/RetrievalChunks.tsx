import { RetrievalChunks } from '@nous/design-system';

const chunks = [
  {
    id: 'flash-attn-3',
    source: 'FlashAttention: Fast and Memory-Efficient Exact Attention',
    locator: 'p. 3',
    score: 0.92,
    text: 'We propose FlashAttention, an IO-aware exact attention algorithm that uses tiling to reduce the number of memory reads/writes between GPU HBM and on-chip SRAM.',
  },
  {
    id: 'longformer-2',
    source: 'Longformer: The Long-Document Transformer',
    locator: 'p. 2',
    score: 0.84,
    text: "Longformer's attention mechanism combines a local windowed attention with a task-motivated global attention, scaling linearly with sequence length.",
  },
  {
    id: 'reformer-5',
    source: 'Reformer: The Efficient Transformer',
    locator: 'p. 5',
    score: 0.67,
    text: 'We replace dot-product attention by one that uses locality-sensitive hashing, changing its complexity from O(L²) to O(L log L).',
  },
];

export const Retrieved = () => (
  <RetrievalChunks
    query="Relevant project sources"
    chunks={chunks}
    visibleCount={chunks.length}
    searching={false}
    className="max-w-[560px]"
  />
);

export const Searching = () => (
  <RetrievalChunks
    query="Relevant project sources"
    chunks={chunks}
    visibleCount={1}
    searching
    className="max-w-[560px]"
  />
);

export const SinglePassage = () => (
  <RetrievalChunks
    query="memory complexity of linear attention"
    chunks={chunks.slice(2)}
    visibleCount={1}
    searching={false}
    className="max-w-[560px]"
  />
);
