import React from 'react';

/**
 * Manuscript scene panel shared by /login and /register.
 *
 * One element, two shapes: on <lg it is the 320px band above the form (grid
 * row 1), on lg+ it is the left column of the two-column split. The washes
 * differ between the two, so there are two overlay divs toggled by breakpoint
 * rather than a duplicated panel with duplicated copy.
 */
export interface AuthScenePanelProps {
  /** Public path of the scene illustration. */
  src: string;
  /** Serif display headline. */
  headline: string;
  /** Lede paragraph — carries the <sup>1</sup> marker. */
  body: React.ReactNode;
  /** Numbered footnotes, rendered under a hairline rule. */
  notes: string[];
  /**
   * Desktop wash strength. 'heavy' (login) keeps the form side of the split
   * quiet; 'light' (register) lets more of the artwork through.
   */
  wash: 'heavy' | 'light';
  /** Tint for the lede + footnotes. Login runs ivory, register parchment. */
  proseTone: 'ivory' | 'parchment';
}

const DESKTOP_WASH: Record<AuthScenePanelProps['wash'], string> = {
  heavy:
    'linear-gradient(to top, rgba(var(--nous-nyx-rgb),0.97) 0%, rgba(var(--nous-nyx-rgb),0.88) 42%, rgba(var(--nous-nyx-rgb),0.45) 72%, rgba(var(--nous-nyx-rgb),0.2) 100%)',
  light:
    'linear-gradient(to top, rgba(var(--nous-nyx-rgb),0.92) 0%, rgba(var(--nous-nyx-rgb),0.55) 38%, rgba(var(--nous-nyx-rgb),0) 70%)',
};

const MOBILE_WASH =
  'linear-gradient(to top, rgba(var(--nous-nyx-rgb),0.97) 0%, rgba(var(--nous-nyx-rgb),0.85) 45%, rgba(var(--nous-nyx-rgb),0.35) 80%, rgba(var(--nous-nyx-rgb),0.2) 100%)';

export default function AuthScenePanel({
  src,
  headline,
  body,
  notes,
  wash,
  proseTone,
}: AuthScenePanelProps): React.JSX.Element {
  const prose =
    proseTone === 'ivory' ? 'text-(--nous-ivory)' : 'text-(--nous-parchment)';

  return (
    <div className="relative h-[320px] overflow-hidden bg-(--nous-nyx) lg:h-auto">
      {/* eslint-disable-next-line @next/next/no-img-element -- decorative SVG,
          object-cover across a full panel; next/image adds no value here. */}
      <img
        src={src}
        alt=""
        aria-hidden="true"
        className="absolute inset-0 block h-full w-full object-cover"
      />
      <div
        aria-hidden="true"
        className="absolute inset-0 lg:hidden"
        style={{ background: MOBILE_WASH }}
      />
      <div
        aria-hidden="true"
        className="absolute inset-0 hidden lg:block"
        style={{ background: DESKTOP_WASH[wash] }}
      />

      <div className="absolute left-5 top-5 flex flex-col leading-none lg:left-16 lg:top-6">
        <span className="text-[15px] font-bold tracking-[0.18em] text-(--nous-ivory) lg:text-[16px]">
          NOUS
        </span>
        <span className="mt-1 text-[11px] tracking-[0.12em] text-(--nous-parchment)">
          Multimodal Intelligence
        </span>
      </div>

      <div className="absolute bottom-7 left-5 right-5 flex flex-col gap-4 lg:bottom-16 lg:left-16 lg:right-16 lg:gap-6">
        <div
          aria-hidden="true"
          className="h-[3px] w-10 bg-(--nous-sol) lg:w-14"
        />
        <h1
          className="m-0 text-[32px] font-normal leading-[1.1] tracking-[-0.02em] text-(--nous-ivory) lg:max-w-[14ch] lg:text-[56px] lg:leading-[1.05]"
          style={{ fontFamily: 'var(--nous-font-body)', textWrap: 'pretty' }}
        >
          {headline}
        </h1>
        <p
          className={`m-0 hidden max-w-[40ch] text-[19px] leading-[1.6] lg:block ${prose}`}
          style={{ fontFamily: 'var(--nous-font-body)' }}
        >
          {body}
        </p>
        <div
          className={`hidden max-w-[420px] flex-col gap-3 border-t border-(--nous-parchment)/25 pt-5 text-[13px] leading-[1.55] lg:flex ${prose}`}
        >
          {notes.map((note, i) => (
            <div key={note} className="grid grid-cols-[16px_1fr] gap-2">
              <span
                aria-hidden="true"
                className="font-semibold text-(--nous-helios)"
              >
                {i + 1}
              </span>
              <span>{note}</span>
            </div>
          ))}
        </div>
      </div>
    </div>
  );
}
