import { NousChatComposer } from '@nous/design-system';

export const Empty = () => (
  <div className="max-w-[640px]">
    <NousChatComposer />
  </div>
);

export const WithQuestion = () => (
  <div className="max-w-[640px]">
    <NousChatComposer initialValue="How does FlashAttention-2 reduce non-matmul FLOPs compared to v1?" />
  </div>
);

export const ProjectChips = () => (
  <div className="max-w-[640px]">
    <NousChatComposer
      placeholder="Ask about Efficient Transformers..."
      chips={['Sources', 'Agents', 'Graph', 'Drafts']}
    />
  </div>
);
