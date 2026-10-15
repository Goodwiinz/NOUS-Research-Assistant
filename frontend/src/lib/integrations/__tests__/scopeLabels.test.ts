import { describe, expect, it } from 'vitest';

import { scopeLabel, scopeSummary } from '../scopeLabels';

// Every scope the backend accepts (STANDARD_SCOPES in
// backend/src/schemas/integration_context.py).
const STANDARD_SCOPES = [
  'harness:execute',
  'tools:read',
  'tools:write',
  'context:read',
  'artifacts:publish',
  'handoff:read',
  'handoff:write',
  'library:read',
  'library:write',
];

describe('scopeLabel', () => {
  it('explains library:write as running without per-action approval', () => {
    expect(scopeLabel('library:write')).toMatch(/without asking each time/);
  });
  it('names the paper edits library:write runs without asking', () => {
    // update_document_metadata auto-runs too: it replaces a paper's title or
    // tags (or clears every tag) on the paper itself, in every folder.
    const label = scopeLabel('library:write');
    expect(label).toMatch(/change the title and tags of a paper/);
    expect(label).toMatch(/removing every tag/);
    expect(label).toMatch(/changes everywhere it appears/);
  });
  it('says what library:write still leaves to the user', () => {
    expect(scopeLabel('library:write')).toMatch(
      /Deleting folders and ingesting papers still require your approval/
    );
  });
});

// scopeLabel is the consent page's sentence; scopeSummary is the short
// fragment the Connected devices page joins into one line per consent.
describe.each([
  ['scopeLabel', scopeLabel],
  ['scopeSummary', scopeSummary],
] as const)('%s', (_name, describeScope) => {
  it('falls back to the raw scope', () => {
    expect(describeScope('weird:scope')).toBe('weird:scope');
  });
  it.each(STANDARD_SCOPES)(
    'labels %s in words, not as the raw scope',
    (scope) => {
      expect(describeScope(scope)).not.toBe(scope);
    }
  );
  it.each(['constructor', 'toString', 'hasOwnProperty', '__proto__'])(
    'treats the object key %s as an unknown scope',
    (scope) => {
      expect(describeScope(scope)).toBe(scope);
    }
  );
});

describe('account wording', () => {
  it('says Codex can run from a chat in any project in the account', () => {
    expect(scopeLabel('harness:execute', 'account')).toMatch(
      /any project in your account/
    );
    expect(scopeSummary('harness:execute', 'account')).toBe(
      'run Local Codex from any chat'
    );
  });
  it('reaches every project in the account for library:write, and keeps what still asks', () => {
    const label = scopeLabel('library:write', 'account');
    expect(label).toMatch(/any project in your account/);
    expect(label).toMatch(/without asking each time/);
    expect(label).toMatch(
      /Deleting folders and ingesting papers still require your approval/
    );
    expect(label).not.toMatch(/this workspace/);
  });
  it('lists the workspaces and projects of the account for library:read', () => {
    expect(scopeLabel('library:read', 'account')).toBe(
      'List the workspaces and projects in your account'
    );
  });
});

describe('project and workspace wording', () => {
  it.each(STANDARD_SCOPES)('is unchanged for %s', (scope) => {
    expect(scopeLabel(scope, 'project')).toBe(scopeLabel(scope));
    expect(scopeLabel(scope, 'workspace')).toBe(scopeLabel(scope));
    expect(scopeSummary(scope, 'workspace')).toBe(scopeSummary(scope));
  });
  it('still names the workspace for library:write', () => {
    expect(scopeLabel('library:write', 'workspace')).toMatch(
      /items in this workspace/
    );
  });
});

describe.each([
  ['scopeLabel', scopeLabel],
  ['scopeSummary', scopeSummary],
] as const)('%s for an account', (_name, describeScope) => {
  it.each(STANDARD_SCOPES)('labels %s in words', (scope) => {
    expect(describeScope(scope, 'account')).not.toBe(scope);
  });
  it.each(['weird:scope', 'constructor', '__proto__'])(
    'falls back to the raw name for %s',
    (scope) => {
      expect(describeScope(scope, 'account')).toBe(scope);
    }
  );
});
