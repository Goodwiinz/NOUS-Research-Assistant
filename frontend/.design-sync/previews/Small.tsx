import { Muted, P, Small } from '@nous/design-system';

export const FormLabel = () => (
  <div className="max-w-[420px] space-y-2">
    <Small>Citation style</Small>
    <P className="text-sm">APA 7th edition</P>
  </div>
);

export const Metadata = () => (
  <div className="max-w-[480px] space-y-2">
    <P>Dense passage retrieval outperforms BM25 on 4 of 5 open-domain QA benchmarks.</P>
    <div className="flex gap-4">
      <Small>Karpukhin et al., 2020</Small>
      <Muted>EMNLP</Muted>
    </div>
  </div>
);
