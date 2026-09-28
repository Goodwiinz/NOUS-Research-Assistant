/**
 * Contract test for the agent SSE event vocabulary (audit finding C1).
 *
 * `agentStreamEvents.ts` is the single frontend mirror of the backend
 * `AgentStreamEvent` StrEnum. This test enforces three things so the vocabulary
 * cannot silently drift across the wire:
 *   1. `AGENT_STREAM_EVENTS` still equals the exact backend wire values.
 *   2. `HANDLED_STREAM_EVENTS` covers every value — including `heartbeat`,
 *      whose `elapsed_ms` drives the live thinking-pill readout — and adds
 *      nothing extra.
 *   3. The exported `HANDLED_STREAM_EVENTS` set actually matches the `case`
 *      labels of the real `switch (ev)` in agentChatService.ts — parsed from
 *      source — so a new `case` (or a removed one) that forgets the set fails.
 *
 * The backend counterpart is
 * backend/tests/contract/test_sse_event_vocabulary.py; keep both in step.
 */
import { readFileSync } from 'node:fs';
import { dirname, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

import { describe, expect, it } from 'vitest';

import { HANDLED_STREAM_EVENTS } from '../agentChatService';
import {
  AGENT_STREAM_EVENTS,
  HEARTBEAT_STREAM_EVENT,
  TERMINAL_STREAM_EVENTS,
  type AgentStreamEvent,
} from '../agentStreamEvents';

// The frozen wire contract — must match backend AgentStreamEvent values (and
// the backend's own pinned list). A rename on either side breaks a client.
const EXPECTED_WIRE_VALUES = [
  'token',
  'reasoning_delta',
  'tool_start',
  'tool_end',
  'rag_context',
  'plan',
  'reflection',
  'trace',
  'usage',
  'heartbeat',
  'status',
  'confirmation',
  'approval_required',
  'done',
  'error',
];

/**
 * Extract the `case '<x>':` labels of a specific `switch (<discriminant>) {`
 * by brace-matching its body — so we read the REAL switch in agentChatService,
 * not the unrelated `switch (status)` in isTerminalJobStatus.
 */
function extractSwitchCases(source: string, discriminant: string): string[] {
  const marker = `switch (${discriminant}) {`;
  const start = source.indexOf(marker);
  if (start === -1) {
    throw new Error(`could not find "switch (${discriminant}) {" in source`);
  }
  let depth = 0;
  let end = -1;
  for (let i = start + marker.length - 1; i < source.length; i++) {
    const c = source[i];
    if (c === '{') depth++;
    else if (c === '}') {
      depth--;
      if (depth === 0) {
        end = i;
        break;
      }
    }
  }
  if (end === -1) throw new Error('unterminated switch block');
  const body = source.slice(start, end);
  return [...body.matchAll(/case '([^']+)':/g)].map((m) => m[1]);
}

const AGENT_CHAT_SERVICE_SRC = readFileSync(
  resolve(dirname(fileURLToPath(import.meta.url)), '../agentChatService.ts'),
  'utf8'
);

describe('agent SSE event vocabulary contract', () => {
  it('pins the frozen wire values (mirror of backend AgentStreamEvent)', () => {
    expect([...AGENT_STREAM_EVENTS]).toEqual(EXPECTED_WIRE_VALUES);
  });

  it('HANDLED_STREAM_EVENTS covers every event, nothing extra', () => {
    const expectedHandled = new Set<AgentStreamEvent>(AGENT_STREAM_EVENTS);
    expect(new Set(HANDLED_STREAM_EVENTS)).toEqual(expectedHandled);
    // heartbeat carries elapsed_ms — the only live progress signal during a
    // silent planner/LLM phase, so it must stay handled.
    expect(HANDLED_STREAM_EVENTS.has(HEARTBEAT_STREAM_EVENT)).toBe(true);
  });

  it('exported HANDLED_STREAM_EVENTS matches the real switch (ev) cases', () => {
    const switchCases = extractSwitchCases(AGENT_CHAT_SERVICE_SRC, 'ev');
    // No duplicate/missing labels vs. the exported set.
    expect(new Set(switchCases)).toEqual(new Set(HANDLED_STREAM_EVENTS));
    // Every case the switch handles is a known wire event (a new literal fails).
    const known = new Set<string>(AGENT_STREAM_EVENTS);
    for (const label of switchCases) {
      expect(known.has(label)).toBe(true);
    }
  });

  it('TERMINAL_STREAM_EVENTS is a subset of the vocabulary', () => {
    for (const ev of TERMINAL_STREAM_EVENTS) {
      expect((AGENT_STREAM_EVENTS as readonly string[]).includes(ev)).toBe(
        true
      );
    }
    expect(new Set(TERMINAL_STREAM_EVENTS)).toEqual(
      new Set(['done', 'error', 'confirmation'])
    );
  });
});
