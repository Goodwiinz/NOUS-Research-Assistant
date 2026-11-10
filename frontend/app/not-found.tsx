import type { Metadata } from 'next';
import type { ReactElement } from 'react';
import Link from 'next/link';

// A server component so it can carry its own title (the root layout's title
// has no template, so the brand suffix is written out). Next keeps the 404
// status and adds the noindex robots tag itself.
export const metadata: Metadata = {
  title: 'Page not found | NOUS',
};

const UI = { fontFamily: 'var(--nous-font-ui)' } as const;
const SERIF = { fontFamily: 'var(--nous-font-body)' } as const;

// House focus ring (HeroSection), on theme tokens: this page can be reached
// signed in, in either app theme.
const RING =
  'focus-visible:outline-hidden focus-visible:ring-2 focus-visible:ring-(--nous-sol)/50';

export default function NotFound(): ReactElement {
  return (
    <div className="min-h-screen flex flex-col bg-(--nous-bg-1) text-(--nous-fg-1)">
      <header
        className="h-14 lg:h-16 px-6 border-b border-(--nous-border-1)"
        style={UI}
      >
        <div className="max-w-7xl mx-auto h-full flex items-center">
          <Link
            href="/"
            className={`flex flex-col leading-none rounded-sm ${RING}`}
          >
            <span className="text-[15px] lg:text-base font-bold tracking-[0.18em] text-(--nous-fg-1)">
              NOUS
            </span>
            <span className="hidden sm:block text-[11px] tracking-[0.12em] text-(--nous-fg-2) mt-1">
              Multimodal Intelligence
            </span>
          </Link>
        </div>
      </header>

      <main className="flex-1 px-6 py-16 lg:py-28">
        <div className="max-w-7xl mx-auto">
          <div
            aria-hidden="true"
            className="w-10 lg:w-14 h-[3px] bg-(--nous-sol) mb-7 lg:mb-10"
          />
          <h1
            className="text-5xl lg:text-[72px] font-normal tracking-[-0.02em] leading-none text-(--nous-fg-1) text-pretty mb-6"
            style={SERIF}
          >
            Page not found
          </h1>
          <p
            className="text-lg lg:text-[22px] leading-[1.6] text-(--nous-fg-2) max-w-[52ch] mb-8 lg:mb-11"
            style={SERIF}
          >
            The page you were looking for does not exist, or it has moved.
          </p>

          <div
            className="flex flex-col items-start gap-4 sm:flex-row sm:items-center sm:gap-6 text-sm"
            style={UI}
          >
            <Link
              href="/"
              className={`inline-flex w-full sm:w-auto items-center justify-center h-12 px-7 rounded-(--nous-radius-md) font-semibold bg-(--nous-fg-1) text-(--nous-bg-1) transition-opacity hover:opacity-90 ${RING} focus-visible:ring-offset-2 focus-visible:ring-offset-(--nous-bg-1)`}
            >
              Back to home
            </Link>
            <Link
              href="/dashboard"
              className={`inline-flex items-center min-h-11 sm:min-h-0 font-medium text-(--nous-fg-2) underline decoration-(--nous-border-2) underline-offset-4 hover:text-(--nous-fg-1) hover:decoration-(--nous-sol) transition-colors rounded-sm ${RING}`}
            >
              Sign in or open your dashboard
            </Link>
          </div>
        </div>
      </main>
    </div>
  );
}
