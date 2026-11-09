import { ScrollArea, ScrollBar, Separator } from '@nous/design-system';

const papers = [
  'Attention Is All You Need',
  'BERT: Pre-training of Deep Bidirectional Transformers',
  'Retrieval-Augmented Generation for Knowledge-Intensive NLP',
  'Dense Passage Retrieval for Open-Domain QA',
  'Longformer: The Long-Document Transformer',
  'Big Bird: Transformers for Longer Sequences',
  'Fusion-in-Decoder',
  'Self-RAG: Learning to Retrieve, Generate, and Critique',
  'GraphRAG: From Local to Global',
  'RAPTOR: Recursive Abstractive Processing',
];

// type="always" keeps the scrollbar visible; the default ("hover") hides it
// until the pointer is over the area.
export const SourceList = () => (
  <ScrollArea type="always" className="h-64 w-[360px] rounded-md border border-border bg-card">
    <div className="p-4">
      <h4 className="mb-3 text-sm font-medium leading-none">Project sources</h4>
      {papers.map((title) => (
        <div key={title}>
          <div className="py-2 text-sm">{title}</div>
          <Separator />
        </div>
      ))}
    </div>
  </ScrollArea>
);

export const HorizontalChips = () => (
  <ScrollArea type="always" className="w-[420px] whitespace-nowrap rounded-md border border-border bg-card">
    <div className="flex w-max gap-3 p-4">
      {['Transformers', 'Retrieval', 'Knowledge graphs', 'Long context', 'Evaluation', 'Reranking', 'Agents'].map(
        (topic) => (
          <div
            key={topic}
            className="rounded-md border border-border bg-muted px-4 py-6 text-sm font-medium"
          >
            {topic}
          </div>
        )
      )}
    </div>
    <ScrollBar orientation="horizontal" />
  </ScrollArea>
);
