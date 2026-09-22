const UI = { fontFamily: 'var(--nous-font-ui)' } as const;
const SERIF = { fontFamily: 'var(--nous-font-body)' } as const;

const STEPS = [
  {
    numeral: 'I',
    title: 'Reads everything you give it',
    desc: 'Papers, notes, datasets and arXiv pulls are ingested, chunked and indexed. Entities and relationships are extracted into a knowledge graph as they land.',
  },
  {
    numeral: 'II',
    title: 'Finds the passage and its neighbours',
    desc: 'Hybrid retrieval returns the sentence that answers you together with the claims it depends on and the papers that dispute it.',
  },
  {
    numeral: 'III',
    title: 'Answers like a careful colleague',
    desc: 'Plain sentences, sources attached, uncertainty stated. Anything destructive waits for your confirmation.',
  },
] as const;

export function CapabilitiesSection() {
  return (
    <section id="what" className="px-6 py-16 lg:py-26 bg-(--nous-aurum)">
      <div className="max-w-7xl mx-auto grid lg:grid-cols-[4fr_8fr] lg:gap-24">
        {/* Artwork leads on narrow screens, follows the heading at lg. */}
        <div className="flex flex-col-reverse gap-7 mb-8 lg:mb-0 lg:flex-col lg:gap-10">
          {/* Plain <img>: next/image refuses to optimize SVG without
              dangerouslyAllowSVG, so it would add cost and no benefit. */}
          {/* eslint-disable-next-line @next/next/no-img-element */}
          <img
            src="/landing/manuscript-artwork.svg"
            alt="An open codex under a lens; the page text resolves into a small constellation of connected points"
            width={400}
            height={300}
            className="block w-[260px] lg:w-[360px] h-auto"
          />
          <h2
            className="text-3xl lg:text-[40px] font-normal tracking-[-0.02em] leading-[1.15] text-(--nous-erebus) text-pretty"
            style={SERIF}
          >
            Three things it does, in the order you need them.
          </h2>
        </div>

        <div className="flex flex-col">
          {STEPS.map((step, i) => (
            <div
              key={step.numeral}
              className={`grid grid-cols-[32px_1fr] gap-3 py-6 border-t border-(--nous-border-2) lg:grid-cols-[64px_1fr] lg:gap-6 lg:py-8 ${
                i === STEPS.length - 1 ? 'border-b' : ''
              }`}
            >
              <span
                className="text-[13px] font-semibold text-(--nous-sol-safe) pt-1 lg:pt-1.5"
                style={UI}
              >
                {step.numeral}
              </span>
              <div>
                <h3
                  className="text-[19px] lg:text-[22px] font-semibold tracking-[-0.02em] text-(--nous-erebus) mb-2 lg:mb-2.5"
                  style={UI}
                >
                  {step.title}
                </h3>
                <p
                  className="text-base lg:text-[18px] leading-[1.65] text-(--nous-titan)"
                  style={SERIF}
                >
                  {step.desc}
                </p>
              </div>
            </div>
          ))}
        </div>
      </div>
    </section>
  );
}
