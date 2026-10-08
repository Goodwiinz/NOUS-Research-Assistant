/**
 * Plain-language text for integration scopes. The consent page shows a full
 * sentence (`scopeLabel`) next to the raw scope; the Connected devices page
 * joins short fragments (`scopeSummary`) into one line per consent. A scope a
 * map does not know falls back to its raw name, so a label can never hide one.
 */
const LABELS: Record<string, string> = {
  'harness:execute': 'Run harness sessions bound to this connection',
  'tools:read': 'Read documents, drafts and search results in scope',
  'tools:write': 'Request changes that you approve one by one',
  'artifacts:publish': 'Publish files from the registered output folder',
  'context:read': 'Read selected memories',
  'handoff:read':
    "Read this chat's saved handoff (goal, decisions, remaining work and results)",
  'handoff:write':
    'Save a handoff for this chat (goal, decisions, remaining work and results)',
  'library:read': 'List the folders (projects) in this workspace',
  // update_document_metadata runs without asking too, and edits the paper
  // itself, so the sentence names it and where the change shows.
  'library:write':
    'Add, remove, move and rename items in this workspace, and change the title and tags of a paper in it (including removing every tag), without asking each time. An edited paper changes everywhere it appears. Deleting folders and ingesting papers still require your approval.',
};

const SUMMARIES: Record<string, string> = {
  'harness:execute': 'run coding sessions',
  'tools:read': 'read project documents',
  'tools:write': 'request notes (you approve each one)',
  'context:read': 'read memories you chose to share',
  'artifacts:publish': 'publish files to chats',
  'handoff:read': "read this chat's handoff",
  'handoff:write': "save this chat's handoff",
  'library:read': 'list the folders in the workspace',
  'library:write': 'change folders and paper details without asking each time',
};

// Own keys only: a scope named like an Object.prototype member ("constructor")
// must fall back to its raw name, not resolve to an inherited function.
const ownText = (texts: Record<string, string>, scope: string): string =>
  Object.prototype.hasOwnProperty.call(texts, scope) ? texts[scope] : scope;

export const scopeLabel = (scope: string): string => ownText(LABELS, scope);

export const scopeSummary = (scope: string): string =>
  ownText(SUMMARIES, scope);
