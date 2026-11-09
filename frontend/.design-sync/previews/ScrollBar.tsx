// ScrollBar only renders inside its ScrollArea parent - the full parent
// composition is the true render (composed inline; see learnings D.md on why
// sibling-preview imports are avoided).
import { ScrollArea, ScrollBar } from '@nous/design-system';


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
