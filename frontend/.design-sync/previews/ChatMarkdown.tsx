import type { ReactNode } from 'react';
import { DarkSurface } from './_DarkSurface';
import { ChatMarkdown, CitationLink } from '@nous/design-system';

// Citations: ChatMarkdown has no citation syntax of its own. The app's
// citation path (CitationRenderer) rewrites `[Doc N]` markers into links whose
// href is `#nous-citation-N`, then overrides the `a` element to render a
// CitationLink. This story writes those links directly and passes the same
// `components` override so the real footnote markers appear in the prose.
const SOURCES = [
  {
    documentId: 'doc-flash-attn',
    title: 'FlashAttention: Fast and Memory-Efficient Exact Attention with IO-Awareness',
    score: 0.94,
    source: 'flash_attn.pdf',
    pageNumber: 3,
  },
  {
    documentId: 'doc-longformer',
    title: 'Longformer: The Long-Document Transformer',
    score: 0.81,
    source: 'longformer.pdf',
    pageNumber: 5,
  },
  {
    externalReferenceId: 'arXiv:2001.04451',
    title: 'Reformer: The Efficient Transformer',
    score: 0.67,
    source: 'arXiv',
  },
];

const citationComponents = {
  a({ href, children }: { href?: string; children?: ReactNode }) {
    const match = href?.match(/^#nous-citation-(\d+)$/);
    if (!match) {
      return (
        <a href={href} target="_blank" rel="noopener noreferrer">
          {children}
        </a>
      );
    }
    const n = Number(match[1]);
    return <CitationLink citationNumber={n} citation={SOURCES[n - 1]} />;
  },
};

const RESEARCH_ANSWER = `## Why exact attention can still be fast

Standard self-attention is quadratic in sequence length: for $n$ tokens and head dimension $d$, computing $\\mathrm{softmax}(QK^\\top / \\sqrt{d})V$ costs $O(n^2 d)$ time and materializes an $n \\times n$ score matrix [1](#nous-citation-1).

### Three families of fixes

1. **IO-aware tiling** keeps the computation exact but never writes the full score matrix to HBM [1](#nous-citation-1).
2. **Sparse patterns** such as sliding windows plus global tokens reduce cost to $O(n \\cdot w)$ [2](#nous-citation-2).
3. **Hashing** buckets similar queries and keys so each token attends to $O(\\log n)$ neighbours [3](#nous-citation-3).

In PyTorch 2.x the fused kernel is selected automatically through \`scaled_dot_product_attention\`:

\`\`\`python
import torch.nn.functional as F

out = F.scaled_dot_product_attention(q, k, v, is_causal=True)
\`\`\`

> For sequences under 2k tokens the tiled exact kernel usually beats the approximate methods on both speed and quality.
`;

// `nous-chat-body` is the assistant column wrapper in ChatBubble: it sets the
// Source Serif reading face; `nous-prose` styles headings, lists and quotes.
export const ResearchAnswer = () => (
  <div className="nous-chat-body">
    <div className="nous-prose w-full max-w-[72ch]">
      <ChatMarkdown content={RESEARCH_ANSWER} components={citationComponents} />
    </div>
  </div>
);

const COMPARISON_TABLE = `### Long-context attention methods compared

| Method | Complexity | Exact | Paper |
| --- | --- | --- | --- |
| FlashAttention | $O(n^2)$, IO-aware | Yes | Dao et al. 2022 |
| Longformer | $O(n \\cdot w)$ | No | Beltagy et al. 2020 |
| Reformer | $O(n \\log n)$ | No | Kitaev et al. 2020 |

- [x] Read all three abstracts
- [ ] Reproduce the FlashAttention benchmark on an A100
- [ ] Draft the related-work paragraph`;

export const TableAndTasks = () => (
  <div className="nous-chat-body">
    <div className="nous-prose w-full max-w-[72ch]">
      <ChatMarkdown content={COMPARISON_TABLE} />
    </div>
  </div>
);

const NOTE = `**Reading note: Longformer §3.** The dilated sliding window widens the receptive field without extra cost; see the [project repository](https://github.com/allenai/longformer) for the CUDA kernel. Cross-check the window size \`w = 512\` against our ablation.`;

export const ProjectNote = () => (
  <div className="nous-prose p-4 font-nous-body text-sm leading-relaxed text-(--nous-fg-1)">
    <ChatMarkdown content={NOTE} />
  </div>
);

// The same answer on the dark theme, the live app's default.
export const Dark = () => (
  <DarkSurface>
    <ResearchAnswer />
  </DarkSurface>
);
