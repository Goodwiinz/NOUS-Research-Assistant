import type { components } from "../../../../frontend/src/types/generated/api.d.ts";
import { integrationHeaders, type IntegrationCredentials } from "../credentials.ts";
import { GrantExpired } from "../grants.ts";
import { apiBase, throwForStatus } from "../mcp/client.ts";

export type Handoff = components["schemas"]["HandoffDTO"];
export type HandoffCreate = components["schemas"]["HandoffCreate"];
export type HandoffConflictBody = components["schemas"]["HandoffConflictBody"];
/** A save outcome: stored, or a 409 carrying the latest version to merge. */
export type SaveOutcome =
  | { saved: Handoff }
  | { conflict: Handoff | null; detail: string };

const MESSAGES = {
  forbidden:
    "NOUS denied the handoff: the grant may lack handoff:read/handoff:write or is not bound to a chat (reconnect with nous-harness connect --chat UUID --tools --handoff)",
  disabled: "NOUS integration tools are disabled",
};
const UUID_RE = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
export const uuid = (value: unknown): value is string =>
  typeof value === "string" && UUID_RE.test(value);
const CONFLICT_DETAIL = "NOUS reported a conflict";
const STATUS = Symbol("httpStatus");
/** The HTTP status behind a failed handoff request; undefined for network failures. */
export function statusOf(error: unknown): number | undefined {
  return typeof error === "object" && error !== null
    ? (error as { [STATUS]?: number })[STATUS]
    : undefined;
}
/** throwForStatus, tagging the thrown error with the status for the offline queue. */
async function check(response: Response): Promise<void> {
  try {
    await throwForStatus(response, MESSAGES);
  } catch (error) {
    if (typeof error === "object" && error !== null)
      (error as { [STATUS]?: number })[STATUS] = response.status;
    throw error;
  }
}

function parseHandoff(data: unknown): Handoff {
  const value = data as Handoff;
  if (
    typeof data !== "object" ||
    data === null ||
    !uuid(value.id) ||
    !uuid(value.thread_id) ||
    typeof value.version !== "number" ||
    typeof value.goal !== "string"
  )
    throw new Error("invalid NOUS handoff");
  return value;
}

/** Every 409 is `{detail, latest}`; anything else is reported, never guessed. */
function parseConflict(data: unknown): SaveOutcome {
  const body = data as Partial<HandoffConflictBody> | undefined;
  const detail =
    typeof body?.detail === "string" && body.detail.length <= 500 ? body.detail : CONFLICT_DETAIL;
  if (typeof body !== "object" || body === null || body.latest === undefined)
    return { conflict: null, detail: CONFLICT_DETAIL };
  if (body.latest === null) return { conflict: null, detail };
  try {
    return { conflict: parseHandoff(body.latest), detail };
  } catch {
    return { conflict: null, detail: CONFLICT_DETAIL };
  }
}

/** Scoped HTTPS client for the chat handoff bound to this grant. */
export class HandoffHttpClient {
  private readonly base: string;
  private readonly headers: Record<string, string>;
  private readonly fetchFn: typeof fetch;
  constructor(apiOrigin: string, credentials: IntegrationCredentials, fetchFn: typeof fetch = fetch) {
    this.base = apiBase(apiOrigin);
    this.headers = integrationHeaders(credentials);
    this.fetchFn = fetchFn;
  }
  /** Latest handoff in the bound chat, or null when none exists yet. */
  async getLatest(): Promise<Handoff | null> {
    const response = await this.send("GET", "/integrations/handoffs/latest");
    if (response.status === 404) return null;
    await check(response);
    return parseHandoff(await this.json(response));
  }
  async save(payload: HandoffCreate): Promise<SaveOutcome> {
    const response = await this.send("POST", "/integrations/handoffs", payload);
    // throwForStatus drops structured bodies; the 409 body is the latest version.
    if (response.status === 409) return parseConflict(await this.json(response).catch(() => undefined));
    await check(response);
    return { saved: parseHandoff(await this.json(response)) };
  }
  private async json(response: Response): Promise<unknown> {
    try {
      return await response.json();
    } catch {
      throw new Error("invalid NOUS response");
    }
  }
  private async send(method: "GET" | "POST", path: string, body?: object): Promise<Response> {
    const url = this.base + path;
    try {
      return await this.fetchFn(url, {
        method,
        redirect: "error",
        headers: { ...this.headers, ...(body ? { "Content-Type": "application/json" } : {}) },
        ...(body ? { body: JSON.stringify(body) } : {}),
        signal: AbortSignal.timeout(20_000),
      });
    } catch (error) {
      // The keeper's fetch refuses an expired grant: that needs a reconnect, not a retry.
      if (error instanceof GrantExpired) throw error;
      const cause =
        error instanceof Error && error.cause instanceof Error
          ? error.cause.message
          : error instanceof Error
            ? error.message
            : String(error);
      const wrapped = new Error(`NOUS request to ${url} failed: ${cause}`);
      console.error(wrapped.message);
      throw wrapped;
    }
  }
}
