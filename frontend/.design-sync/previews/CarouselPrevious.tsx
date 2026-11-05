// CarouselPrevious only renders inside its Carousel parent - the full parent
// composition is the true render (composed inline; see learnings D.md on why
// sibling-preview imports are avoided).
import {
  Badge,
  Carousel,
  CarouselContent,
  CarouselItem,
  CarouselNext,
  CarouselPrevious,
} from '@nous/design-system';

const papers = [
  { title: 'Attention Is All You Need', meta: 'Vaswani et al. · 2017', tag: 'Transformers' },
  { title: 'Retrieval-Augmented Generation', meta: 'Lewis et al. · 2020', tag: 'Retrieval' },
  { title: 'Longformer', meta: 'Beltagy et al. · 2020', tag: 'Long context' },
  { title: 'Self-RAG', meta: 'Asai et al. · 2023', tag: 'Retrieval' },
  { title: 'GraphRAG', meta: 'Edge et al. · 2024', tag: 'Knowledge graphs' },
];

const PaperSlide = ({ title, meta, tag }: (typeof papers)[number]) => (
  <div className="flex h-36 flex-col justify-between rounded-lg border border-border bg-card p-4">
    <Badge variant="secondary" className="self-start">{tag}</Badge>
    <div>
      <div className="text-sm font-semibold leading-snug">{title}</div>
      <div className="mt-1 font-mono text-xs text-muted-foreground">{meta}</div>
    </div>
  </div>
);

export const RecommendedPapers = () => (
  <div style={{ width: 640, paddingInline: 56 }} className="py-2">
    <Carousel opts={{ align: 'start' }}>
      <CarouselContent>
        {papers.map((p) => (
          <CarouselItem key={p.title} style={{ flexBasis: '33.333%' }}>
            <PaperSlide {...p} />
          </CarouselItem>
        ))}
      </CarouselContent>
      <CarouselPrevious />
      <CarouselNext />
    </Carousel>
  </div>
);

