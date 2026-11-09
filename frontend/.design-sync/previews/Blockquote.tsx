import { Blockquote, Muted, P } from '@nous/design-system';

export const PaperQuote = () => (
  <div className="max-w-[560px]">
    <P>The original transformer paper framed the architecture plainly:</P>
    <Blockquote>
      We propose a new simple network architecture, the Transformer, based
      solely on attention mechanisms.
    </Blockquote>
    <Muted className="mt-3">Vaswani et al., 2017 · Abstract</Muted>
  </div>
);

export const HighlightedClaim = () => (
  <Blockquote className="max-w-[560px]">
    Retrieval reduces hallucinated citations by 41% compared with the same
    model answering from parametric memory alone.
  </Blockquote>
);
