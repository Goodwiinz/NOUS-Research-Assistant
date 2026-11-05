import { InlineCode, P } from '@nous/design-system';

export const InParagraph = () => (
  <P className="max-w-[560px]">
    Set <InlineCode>top_k</InlineCode> to 8 and enable{' '}
    <InlineCode>rerank: true</InlineCode> to retrieve more candidate passages
    before the cross-encoder reorders them.
  </P>
);

export const Identifiers = () => (
  <P className="max-w-[560px]">
    The paper <InlineCode>arXiv:2005.11401</InlineCode> introduced RAG; NOUS
    stores it as document <InlineCode>doc_71bd17fe</InlineCode>.
  </P>
);
