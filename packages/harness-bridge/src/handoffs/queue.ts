import type { CredentialStore } from "../credentials.ts";
import { GrantExpired } from "../grants.ts";
import { record } from "../rpc.ts";
import { ReauthenticationRequired } from "../mcp/client.ts";
import { statusOf, uuid, type Handoff, type HandoffCreate, type SaveOutcome } from "./client.ts";

/**
 * Local journal of handoff saves, one owner-only file per handoff_id.
 *
 * A save is written here before the POST and deleted after a 2xx. What stays
 * is either `pending` (outcome unknown or retryable: network, timeout, 5xx,
 * 429, 401, expired grant), `conflicted` (409: the latest version is stored
 * for a person or model to merge; never merged here) or `rejected` (other
 * 4xx such as 403/422: terminal, kept with the error). `flush` re-sends only
 * pending entries for the current chat, with the same handoff_id so NOUS
 * dedupes a save that did land. There is no background retry.
 */
export type QueueState = "pending" | "conflicted" | "rejected";
export type QueueEntry = {
  handoff_id: string;
  thread_id: string;
  project_id: string;
  body: HandoffCreate;
  state: QueueState;
  created_at: string;
  last_error?: string;
  latest?: Handoff | null;
};
export type Binding = { threadId: string; projectId: string };
export type Attempt =
  | { state: "done"; saved: Handoff }
  | { state: "conflicted"; latest: Handoff | null; detail: string }
  | { state: "rejected"; error: Error }
  | { state: "pending"; error: Error; reconnect: boolean };
export type FlushResult = {
  attempted: { entry: QueueEntry; attempt: Attempt }[];
  skipped: QueueEntry[];
};

const PREFIX = "handoff-";

function entryOf(value: unknown): QueueEntry | null {
  if (
    !record(value) ||
    !uuid(value.handoff_id) ||
    !uuid(value.thread_id) ||
    !uuid(value.project_id) ||
    !record(value.body) ||
    value.body.handoff_id !== value.handoff_id ||
    !["pending", "conflicted", "rejected"].includes(value.state as string)
  )
    return null;
  return value as QueueEntry;
}

/** A different save already journaled under this handoff_id; it is never overwritten. */
export class HandoffIdInUse extends Error {
  constructor(handoffId: string) {
    super(
      `handoff_id ${handoffId} is already queued with a different body or chat; run nous-harness handoff discard ${handoffId} or save with a new handoff_id`,
    );
  }
}

/** JSON with object keys sorted, so equal bodies compare equal whatever their key order. */
function canonical(value: unknown): string {
  if (Array.isArray(value)) return `[${value.map(canonical).join(",")}]`;
  if (record(value))
    return `{${Object.keys(value)
      .filter((key) => value[key] !== undefined)
      .sort()
      .map((key) => `${JSON.stringify(key)}:${canonical(value[key])}`)
      .join(",")}}`;
  return JSON.stringify(value);
}

/** Retryable failures stay pending; any other refusal is terminal. */
function classify(error: unknown): Extract<Attempt, { state: "pending" | "rejected" }> {
  const err = error instanceof Error ? error : new Error(String(error));
  if (err instanceof GrantExpired || err instanceof ReauthenticationRequired)
    return { state: "pending", error: err, reconnect: true };
  const status = statusOf(err);
  if (status === undefined || status === 429 || status >= 500)
    return { state: "pending", error: err, reconnect: false };
  return { state: "rejected", error: err };
}

/**
 * The chat bound to `credentialHandle`, from local state. Null when the device
 * is unbound or was reconnected under another handle: a stale MCP child must
 * never journal (and later flush) its save under the new binding's chat.
 */
export async function localBinding(store: CredentialStore, credentialHandle: string): Promise<Binding | null> {
  const state = await store.readLocal("connection").catch(() => null);
  if (
    !record(state) ||
    state.credentialHandle !== credentialHandle ||
    !uuid(state.threadId) ||
    !uuid(state.projectId)
  )
    return null;
  return { threadId: state.threadId, projectId: state.projectId };
}

