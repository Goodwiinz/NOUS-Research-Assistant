import type { components } from "../../../../frontend/src/types/generated/api.d.ts";
import { integrationHeaders, type IntegrationCredentials } from "../credentials.ts";
import { apiBase, throwForStatus } from "../mcp/client.ts";

export type Handoff = components["schemas"]["HandoffDTO"];
export type HandoffCreate = components["schemas"]["HandoffCreate"];
/** A save outcome: stored, or a 409 carrying the latest version to merge. */
export type SaveOutcome =
  | { saved: Handoff }
  | { conflict: Handoff | null; detail?: string };

const MESSAGES = {
  forbidden:
    "NOUS denied the handoff: the grant may lack handoff:read/handoff:write or is not bound to a chat (reconnect with nous-harness connect --chat UUID --tools --handoff)",
  disabled: "NOUS integration tools are disabled",
};
const uuid = (value: unknown): value is string =>
  typeof value === "string" &&
  /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i.test(value);

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
    await throwForStatus(response, MESSAGES);
    return parseHandoff(await this.json(response));
  }
  async save(payload: HandoffCreate): Promise<SaveOutcome> {
    const response = await this.send("POST", "/integrations/handoffs", payload);
    // throwForStatus drops structured bodies; the 409 body is the latest version.
    if (response.status === 409) {
      const data = await this.json(response).catch(() => undefined);
      const detail = (data as { detail?: unknown } | undefined)?.detail;
      if (typeof detail === "string") return { conflict: null, detail: detail.slice(0, 500) };
      return { conflict: parseHandoff(data) };
    }
    await throwForStatus(response, MESSAGES);
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
