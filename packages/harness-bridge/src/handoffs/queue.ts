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

/** The chat this device is bound to, from local state; null when unbound. */
export async function localBinding(store: CredentialStore): Promise<Binding | null> {
  const state = await store.readLocal("connection").catch(() => null);
  if (!record(state) || !uuid(state.threadId) || !uuid(state.projectId)) return null;
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

  /** Journal the save, then POST it. */
  async submit(binding: Binding, body: HandoffCreate): Promise<Attempt> {
    if (!uuid(body.handoff_id)) throw new Error("handoff_id must be a UUID");
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