/** Every journaled entry, oldest first; offline. Unreadable files are reported, not deleted. */
export async function queueEntries(store: CredentialStore): Promise<QueueEntry[]> {
  const entries: QueueEntry[] = [];
  for (const name of await store.listLocal(PREFIX)) {
    const entry = entryOf(await store.readLocal(name).catch(() => null));
    if (entry) entries.push(entry);
    else console.error(`Ignoring unreadable handoff queue entry ${name}.json`);
  }
  return entries.sort((a, b) => a.created_at.localeCompare(b.created_at));
}

export class HandoffQueue {
  constructor(
    private readonly store: CredentialStore,
    private readonly client: { save(payload: HandoffCreate): Promise<SaveOutcome> },
    private readonly now: () => Date = () => new Date(),
  ) {}

  /**
   * Journal the save, then POST it. An existing entry for the handoff_id is
   * the only copy of an earlier save: an identical retry (same chat, project
   * and body) re-sends that entry; anything else is refused, never overwritten.
   */
  async submit(binding: Binding, body: HandoffCreate): Promise<Attempt> {
    if (!uuid(body.handoff_id)) throw new Error("handoff_id must be a UUID");
    const existing = await this.store.readLocal(PREFIX + body.handoff_id).catch(() => undefined);
    if (existing !== undefined) {
      const queued = entryOf(existing);
      if (
        queued === null ||
        queued.thread_id !== binding.threadId ||
        queued.project_id !== binding.projectId ||
        canonical(queued.body) !== canonical(body)
      )
        throw new HandoffIdInUse(body.handoff_id);
      return this.attempt(queued);
    }
    const entry: QueueEntry = {
      handoff_id: body.handoff_id,
      thread_id: binding.threadId,
      project_id: binding.projectId,
      body,
      state: "pending",
      created_at: this.now().toISOString(),
    };
    await this.store.writeLocal(PREFIX + entry.handoff_id, entry);
    return this.attempt(entry);
  }

  /** Retry pending entries of the current chat; others are reported as skipped. */
  async flush(binding: Binding): Promise<FlushResult> {
    const result: FlushResult = { attempted: [], skipped: [] };
    for (const entry of await this.list()) {
      if (entry.state !== "pending") continue;
      if (entry.thread_id !== binding.threadId || entry.project_id !== binding.projectId) {
        result.skipped.push(entry);
        continue;
      }
      result.attempted.push({ entry, attempt: await this.attempt(entry) });
    }
    return result;
  }

  /** Drop one entry in any state; returns what was removed, or null. */
  async discard(handoffId: string): Promise<QueueEntry | null> {
    if (!uuid(handoffId)) throw new Error("handoff_id must be a UUID");
    const entry = entryOf(await this.store.readLocal(PREFIX + handoffId).catch(() => null));
    if (entry === null) return null;
    await this.store.removeLocal(PREFIX + handoffId);
    return entry;
  }

  list(): Promise<QueueEntry[]> {
    return queueEntries(this.store);
  }

  private async attempt(entry: QueueEntry): Promise<Attempt> {
    const key = PREFIX + entry.handoff_id;
    let outcome: SaveOutcome;
    try {
      outcome = await this.client.save(entry.body);
    } catch (error) {
      const failed = classify(error);
      await this.store.writeLocal(key, { ...entry, state: failed.state, last_error: failed.error.message });
      return failed;
    }
    if ("saved" in outcome) {
      await this.store.removeLocal(key);
      return { state: "done", saved: outcome.saved };
    }
    // The 409 envelope's `latest` is kept for a manual merge; never merged here.
    await this.store.writeLocal(key, {
      ...entry,
      state: "conflicted",
      latest: outcome.conflict,
      last_error: outcome.detail,
    });
    return { state: "conflicted", latest: outcome.conflict, detail: outcome.detail };
  }
}
