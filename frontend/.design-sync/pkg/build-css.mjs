// Compiles the app's Tailwind v4 stylesheet into .design-sync/pkg/styles.css
// (cfg.cssEntry). The app ships Tailwind *source* (globals.css runs
// `@import 'tailwindcss'`), so the converter has nothing compiled to scrape;
// this runs the repo's own @tailwindcss/postcss over globals.css +
// nous-tokens.css with every component source as a @source, so each utility
// the components use is present in the shipped CSS.
// Run from frontend/:  node .design-sync/pkg/build-css.mjs
import { createRequire } from 'node:module';
import { existsSync, readdirSync, readFileSync, statSync, writeFileSync } from 'node:fs';
import { dirname, join, relative } from 'node:path';

const FRONTEND = new URL('../../', import.meta.url).pathname;
const PKG = new URL('./', import.meta.url).pathname;
const require = createRequire(join(FRONTEND, 'package.json'));
const postcss = require('postcss');
const tailwind = require('@tailwindcss/postcss');

// layout.tsx loads these four families via next/font/google and exposes them
// as --font-* variables on <html>. Outside Next there is no next/font, so the
// same families come from fonts.css (committed @font-face rules, fetched from
//   https://fonts.googleapis.com/css2?family=Inter:wght@300..800&family=JetBrains+Mono:wght@400..700&family=Outfit:wght@300..800&family=Source+Serif+4:ital,opsz,wght@0,8..60,300..700;1,8..60,300..700&display=swap
// with a Chrome user agent) and the variables are defined here; an undefined
// var() would invalidate every font-family that references it.
const entry = `
@import './fonts.css';
@import '../../app/globals.css';
@import '../../app/nous-tokens.css';
@source '../../src/components';
@source '../../app/components';
@source '../previews';
@source './provider.tsx';

:root {
  --font-inter: 'Inter';
  --font-display: 'Outfit';
  --font-serif: 'Source Serif 4';
  --font-mono: 'JetBrains Mono';
}
`;

const from = join(PKG, '.styles-entry.css');
const result = await postcss([tailwind({ base: FRONTEND })]).process(entry, { from });
writeFileSync(join(PKG, 'styles.css'), result.css);
console.error(`build-css: wrote styles.css (${(result.css.length / 1024).toFixed(0)} KiB)`);

// MathDisplay / ChatMarkdownMath import 'katex/dist/katex.min.css', whose
// @font-face lists .ttf fallbacks the converter's esbuild has no loader for
// (woff/woff2 only). Ship a shim with the woff2 sources alone, and route the
// import to it through tsconfig.bundle.json - the converter's paths plugin
// reads only that file's own `paths`, so the app's aliases are copied in.
const katexDir = join(dirname(require.resolve('katex/package.json')), 'dist');
const katexCss = readFileSync(join(katexDir, 'katex.min.css'), 'utf8')
  .replace(/,\s*url\(fonts\/[^)]+\.(?:woff|ttf)\)\s*format\("(?:woff|truetype)"\)/g, '')
  .replace(/url\(fonts\//g, `url(${relative(PKG, join(katexDir, 'fonts'))}/`);
writeFileSync(join(PKG, 'katex-shim.css'), katexCss);
const appTsconfig = JSON.parse(readFileSync(join(FRONTEND, 'tsconfig.json'), 'utf8'));
const prefix = relative(PKG, FRONTEND);
const paths = Object.fromEntries(Object.entries(appTsconfig.compilerOptions.paths)
  .map(([k, v]) => [k, v.map((t) => join(prefix, t))]));
// The paths plugin tries the bare stem before /index.ts, so a directory
// import (`@/components/chat`) resolves to the directory and esbuild fails
// with "is a directory". Exact-match keys for every index-bearing dir under
// src/ and app/ come first, so directory imports hit their barrel.
const dirIndexes = {};
const indexWalk = (root, rel = '') => {
  const abs = join(FRONTEND, root, rel);
  for (const n of readdirSync(abs)) {
    if (n === 'node_modules' || n.startsWith('.')) continue;
    const p = join(abs, n);
    if (!statSync(p).isDirectory()) continue;
    const sub = rel ? `${rel}/${n}` : n;
    // A sibling `<dir>.ts` beats `<dir>/index.ts` (TS module resolution order).
    const sibling = ['.ts', '.tsx'].map((e) => `${sub}${e}`).find((f) => existsSync(join(FRONTEND, root, f)));
    const idx = ['index.ts', 'index.tsx'].map((f) => `${sub}/${f}`).find((f) => existsSync(join(FRONTEND, root, f)));
    const target = sibling ?? idx;
    if (target && !dirIndexes[`@/${sub}`]) dirIndexes[`@/${sub}`] = [join(prefix, root, target)];
    indexWalk(root, sub);
  }
};
indexWalk('src');
indexWalk('app');
writeFileSync(join(PKG, 'tsconfig.bundle.json'), JSON.stringify({
  compilerOptions: {
    baseUrl: '.',
    paths: {
      'katex/dist/katex.min.css': ['./katex-shim.css'],
      // Size cap: see stubs/pdfjs-dist.ts.
      'pdfjs-dist': ['./stubs/pdfjs-dist.ts'],
      ...dirIndexes,
      ...paths,
    },
  },
}, null, 2) + '\n');
console.error('build-css: wrote katex-shim.css + tsconfig.bundle.json');
