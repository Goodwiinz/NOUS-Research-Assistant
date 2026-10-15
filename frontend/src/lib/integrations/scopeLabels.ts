import type { ApiBindingKind } from '@/types/api/integration-connections-contract';

/**
 * Plain-language text for integration scopes. The consent page shows a full
 * sentence (`scopeLabel`) next to the raw scope; the Connected devices page
 * joins short fragments (`scopeSummary`) into one line per consent. A scope a
 * map does not know falls back to its raw name, so a label can never hide one.
 *
 * Project and workspace approvals share one wording. An account approval
 * reaches every project in the account, so its scopes say so. The scopes an
 * account approval cannot hold (publishing, memories, handoffs) have no
 * account wording and fall back to the shared one.
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

const ACCOUNT_LABELS: Record<string, string> = {
  'harness:execute':
    'Run Local Codex on this computer from a chat in any project in your account',
  'tools:read':
    'Read documents, drafts and search results in every project in your account',
  'tools:write':
    'Request changes in any project in your account that you approve one by one',
  'library:read': 'List the workspaces and projects in your account',
  'library:write':
    'Add, remove and move papers in any project in your account, create folders in any workspace, rename any project, and change the title and tags of a paper (including removing every tag), without asking each time. An edited paper changes everywhere it appears. Deleting folders and ingesting papers still require your approval.',
};

const SUMMARIES: Record<string, string> = {
  'harness:execute': 'run coding sessions',
  'tools:read': 'read project documents',
  'tools:write': 'request changes (you approve each one)',
  'context:read': 'read memories you chose to share',
  'artifacts:publish': 'publish files to chats',
  'handoff:read': "read this chat's handoff",
  'handoff:write': "save this chat's handoff",
  'library:read': 'list the folders in the workspace',
  'library:write': 'change folders and paper details without asking each time',
};

const ACCOUNT_SUMMARIES: Record<string, string> = {
  'harness:execute': 'run Local Codex from any chat',
  'tools:read': 'read documents in every project',
  'tools:write': 'request changes in any project (you approve each one)',
  'library:read': 'list your workspaces and projects',
  'library:write':
    'add, move and remove papers, create folders, rename projects and edit paper details anywhere in your account without asking each time',
};

// Own keys only: a scope named like an Object.prototype member ("constructor")
// must fall back to its raw name, not resolve to an inherited function.
const own = (
  texts: Record<string, string>,
  scope: string
): string | undefined =>
  Object.prototype.hasOwnProperty.call(texts, scope) ? texts[scope] : undefined;

const textFor = (
  accountTexts: Record<string, string>,
  texts: Record<string, string>,
  scope: string,
  kind: ApiBindingKind
): string =>
  (kind === 'account' ? own(accountTexts, scope) : undefined) ??
  own(texts, scope) ??
  scope;

export const scopeLabel = (
  scope: string,
  kind: ApiBindingKind = 'project'
): string => textFor(ACCOUNT_LABELS, LABELS, scope, kind);

export const scopeSummary = (
  scope: string,
  kind: ApiBindingKind = 'project'
): string => textFor(ACCOUNT_SUMMARIES, SUMMARIES, scope, kind);
