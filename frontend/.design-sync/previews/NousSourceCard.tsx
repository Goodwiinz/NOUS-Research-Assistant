import { NousSourceCard } from '@nous/design-system';

export const HighRelevance = () => (
  <div className="max-w-[560px]">
    <NousSourceCard
      citationIndex={1}
      title="FlashAttention: Fast and Memory-Efficient Exact Attention with IO-Awareness"
      excerpt="We propose FlashAttention, an IO-aware exact attention algorithm that uses tiling to reduce the number of memory reads/writes between GPU high bandwidth memory and on-chip SRAM."
      source="Dao et al. 2022 · flash_attn.pdf"
      page={3}
      score={0.94}
    />
  </div>
);

export const ModerateRelevance = () => (
  <div className="max-w-[560px]">
    <NousSourceCard
      citationIndex={4}
      title="Longformer: The Long-Document Transformer"
      excerpt="Longformer's attention mechanism is a drop-in replacement for standard self-attention and combines a local windowed attention with a task-motivated global attention."
      source="Beltagy et al. 2020 · longformer.pdf"
      score={0.62}
    />
  </div>
);

export const SourceList = () => (
  <div className="grid max-w-[560px] gap-3">
    <NousSourceCard
      citationIndex={1}
      title="Attention Is All You Need"
      excerpt="The Transformer is the first transduction model relying entirely on self-attention to compute representations of its input and output without using sequence-aligned RNNs or convolution."
      source="Vaswani et al. 2017 · transformer.pdf"
      page={2}
      score={0.91}
    />
    <NousSourceCard
      citationIndex={2}
      title="Reformer: The Efficient Transformer"
      excerpt="We replace dot-product attention by one that uses locality-sensitive hashing, changing its complexity from O(L²) to O(L log L)."
      source="Kitaev et al. 2020 · reformer.pdf"
      page={5}
      score={0.77}
    />
  </div>
);
