import type { Metadata } from 'next';
import localFont from 'next/font/local';
import './globals.css';
import './nous-tokens.css';
import { Providers } from './providers';

/**
 * Fonts are bundled under `src/fonts/` and loaded with `next/font/local` so
 * `next build` never fetches from Google Fonts (vercel/next.js#99114 broke the
 * production image build). Provenance, hashes and the root cause are in
 * `src/fonts/README.md`. Variables, weights and styles match the previous
 * Google declarations; Inter is the authentic variable font pinned at opsz=14.
 */
const inter = localFont({
  src: '../src/fonts/inter/Inter-opsz14-VariableFont_wght.woff2',
  weight: '100 900',
  style: 'normal',
  variable: '--font-inter',
});
const jetbrainsMono = localFont({
  src: '../src/fonts/jetbrains-mono/JetBrainsMono-VariableFont_wght.woff2',
  weight: '100 800',
  style: 'normal',
  variable: '--font-mono',
});
const outfit = localFont({
  src: '../src/fonts/outfit/Outfit-VariableFont_wght.woff2',
  // The asset carries the full 100..900 axis; the declared range matches the
  // six weights (300..800) the previous Google declaration loaded.
  weight: '300 800',
  style: 'normal',
  variable: '--font-display',
});
const sourceSerif4 = localFont({
  src: [
    {
      path: '../src/fonts/source-serif-4/SourceSerif4-VariableFont_opsz-wght.woff2',
      weight: '200 900',
      style: 'normal',
    },
    {
      path: '../src/fonts/source-serif-4/SourceSerif4-Italic-VariableFont_opsz-wght.woff2',
      weight: '200 900',
      style: 'italic',
    },
  ],
  variable: '--font-serif',
  // next/font/local defaults the size-adjusted fallback to Arial; the Google
  // loader used the family's serif category, so keep a serif fallback.
  adjustFontFallback: 'Times New Roman',
});

export const metadata: Metadata = {
  title: 'NOUS | Multimodal Intelligence',
  description:
    'NOUS — Multimodal intelligence platform for research, document processing, and knowledge graph capabilities',
};

/**
 * Force dynamic rendering app-wide.
 *
 * proxy.ts (#699) sets a per-request CSP `script-src 'self' 'nonce-…'
 * 'strict-dynamic'` with no `unsafe-inline`. Next only stamps that per-request
 * nonce onto its inline bootstrap scripts when a route renders dynamically; a
 * statically prerendered page's scripts carry no matching nonce, so
 * strict-dynamic blocks every script and the page renders blank (login,
 * register, home and the other auth pages were all statically optimized).
 * A nonce-based CSP is incompatible with static prerendering, so the app must
 * render dynamically for the nonce to apply.
 */
export const dynamic = 'force-dynamic';

export default function RootLayout({
  children,
}: {
  children: React.ReactNode;
}) {
  return (
    <html
      lang="en"
      suppressHydrationWarning
      className={`${inter.variable} ${jetbrainsMono.variable} ${outfit.variable} ${sourceSerif4.variable}`}
    >
      <head>
        <meta
          name="viewport"
          content="width=device-width, initial-scale=1, viewport-fit=cover"
        />
      </head>
      <body
        className={`${inter.className} antialiased bg-background text-foreground`}
        suppressHydrationWarning
      >
        <Providers>{children}</Providers>
      </body>
    </html>
  );
}
