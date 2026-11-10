'use client';

import { CapabilitiesSection } from '@/components/landing/CapabilitiesSection';
import {
  ClosingSection,
  FooterSection,
} from '@/components/landing/FooterSection';
import { HeroSection, LandingNav } from '@/components/landing/HeroSection';
import { useAuth } from '@/hooks/useAuth';
import type { ReactElement } from 'react';

export default function HomePage(): ReactElement {
  const { isAuthenticated } = useAuth();

  return (
    <div
      className="nous-light min-h-screen flex flex-col bg-(--nous-selene) text-(--nous-erebus) selection:bg-(--nous-sol) selection:text-(--nous-erebus)"
      suppressHydrationWarning
    >
      <a
        href="#main-content"
        className="sr-only focus:not-sr-only focus:fixed focus:left-4 focus:top-3 focus:z-50 focus:rounded-(--nous-radius-md) focus:bg-(--nous-erebus) focus:px-4 focus:py-2 focus:text-sm focus:font-semibold focus:text-(--nous-selene) focus-visible:outline-hidden focus-visible:ring-2 focus-visible:ring-(--nous-sol)/50 focus-visible:ring-offset-2 focus-visible:ring-offset-(--nous-selene)"
        style={{ fontFamily: 'var(--nous-font-ui)' }}
      >
        Skip to main content
      </a>
      <LandingNav isAuthenticated={isAuthenticated} />
      <main id="main-content" className="flex-1">
        <HeroSection isAuthenticated={isAuthenticated} />
        <CapabilitiesSection />
        <ClosingSection isAuthenticated={isAuthenticated} />
      </main>
      <FooterSection isAuthenticated={isAuthenticated} />
    </div>
  );
}
