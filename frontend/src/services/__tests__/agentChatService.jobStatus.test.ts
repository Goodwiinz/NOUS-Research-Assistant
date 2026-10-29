/**
 * AgentJobStatus / isTerminalJobStatus — the frontend mirror of backend
 * JobStatus (backend/src/shared/enums.py, audit finding C7).
 *
 * The poller previously hand-listed 'completed'/'failed' and spun for its
 * full poll budget on 'error' and 'cancelled' jobs. These tests pin the full
 * truth table and enforce exhaustiveness: the `satisfies
 * Record<AgentJobStatus, boolean>` table below fails to COMPILE when a new
 * status is added to the union without being classified, complementing the
 * `never` check inside isTerminalJobStatus itself.
 */

import { describe, expect, it, vi } from 'vitest';

vi.mock('@/services/apiClient', () => ({
  apiClient: {
    get: vi.fn(),
    post: vi.fn(),
  },
}));

vi.mock('@/lib/supabase/client', () => ({
  createClient: () => ({
    auth: {
      getSession: async () => ({ data: { session: null } }),
    },
  }),
}));

import { isTerminalJobStatus, type AgentJobStatus } from '../agentChatService';

// Compile-time exhaustiveness: adding a member to AgentJobStatus without a
// row here is a type error. Runtime: the table drives the truth-table test.
const TRUTH_TABLE = {
  queued: false,
  running: false,
  awaiting_confirmation: false,
  stopping: false,
  delegated: false,
  completed: true,
  failed: true,
  error: true, // legacy alias for 'failed' — one-release transition
  cancelled: true,
} satisfies Record<AgentJobStatus, boolean>;

describe('isTerminalJobStatus', () => {
  it.each(Object.entries(TRUTH_TABLE))(
    'classifies %s as terminal=%s',
    (status, terminal) => {
      expect(isTerminalJobStatus(status as AgentJobStatus)).toBe(terminal);
    }
  );

  it('covers every member of the union (runtime mirror of the type check)', () => {
    const classified = Object.keys(TRUTH_TABLE).sort();
    expect(classified).toEqual(
      [
        'queued',
        'running',
        'awaiting_confirmation',
        'stopping',
        'delegated',
        'completed',
        'failed',
        'error',
        'cancelled',
      ].sort()
    );
  });

  it('keeps polling on a runtime-unknown status (forward compatibility)', () => {
    // A newer backend may introduce a status this build does not know.
    // The poller must not treat it as terminal (its poll budget bounds the
    // loop) — mirrors the `never` default arm.
    expect(isTerminalJobStatus('warming_up' as AgentJobStatus)).toBe(false);
  });

  it('has at least one terminal and one non-terminal status', () => {
    const values = Object.values(TRUTH_TABLE);
    expect(values).toContain(true);
    expect(values).toContain(false);
  });
});
