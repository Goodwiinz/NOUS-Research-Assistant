import { H1, Lead, P } from '@nous/design-system';

export const UnderTitle = () => (
  <div className="max-w-[640px]">
    <H1>Knowledge graphs for scientific discovery</H1>
    <Lead className="mt-4">
      A survey of entity extraction, link prediction and graph-grounded
      question answering across 22 papers.
    </Lead>
  </div>
);

export const IntroParagraph = () => (
  <div className="max-w-[640px]">
    <Lead>
      Start a research thread by asking a question. NOUS searches your project
      documents and arXiv, then answers with citations.
    </Lead>
    <P>Every answer links back to the exact passage it was drawn from.</P>
  </div>
);
