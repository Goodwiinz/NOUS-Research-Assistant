'use client';

import { ArrowRight } from 'lucide-react';
import Link from 'next/link';

import { GoesOutComesInUnderline } from '@/components/ui/underline-animation';

const UI = { fontFamily: 'var(--nous-font-ui)' } as const;
const SERIF = { fontFamily: 'var(--nous-font-body)' } as const;

const FOOTER_COLS = [
  {
    head: 'Product',
    links: [
      { label: 'Features', href: '/#what' },
      { label: 'Search', href: '/search' },
      { label: 'Dashboard', href: '/dashboard' },
    ],
  },
  {
    head: 'Account',
    links: [
      { label: 'Sign in', href: '/login' },
      { label: 'Get started', href: '/register' },
    ],
  },
] as const;

interface FooterSectionProps {
  isAuthenticated: boolean;
}

export function FooterSection({ isAuthenticated }: FooterSectionProps) {
  return (
    <>
      {/* Closing CTA */}
      <section className="px-6 py-18 lg:py-28">
        <div className="max-w-7xl mx-auto lg:text-center">
          <h2
            className="text-[32px] lg:text-[44px] font-normal tracking-[-0.02em] leading-[1.1] text-(--nous-erebus) text-pretty mb-4 lg:mb-5"
            style={SERIF}
          >
            Point it at your corpus and start asking.
          </h2>
          <p
            className="text-lg lg:text-xl leading-[1.6] text-(--nous-titan) mb-7 lg:mb-10"
            style={SERIF}
          >
            Every answer traceable to its source.
          </p>

          <Link
            href={isAuthenticated ? '/dashboard' : '/register'}
            className="group inline-flex w-full lg:w-auto items-center justify-center gap-2 h-12 px-7 rounded-(--nous-radius-md) text-sm font-semibold bg-(--nous-sol) text-(--nous-erebus) transition-colors hover:bg-(--nous-helios) focus-visible:outline-hidden focus-visible:ring-2 focus-visible:ring-(--nous-sol)/50 focus-visible:ring-offset-2 focus-visible:ring-offset-(--nous-selene)"
            style={UI}
          >
            {isAuthenticated ? 'Open dashboard' : 'Get started'}
            <ArrowRight
              className="w-4 h-4 transition-transform group-hover:translate-x-0.5"
              aria-hidden="true"
            />
          </Link>
        </div>
      </section>

      {/* Footer */}
      <footer
        className="px-6 pt-12 pb-8 lg:pt-20 lg:pb-10 border-t border-(--nous-enceladus)"
        style={UI}
      >
        <div className="max-w-7xl mx-auto">
          <div className="grid grid-cols-2 lg:grid-cols-4 gap-10 mb-10 lg:mb-16">
            <div className="col-span-2 lg:col-span-1">
              <span className="text-[15px] lg:text-base font-bold tracking-[0.18em] text-(--nous-erebus)">
                NOUS
              </span>
              <p
                className="mt-3 lg:mt-4 text-sm leading-[1.625] text-(--nous-titan) max-w-[320px]"
                style={SERIF}
              >
                A multimodal intelligence platform for research teams who need
                answers they can cite.
              </p>
            </div>

            {FOOTER_COLS.map((col) => (
              <nav key={col.head} aria-label={col.head}>
                <h3 className="text-xs font-semibold uppercase tracking-[0.12em] text-(--nous-titan) mb-3 lg:mb-4">
                  {col.head}
                </h3>
                <ul className="flex flex-col gap-2.5">
                  {col.links.map((l) => (
                    <li key={l.label}>
                      <Link
                        href={l.href}
                        className="inline-block text-sm text-(--nous-titan) hover:text-(--nous-erebus) transition-colors rounded-sm focus-visible:outline-hidden focus-visible:ring-2 focus-visible:ring-(--nous-sol)/50"
                      >
                        <GoesOutComesInUnderline
                          label={l.label}
                          direction="left"
                        />
                      </Link>
                    </li>
                  ))}
                </ul>
              </nav>
            ))}
          </div>

          <div className="pt-6 lg:pt-8 border-t border-(--nous-enceladus) text-[13px] lg:text-sm text-(--nous-titan)">
            &copy; 2026 NOUS. All rights reserved.
          </div>
        </div>
      </footer>
    </>
  );
}
