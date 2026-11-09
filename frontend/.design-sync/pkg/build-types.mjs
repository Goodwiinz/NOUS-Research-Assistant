// Emits .design-sync/pkg/types/ (the wrapper package's .d.ts tree) and
// rewrites tsconfig `@/` path aliases to relative specifiers - tsc keeps
// aliases verbatim, and the design-sync type extractor has no `paths`
// support, so unrewritten aliases would collapse imported prop types to any.
// Run from frontend/ after gen-entry.mjs:  node .design-sync/pkg/build-types.mjs
import { execFileSync } from 'node:child_process';
import { existsSync, readdirSync, readFileSync, rmSync, statSync, writeFileSync } from 'node:fs';
import { dirname, join, relative } from 'node:path';

const FRONTEND = new URL('../../', import.meta.url).pathname;
const PKG = new URL('./', import.meta.url).pathname;
const TYPES = join(PKG, 'types');

rmSync(TYPES, { recursive: true, force: true });
try {
  execFileSync(join(FRONTEND, '../node_modules/.bin/tsc'), ['-p', join(PKG, 'tsconfig.json')], { stdio: 'inherit' });
} catch {
  // noEmitOnError is false: declarations are still written. Type errors are
  // the app's own (tsc --noEmit in CI owns them), not a sync blocker.
  console.error('build-types: tsc reported errors; declarations emitted anyway');
}

// Same order as tsconfig.json `paths`: `@/nous` -> app/components/nous,
// then `@/*` -> src/* | app/*.
const ALIASES = [
  [/^@\/nous$/, ['app/components/nous/index']],
  [/^@\/nous\/(.*)$/, ['app/components/nous/$1']],
  [/^@\/(.*)$/, ['src/$1', 'app/$1']],
];
const EXTS = ['.d.ts', '/index.d.ts'];
const SRC_EXTS = ['.d.ts', '.ts', '.tsx', '/index.ts', '/index.tsx'];
const resolveAlias = (spec) => {
  for (const [rx, targets] of ALIASES) {
    if (!rx.test(spec)) continue;
    for (const t of targets) {
      const base = spec.replace(rx, t);
      // Prefer the emitted declaration; fall back to the original source for
      // ambient .d.ts files tsc never re-emits.
      if (EXTS.some((e) => existsSync(join(TYPES, base + e)))) return { root: TYPES, base };
      if (SRC_EXTS.some((e) => existsSync(join(FRONTEND, base + e)))) return { root: FRONTEND, base };
    }
    return null;
  }
  return null;
};

const walk = (d) => readdirSync(d).flatMap((n) => {
  const p = join(d, n);
  return statSync(p).isDirectory() ? walk(p) : n.endsWith('.d.ts') ? [p] : [];
});
let rewrites = 0, misses = 0;
for (const file of walk(TYPES)) {
  const text = readFileSync(file, 'utf8');
  const next = text.replace(/(from\s+|import\(\s*)(['"])(@\/[^'"]+)\2/g, (m, lead, q, spec) => {
    const hit = resolveAlias(spec);
    if (!hit) { misses++; return m; }
    let rel = relative(dirname(file), join(hit.root, hit.base));
    if (!rel.startsWith('.')) rel = './' + rel;
    rewrites++;
    return `${lead}${q}${rel}${q}`;
  });
  if (next !== text) writeFileSync(file, next);
}
console.error(`build-types: ${rewrites} alias imports rewritten, ${misses} unresolved`);
