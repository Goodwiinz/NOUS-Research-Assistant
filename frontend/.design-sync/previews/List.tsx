import { H4, List } from '@nous/design-system';

export const BulletList = () => (
  <section className="max-w-[560px]">
    <H4>Key contributions</H4>
    <List>
      <li>A retriever trained end-to-end with the generator</li>
      <li>State-of-the-art results on Natural Questions and TriviaQA</li>
      <li>Answers that can be traced to specific Wikipedia passages</li>
    </List>
  </section>
);

export const ReadingList = () => (
  <List className="max-w-[560px]">
    <li>Lewis et al., Retrieval-Augmented Generation (2020)</li>
    <li>Izacard &amp; Grave, Fusion-in-Decoder (2021)</li>
    <li>Asai et al., Self-RAG (2023)</li>
    <li>Edge et al., GraphRAG (2024)</li>
  </List>
);
