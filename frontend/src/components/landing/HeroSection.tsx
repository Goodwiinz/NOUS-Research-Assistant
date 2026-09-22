'use client';

import { ArrowRight } from 'lucide-react';
import Link from 'next/link';

const UI = { fontFamily: 'var(--nous-font-ui)' } as const;
const SERIF = { fontFamily: 'var(--nous-font-body)' } as const;

// Marginalia: the two numbered notes back the <sup> marks in the lead
// paragraph, the third is an unnumbered aside.
const MARGIN_NOTES = [
  {
    mark: '1',
    text: 'Inline citations resolve to the retrieved passage, page and document, not to a search result.',
  },
  {
    mark: '2',
    text: 'Retrieval is hybrid: vector, BM25 and knowledge-graph traversal. 12ms median across millions of documents.',
  },
  {
    mark: '',
    text: 'Inference can run in the browser with WebLLM. Your corpus stays on your infrastructure.',
  },
] as const;

const FOOTNOTE_MARK =
  'font-semibold text-[0.6em] text-(--nous-sol-safe) ml-0.5';

interface HeroSectionProps {
  isAuthenticated: boolean;
}

export function HeroSection({ isAuthenticated }: HeroSectionProps) {
  return (
    <>
      <nav
        className="h-14 lg:h-16 border-b border-(--nous-enceladus)"
        style={UI}
      >
        <div className="max-w-7xl mx-auto px-6 h-full flex items-center justify-between">
          <Link href="/" className="flex flex-col leading-none">
            <span className="text-[15px] lg:text-base font-bold tracking-[0.18em] text-(--nous-erebus)">
              NOUS
            </span>
            <span className="hidden sm:block text-[11px] tracking-[0.12em] text-(--nous-titan) mt-1">
              Multimodal Intelligence
            </span>
          </Link>

          <div className="flex items-center gap-4 sm:gap-7 text-sm">
            <a
              href="#what"
              className="hidden sm:inline-flex text-(--nous-titan) hover:text-(--nous-erebus) transition-colors rounded-sm focus-visible:outline-hidden focus-visible:ring-2 focus-visible:ring-(--nous-sol)/50"
            >
              What it does
            </a>
            {isAuthenticated ? (
              <Link
                href="/dashboard"
                className="inline-flex items-center h-9 px-4 rounded-(--nous-radius-md) font-semibold bg-(--nous-erebus) text-(--nous-selene) transition-opacity hover:opacity-90 focus-visible:outline-hidden focus-visible:ring-2 focus-visible:ring-(--nous-sol)/50 focus-visible:ring-offset-2 focus-visible:ring-offset-(--nous-selene)"
              >
                Open dashboard
              </Link>
            ) : (
              <>
                <Link
                  href="/login"
                  className="text-(--nous-titan) hover:text-(--nous-erebus) transition-colors rounded-sm focus-visible:outline-hidden focus-visible:ring-2 focus-visible:ring-(--nous-sol)/50"
                >
                  Sign in
                </Link>
                <Link
                  href="/register"
                  className="inline-flex items-center h-9 px-4 rounded-(--nous-radius-md) font-semibold bg-(--nous-erebus) text-(--nous-selene) transition-opacity hover:opacity-90 focus-visible:outline-hidden focus-visible:ring-2 focus-visible:ring-(--nous-sol)/50 focus-visible:ring-offset-2 focus-visible:ring-offset-(--nous-selene)"
                >
                  Get started
                </Link>
              </>
            )}
          </div>
        </div>
      </nav>

      <section className="px-6 py-16 lg:pt-32 lg:pb-28">
        {/* CSS entrance, not framer-motion: an `initial={{ opacity: 0 }}` on
            the SSR markup leaves the hero invisible until hydration, which on
            a slow load is seconds of blank page and a late LCP. */}
        <div className="max-w-7xl mx-auto grid lg:grid-cols-[8fr_3fr] lg:gap-24 animate-in fade-in-0 slide-in-from-bottom-4 duration-500 motion-reduce:animate-none">
          <div>
            <div
              aria-hidden="true"
              className="w-10 lg:w-14 h-[3px] bg-(--nous-sol) mb-7 lg:mb-10"
            />

            <h1
              className="text-5xl lg:text-[96px] font-normal tracking-[-0.02em] leading-none text-(--nous-erebus) text-pretty mb-6 lg:mb-9"
              style={SERIF}
            >
              Knowledge that{' '}
              <em className="italic text-(--nous-sol-safe)">answers back.</em>
            </h1>

            <p
              className="text-lg lg:text-[22px] leading-[1.6] text-(--nous-titan) max-w-[52ch] mb-8 lg:mb-11"
              style={SERIF}
            >
              A research instrument for people who read for a living. NOUS
              retrieves across your papers and documents, reasons over a
              knowledge graph, and footnotes every claim
              <sup className={FOOTNOTE_MARK} style={UI}>
                1
              </sup>{' '}
              so you can check its work
              <sup className={FOOTNOTE_MARK} style={UI}>
                2
              </sup>{' '}
              before you build on it.
            </p>

            <div
              className="flex flex-col items-start gap-4 sm:flex-row sm:items-center sm:gap-6 text-sm"
              style={UI}
            >
              <Link
                href={isAuthenticated ? '/dashboard' : '/register'}
                className="group inline-flex w-full sm:w-auto items-center justify-center gap-2 h-12 px-7 rounded-(--nous-radius-md) font-semibold bg-(--nous-erebus) text-(--nous-selene) transition-opacity hover:opacity-90 focus-visible:outline-hidden focus-visible:ring-2 focus-visible:ring-(--nous-sol)/50 focus-visible:ring-offset-2 focus-visible:ring-offset-(--nous-selene)"
              >
                {isAuthenticated ? 'Open dashboard' : 'Get started'}
                <ArrowRight
                  className="w-4 h-4 transition-transform group-hover:translate-x-0.5"
                  aria-hidden="true"
                />
              </Link>
              <a
                href="#what"
                className="inline-flex items-center min-h-11 sm:min-h-0 font-medium text-(--nous-sol-safe) border-b border-(--nous-sol) pb-0.5 hover:text-(--nous-erebus) transition-colors focus-visible:outline-hidden focus-visible:ring-2 focus-visible:ring-(--nous-sol)/50"
              >
                Read how it works
              </a>
            </div>
          </div>

          {/* Marginalia. A right-hand column at lg, a ruled block under the
              CTAs on narrow screens — same DOM order either way. */}
          <aside
            className="mt-10 pt-6 border-t border-(--nous-enceladus) flex flex-col gap-4 text-[13px] leading-[1.55] text-(--nous-titan) lg:mt-0 lg:pt-11 lg:gap-7 lg:border-t-0 lg:border-l lg:border-(--nous-enceladus) lg:pl-7"
            style={UI}
          >
            {MARGIN_NOTES.map((note) => (
              <div
                key={note.text}
                className={`grid grid-cols-[16px_1fr] gap-2 ${
                  note.mark ? '' : 'text-(--nous-fg-3)'
                }`}
              >
                <span className="font-semibold text-(--nous-sol-safe)">
                  {note.mark}
                </span>
                <span>{note.text}</span>
              </div>
            ))}
          </aside>
        </div>
      </section>
    </>
  );
}
