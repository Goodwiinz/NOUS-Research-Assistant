import { createRequire, registerHooks } from 'node:module';
import { pathToFileURL } from 'node:url';

// The repository's hoisted linker installs separate React copies under Ink and
// assistant-ui. Hooks and context stores require identity, not just matching
// versions. Bind this process to React 19 and one copy of the Ink SDK's stores.
const require = createRequire(import.meta.url);
const sdkURL = pathToFileURL(require.resolve('@assistant-ui/react-ink')).href;
const react = new Map(
  ['react', 'react/jsx-runtime', 'react/jsx-dev-runtime'].map((name) => [
    name,
    pathToFileURL(require.resolve(name)).href,
  ])
);
registerHooks({
  resolve(specifier, context, nextResolve) {
    if (/^@assistant-ui\/(core|store|tap)(\/|$)/.test(specifier)) {
      return nextResolve(specifier, { ...context, parentURL: sdkURL });
    }
    return nextResolve(react.get(specifier) ?? specifier, context);
  },
});
