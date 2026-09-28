#!/usr/bin/env node
import { homedir } from "node:os";
import { join, basename } from "node:path";
import { realpath, stat } from "node:fs/promises";
import { randomUUID } from "node:crypto";
import { pathToFileURL } from "node:url";
import { parseArgs } from "node:util";
import { CredentialStore } from "./credentials.ts";
import { record } from "./rpc.ts";

type LocalState = {
  apiUrl: string;
  deviceId: string;
  projectId: string;
  credentialHandle: string;
  workspaces: { id: string; root: string; label: string; projectId: string }[];
};
type ClientOptions = {
  stateDir: string;
  fetchFn?: typeof fetch;
  announce?: (message: string) => void;
  delay?: () => Promise<void>;
};
const uuid = (value: unknown): value is string =>
  typeof value === "string" &&
  /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i.test(value);
function apiBase(value: string): string {
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
async function request(
  fetchFn: typeof fetch,
  base: string,
  path: string,
  token?: string,
  body?: object,
): Promise<Record<string, any>> {
  const response = await fetchFn(base + path, {
    method: body ? "POST" : "GET",
    redirect: "error",
    headers: {
      ...(token ? { Authorization: `Bearer ${token}` } : {}),
      ...(body ? { "Content-Type": "application/json" } : {}),
    },
    ...(body ? { body: JSON.stringify(body) } : {}),
    signal: AbortSignal.timeout(15_000),
  });
  if (!response.ok) throw new Error(`NOUS request failed (${response.status})`);
  let data: unknown;
  try {
    data = await response.json();
  } catch {
    throw new Error("invalid NOUS response");
  }
  if (!record(data)) throw new Error("invalid NOUS response");
  return data;
}
async function poll(
  read: () => Promise<Record<string, any>>,
  options: ClientOptions,
): Promise<Record<string, any>> {
  const deadline = Date.now() + 10 * 60_000;
  while (Date.now() < deadline) {
    const result = await read();
    if (result.status === "approved") return result;
    if (result.status !== "pending")
      throw new Error("authorization denied or expired");
    await (options.delay?.() ??
      new Promise((resolve) => setTimeout(resolve, 2000)));
  }
  throw new Error("authorization expired");
}
/** Login uses the existing CLI device flow; only the browser can approve the grant. */
export async function connect(
  options: ClientOptions & { apiUrl: string; projectId: string; label: string },
): Promise<{ deviceId: string; credentialHandle: string }> {
  const base = apiBase(options.apiUrl);
  if (!uuid(options.projectId) || !options.label.trim())
    throw new Error("project UUID and device label required");
  const fetchFn = options.fetchFn ?? fetch;
  const announce = options.announce ?? console.log;
  const login = await request(fetchFn, base, "/cli-auth/start", undefined, {});
  if (
    typeof login.session_id !== "string" ||
    typeof login.poll_token !== "string" ||
    typeof login.browser_url !== "string"
  )
    throw new Error("invalid CLI login response");
  announce(`Authorize CLI login in your browser: ${login.browser_url}`);
  const auth = await poll(
    () =>
      request(
        fetchFn,
        base,
        `/cli-auth/status/${encodeURIComponent(login.session_id)}?poll_token=${encodeURIComponent(login.poll_token)}`,
      ),
    options,
  );
  if (typeof auth.token !== "string" || !auth.token)
    throw new Error("missing CLI access token");
  const device = await request(
    fetchFn,
    base,
    "/integrations/devices",
    auth.token,
    { label: options.label },
  );
  if (!uuid(device.id)) throw new Error("invalid device ID");
  const consent = await request(
    fetchFn,
    base,
    "/integrations/grant-requests",
    auth.token,
    {
      project_id: options.projectId,
      device_id: device.id,
      scopes: ["harness:execute"],
    },
  );
  if (!uuid(consent.id) || typeof consent.approval_url !== "string")
    throw new Error("invalid grant request");
  announce(
    `Approve this device and project in your browser: ${consent.approval_url}`,
  );
  await poll(
    () =>
      request(
        fetchFn,
        base,
        `/integrations/grant-requests/${consent.id}`,
        auth.token,
      ),
    options,
  );
  const grant = await request(
    fetchFn,
    base,
    `/integrations/grant-requests/${consent.id}/exchange`,
    auth.token,
    {},
  );
  const store = new CredentialStore(options.stateDir);
  const credentialHandle = await store.save({
    accessToken: auth.token,
    grantToken: grant.token,
  });
  await store.writeLocal("connection", {
    apiUrl: base,
    deviceId: device.id,
    projectId: options.projectId,
    credentialHandle,
    workspaces: [],
  } satisfies LocalState);
  return { deviceId: device.id, credentialHandle };
}
export async function addWorkspace(
  options: ClientOptions & { root: string; label?: string },
): Promise<{ workspaceId: string; root: string }> {
  const store = new CredentialStore(options.stateDir);
  const value = await store.readLocal("connection");
  if (
    !record(value) ||
    !uuid(value.deviceId) ||
    !uuid(value.projectId) ||
    typeof value.apiUrl !== "string" ||
    !Array.isArray(value.workspaces) ||
    typeof value.credentialHandle !== "string"
  )
    throw new Error("connect this device first");
  const state = value as LocalState;
  const root = await realpath(options.root);
  if (!(await stat(root)).isDirectory())
    throw new Error("workspace must be a directory");
  const existing = state.workspaces.find((w) => w.root === root);
  if (existing) return { workspaceId: existing.id, root };
  const credentials = await store.load(state.credentialHandle);
  const workspaceId = randomUUID();
  const label = options.label ?? basename(root);
  await request(
    options.fetchFn ?? fetch,
    apiBase(state.apiUrl),
    `/integrations/devices/${state.deviceId}/workspaces`,
    credentials.accessToken,
    { workspace_id: workspaceId, label, project_id: state.projectId },
  );
  state.workspaces.push({
    id: workspaceId,
    root,
    label,
    projectId: state.projectId,
  });
  await store.writeLocal("connection", state);
  (options.announce ?? console.log)(
    "Registered local write/output root. Folder registration does not isolate reads of other local files.",
  );
  return { workspaceId, root };
}
async function main(): Promise<void> {
  const { positionals, values } = parseArgs({
    allowPositionals: true,
    options: {
      api: { type: "string" },
      project: { type: "string" },
      label: { type: "string" },
      store: { type: "string" },
      root: { type: "string" },
    },
  });
  const stateDir = values.store ?? join(homedir(), ".nous", "harness-bridge");
  if (
    positionals.join(" ") === "connect" &&
    values.api &&
    values.project &&
    values.label
  )
    await connect({
      stateDir,
      apiUrl: values.api,
      projectId: values.project,
      label: values.label,
    });
  else if (positionals.join(" ") === "workspace add" && values.root)
    await addWorkspace({ stateDir, root: values.root, label: values.label });
  else
    throw new Error(
      "Usage: nous-harness connect --api https://host/api/v1 --project UUID --label NAME | workspace add --root PATH [--label NAME]",
    );
}
if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href)
  void main().catch((error) => {
    console.error(
      error instanceof Error ? error.message : "Bridge command failed",
    );
    process.exitCode = 1;
  });
