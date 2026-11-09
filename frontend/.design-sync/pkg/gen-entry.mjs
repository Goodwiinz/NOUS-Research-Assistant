// Generates .design-sync/pkg/index.ts - the curated barrel /design-sync bundles.
// The frontend is a Next.js app, not a published package: there is no dist/
// or .d.ts tree, so this wrapper package stands in for one. Run from frontend/:
//   node .design-sync/pkg/gen-entry.mjs
// Component discovery: PascalCase value exports that look like React
// components (function decls, arrow/function vars, forwardRef/memo calls,
// Radix `Primitive.Part` aliases) in src/components/** and app/components/**.
// First export wins on a name collision (priority order below); collisions
// are listed in the generated header so they're visible in review.
import { createRequire } from 'node:module';
import { mkdirSync, readdirSync, rmSync, statSync, writeFileSync } from 'node:fs';
import { join, relative } from 'node:path';

const require = createRequire(new URL('../../.ds-sync/package.json', import.meta.url));
const { Project, Node, SyntaxKind } = require('ts-morph');

const FRONTEND = new URL('../../', import.meta.url).pathname;
const PKG = new URL('./', import.meta.url).pathname;

// Mirrors the tsconfig.json `exclude` list for component files (they don't
// type-check) plus demo/test scaffolding that isn't product UI.
const EXCLUDE = [
  /__tests__\//, /\.(test|spec|stories)\.tsx$/, /\/demo\//, /-demo\.tsx$/,
  /src\/components\/layout\/(AppLayout|Header|Sidebar)\.tsx$/,
  /src\/components\/monitoring\/(PerformanceMetricsDashboard|SystemOverviewDashboard)\.tsx$/,
  /src\/components\/ui\/enhanced-new-chat-dialog\.tsx$/,
  /src\/components\/ui\/shadcn-io\//,
  /src\/components\/analytics\/AnalyticsProvider\.tsx$/,
  /app\/components\/AppNavbar\.tsx$/,
  // Legacy pre-NOUS nav ("RAG System" logo), imported nowhere; NousNav replaces
  // it, and it renders null without a signed-in session.
  /app\/components\/Navigation\.tsx$/,
  // Unmounted legacy [Doc N] chip with hardcoded Tailwind blue; chat cites
  // through CitationLink, which is the one designs should use.
  /src\/components\/citations\/CitationPreview\.tsx$/,
  // Pre-shadcn-sidebar toggle, imported nowhere; needs a SidebarProvider.
  /src\/components\/layout\/sidebar\/SidebarToggle\.tsx$/,
  // cytoscape (+ layout-base) is ~1.2 MB of the 12 MB bundle cap, and only
  // this canvas graph uses it; it renders nothing without live graph data.
  /src\/components\/citations\/CitationGraph\.tsx$/,
];
// Earlier roots win name collisions: brand kit, then primitives, then the rest.
const ROOTS = ['app/components/nous', 'src/components/ui', 'src/components/elements', 'app/components', 'src/components'];

const walk = (d) => readdirSync(d).flatMap((n) => {
  const p = join(d, n);
  return statSync(p).isDirectory() ? walk(p) : /\.tsx$/.test(n) ? [p] : [];
});
const seenFiles = new Set();
const files = [];
for (const r of ROOTS) {
  for (const p of walk(join(FRONTEND, r)).sort()) {
    const rel = relative(FRONTEND, p);
    if (seenFiles.has(rel) || EXCLUDE.some((rx) => rx.test(rel))) continue;
    seenFiles.add(rel);
    files.push(p);
  }
}

const project = new Project({ skipAddingFilesFromTsConfig: true, skipFileDependencyResolution: true });
const COMPONENT_CALL = /(forwardRef|memo|lazy|dynamic)\s*[(<]/;
const looksLikeComponent = (d) => {
  if (Node.isFunctionDeclaration(d)) return true;
  if (Node.isClassDeclaration(d)) return /Component/.test(d.getExtends()?.getText() ?? '');
  if (!Node.isVariableDeclaration(d)) return false;
  const init = d.getInitializer();
  if (!init) return false;
  const k = init.getKind();
  if (k === SyntaxKind.ArrowFunction || k === SyntaxKind.FunctionExpression) return true;
  if (k === SyntaxKind.CallExpression) return COMPONENT_CALL.test(init.getExpression().getText() + '(');
  if (k === SyntaxKind.PropertyAccessExpression) return /\.[A-Z]\w*$/.test(init.getText());
  return false;
};

const byName = new Map();
const collisions = [];
for (const p of files) {
  const sf = project.addSourceFileAtPath(p);
  const spec = './' + relative(PKG, p).replace(/\.tsx$/, '');
  for (const [name, decls] of sf.getExportedDeclarations()) {
    const local = name === 'default' ? decls.map((d) => d.getName?.()).find(Boolean) : name;
    if (!local || !/^[A-Z][A-Za-z0-9]*$/.test(local)) continue;
    // Only decls declared in THIS file - re-exports are picked up at their source.
    if (!decls.some((d) => d.getSourceFile() === sf && looksLikeComponent(d))) continue;
    // Same file exporting a component both named and default - keep the named one.
    if (byName.get(local)?.spec === spec) { if (name !== 'default') byName.set(local, { spec, isDefault: false }); continue; }
    if (byName.has(local)) { collisions.push(`${local}: ${byName.get(local).spec} wins over ${spec}`); continue; }
    byName.set(local, { spec, isDefault: name === 'default' });
  }
}

const bySpec = new Map();
for (const [name, { spec, isDefault }] of byName) {
  if (!bySpec.has(spec)) bySpec.set(spec, []);
  bySpec.get(spec).push(isDefault ? `default as ${name}` : name);
}
const out = [
  '// GENERATED by .design-sync/pkg/gen-entry.mjs - do not edit; re-run the generator.',
  ...collisions.map((c) => `// collision: ${c}`),
  ...[...bySpec].map(([spec, names]) => `export { ${names.sort().join(', ')} } from '${spec}';`),
  '',
].join('\n');
writeFileSync(join(PKG, 'index.ts'), out);

// Category-only doc stubs (cfg.docsDir = docs/): the converter groups by the
// file's last non-generic dir, which sends every multi-export file (dialog.tsx
// -> DialogContent) and all of components/ui/ to "general". A frontmatter-only
// stub regroups without replacing the synthesized .prompt.md body.
const groupFor = (spec) => {
  const rel = spec.replace(/^(\.\/)?(\.\.\/)+/, '');
  if (rel.startsWith('app/components/nous/')) return 'Brand';
  if (rel.startsWith('app/components/')) return 'App shell';
  const top = rel.split('/')[2];
  return { ui: 'UI', elements: 'Elements' }[top] ?? top;
};
const DOCS = join(PKG, 'docs');
rmSync(DOCS, { recursive: true, force: true });
mkdirSync(DOCS, { recursive: true });
for (const [name, { spec }] of byName) {
  writeFileSync(join(DOCS, `${name}.md`), `---\ncategory: ${groupFor(spec)}\n---\n`);
}
console.error(`gen-entry: ${byName.size} components from ${bySpec.size} files (${files.length} scanned, ${collisions.length} collisions)`);
