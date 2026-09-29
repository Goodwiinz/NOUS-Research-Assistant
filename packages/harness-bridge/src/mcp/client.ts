import type { components } from "../../../../frontend/src/types/generated/api.d.ts";
import {
  integrationHeaders,
  type IntegrationCredentials,
} from "../credentials.ts";

export type ToolDescriptorDTO = components["schemas"]["ToolDescriptorDTO"];
export type ToolInvocation = components["schemas"]["ToolInvocation"];
export type ToolResult = components["schemas"]["ToolResult"];
export type ToolDescriptor = {
  name: string;
  description: string;
  inputSchema: Record<string, unknown>;
};
/** Opaque local handles only; tokens never travel through argv or config. */
export type McpSession = {
  apiOrigin: string;
  credentialHandle: string;
  stateDir: string;
};
export const REAUTH_MESSAGE =
  "NOUS authorization rejected; reconnect this device (nous-harness connect)";
export class ReauthenticationRequired extends Error {
  constructor() {
    super(REAUTH_MESSAGE);
  }
}
export class ToolRequestRejected extends Error {}

/** HTTPS only, except the explicit development loopback; no embedded credentials. */
export function apiBase(value: string): string {
  const url = new URL(value);
  if (
    url.username ||
    url.password ||
    url.search ||
    url.hash ||
    (url.protocol !== "https:" &&
      !(
        url.protocol === "http:" &&
        ["localhost", "127.0.0.1", "[::1]"].includes(url.hostname)
      ))
  )
    throw new Error(
      "API requires HTTPS (or local loopback) without credentials",
    );
  return value.replace(/\/$/, "");
}

/** Scoped HTTPS facade over the backend read gateway. Reads only; never retries. */
export class CapabilityClient {
  private readonly base: string;
  private readonly headers: Record<string, string>;
  private readonly fetchFn: typeof fetch;
  constructor(
    apiOrigin: string,
    credentials: IntegrationCredentials,
    fetchFn: typeof fetch = fetch,
  ) {
    this.base = apiBase(apiOrigin);
    this.headers = integrationHeaders(credentials);
    this.fetchFn = fetchFn;
  }
  async listTools(): Promise<ToolDescriptor[]> {
    const data = await this.request("/integrations/tools");
    if (!Array.isArray(data)) throw new Error("invalid NOUS tool catalog");
    return (data as ToolDescriptorDTO[]).map((tool) => ({
      name: tool.name,
      description: tool.description,
      inputSchema: tool.input_schema,
    }));
  }
  async invokeRead(invocation: ToolInvocation): Promise<ToolResult> {
    const data = await this.request("/integrations/tools/read", {
      tool_name: invocation.tool_name,
      arguments: invocation.arguments ?? {},
      invocation_id: invocation.invocation_id,
    });
    if (
      typeof data !== "object" ||
      data === null ||
      !Array.isArray((data as ToolResult).content) ||
      !Array.isArray((data as ToolResult).source_refs) ||
      typeof (data as ToolResult).is_error !== "boolean"
    )
      throw new Error("invalid NOUS tool result");
    return data as ToolResult;
  }
  private async request(path: string, body?: object): Promise<unknown> {
    const response = await this.fetchFn(this.base + path, {
      method: body ? "POST" : "GET",
      redirect: "error",
      headers: {
        ...this.headers,
        ...(body ? { "Content-Type": "application/json" } : {}),
      },
      ...(body ? { body: JSON.stringify(body) } : {}),
      signal: AbortSignal.timeout(30_000),
    });
    if (response.status === 401 || response.status === 403)
      throw new ReauthenticationRequired();
    if (response.status === 422)
      throw new ToolRequestRejected("NOUS rejected the tool arguments");
    if (response.status === 503)
      throw new ToolRequestRejected("NOUS integration tools are disabled");
    if (!response.ok)
      throw new Error(`NOUS request failed (${response.status})`);
    try {
      return await response.json();
    } catch {
      throw new Error("invalid NOUS response");
    }
  }
}
