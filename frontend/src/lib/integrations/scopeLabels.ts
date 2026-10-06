/**
 * Plain-language text for the permissions on the integration consent page.
 * The page always shows the raw scope next to its label, and a scope this map
 * does not know falls back to its raw name, so a label can never hide one.
 */
const LABELS: Record<string, string> = {
  'harness:execute': 'Run harness sessions bound to this connection',
  'tools:read': 'Read documents, drafts and search results in scope',
  'tools:write': 'Request changes that you approve one by one',
  'artifacts:publish': 'Publish files from the registered output folder',
  'context:read': 'Read selected memories',
  'library:read': 'List the folders (projects) in this workspace',
  'library:write':
    'Add, remove, move and rename items in this workspace without asking each time. Deleting folders and ingesting papers still require your approval.',
};

// Own keys only: a scope named like an Object.prototype member ("constructor")
// must fall back to its raw name, not resolve to an inherited function.
export const scopeLabel = (scope: string): string =>
  Object.prototype.hasOwnProperty.call(LABELS, scope) ? LABELS[scope] : scope;
