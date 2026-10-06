import { describe, expect, it } from 'vitest';

import { scopeLabel } from '../scopeLabels';

// Every scope the backend accepts (STANDARD_SCOPES in
// backend/src/schemas/integration_context.py).
const STANDARD_SCOPES = [
  'harness:execute',
  'tools:read',
  'tools:write',
  'context:read',
  'artifacts:publish',
  'library:read',
  'library:write',
];

describe('scopeLabel', () => {
  it('explains library:write as running without per-action approval', () => {
    expect(scopeLabel('library:write')).toMatch(/without asking each time/);
  });
  it('says what library:write still leaves to the user', () => {
    expect(scopeLabel('library:write')).toMatch(
      /Deleting folders and ingesting papers still require your approval/
    );
  });
  it('falls back to the raw scope', () => {
    expect(scopeLabel('weird:scope')).toBe('weird:scope');
  });
  it.each(STANDARD_SCOPES)(
    'labels %s in words, not as the raw scope',
    (scope) => {
      expect(scopeLabel(scope)).not.toBe(scope);
    }
  );
  it.each(['constructor', 'toString', 'hasOwnProperty', '__proto__'])(
    'treats the object key %s as an unknown scope',
    (scope) => {
      expect(scopeLabel(scope)).toBe(scope);
    }
  );
});
