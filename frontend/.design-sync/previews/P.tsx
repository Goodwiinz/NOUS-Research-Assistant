import {
  Blockquote,
  H1,
  H2,
  H3,
  InlineCode,
  Lead,
  List,
  Muted,
  P,
} from '@nous/design-system';

// H1-H4 ship in the bundle but get no card of their own (the converter drops
// ALL-CAPS names as constants), so the canonical heading-scale story lives here.
// The full typography scale composed as a literature-review article — the
// canonical way these primitives are used together.
export const ResearchArticle = () => (
  <article style={{ maxWidth: 640 }}>
    <Muted>Literature review · Sparse attention survey · 14 sources</Muted>
    <H1 className="mt-2">Sparse attention at long context</H1>
    <Lead className="mt-4">
      How block-sparse, sliding-window and routing attention trade recall for
      throughput beyond 128k tokens.
    </Lead>
    <H2 className="mt-10">Background</H2>
    <P>
      Dense self-attention scales quadratically with sequence length. Every
      method in this review replaces the full <InlineCode>QKᵀ</InlineCode>{' '}
      product with a structured subset of token pairs.
    </P>
    <Blockquote>
      Longformer reaches dense-attention quality on WikiHop while attending to
      under 2% of token pairs.
    </Blockquote>
    <H3 className="mt-8">Methods compared</H3>
    <List>
      <li>Sliding-window attention (Longformer, Mistral)</li>
      <li>Block-sparse attention (BigBird)</li>
      <li>Learned routing (Routing Transformer)</li>
    </List>
  </article>
);


export const BodyParagraphs = () => (
  <section className="max-w-[640px]">
    <H3>Summary</H3>
    <P>
      Retrieval-augmented generation grounds a language model in documents
      fetched at query time, so answers can cite their sources and stay current
      without retraining.
    </P>
    <P>
      The surveyed systems differ mainly in how they chunk documents, how many
      passages they retrieve, and whether the retriever is trained jointly with
      the generator.
    </P>
  </section>
);

export const SingleParagraph = () => (
  <P className="max-w-[560px]">
    NOUS found 12 passages supporting this claim and 2 that contradict it. Open
    the evidence panel to review each source before citing it in your draft.
  </P>
);
