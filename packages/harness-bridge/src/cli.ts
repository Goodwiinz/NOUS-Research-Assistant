#!/usr/bin/env node
import { Journal } from "./journal.ts";
import { connectBridge } from "./connection.ts";
import { CodexAdapter } from "./adapters/codex.ts";
import { homedir } from "node:os";
import { join, basename, resolve } from "node:path";
import { realpath, stat } from "node:fs/promises";
import { randomUUID } from "node:crypto";
import { pathToFileURL } from "node:url";
import { parseArgs } from "node:util";
import { CredentialStore } from "./credentials.ts";
import { GrantExpired, GrantKeeper } from "./grants.ts";
import { record } from "./rpc.ts";
import { apiBase, type McpSession } from "./mcp/client.ts";
import {
  buildManagedMcpConfig,
  standaloneInstallCommand,
} from "./mcp/config.ts";
import { runStdioMcp } from "./mcp/stdio.ts";
import type { SessionOptions } from "./contracts.ts";

export type LocalState = {
  apiUrl: string;
  deviceId: string;
  projectId: string;
  // The NOUS chat standalone publications land in; absent = project only.
  threadId?: string;
  credentialHandle: string;
  // Absent on connections made before tools:read existed.
  scopes?: string[];
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
  options: ClientOptions & {
    apiUrl: string;
    projectId: string;
    threadId?: string;
    label: string;
    tools?: boolean;
    publish?: boolean;
    write?: boolean;
  },
): Promise<{ deviceId: string; credentialHandle: string }> {
  const base = apiBase(options.apiUrl);
  if (options.threadId !== undefined) {
    if (!options.projectId) throw new Error("--chat requires --project");
    if (!uuid(options.threadId)) throw new Error("--chat must be a chat UUID");
  }
  if (!uuid(options.projectId) || !options.label.trim())
    throw new Error("project UUID and device label required");
  // NOUS capabilities are opt-in and each scope is shown on the consent page.
  if (options.publish && !options.tools)
    throw new Error("--publish requires --tools");
  if (options.write && !options.tools)
    throw new Error("--write requires --tools");
  const scopes = [
    "harness:execute",
    ...(options.tools ? ["tools:read"] : []),
    ...(options.publish ? ["artifacts:publish"] : []),
    ...(options.write ? ["tools:write"] : []),
  ];
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
      scopes,
      ...(options.threadId ? { thread_id: options.threadId } : {}),
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
    ...(uuid(grant.grant_id)
      ? { grantId: grant.grant_id, renewedAt: Date.now() }
      : {}),
  });
  // A reconnect (e.g. to add --publish) must not drop registered folders:
  // re-register each root with the new device so managed runs and
  // standalone installs keep working. Roots that vanished are skipped.
  const previous = await store.readLocal("connection").catch(() => null);
  const carried: LocalState["workspaces"] = [];
  if (record(previous) && Array.isArray(previous.workspaces)) {
    for (const old of previous.workspaces as LocalState["workspaces"]) {
      if (typeof old?.root !== "string" || typeof old?.label !== "string") continue;
      const root = await realpath(old.root).catch(() => null);
      if (root === null) {
        announce(`Skipped vanished workspace root ${old.root}; register it again if needed.`);
        continue;
      }
      const workspaceId = randomUUID();
      await request(
        fetchFn,
        base,
        `/integrations/devices/${device.id}/workspaces`,
        auth.token,
        { workspace_id: workspaceId, label: old.label, project_id: options.projectId },
      );
      carried.push({ id: workspaceId, root, label: old.label, projectId: options.projectId });
    }
  }
  await store.writeLocal("connection", {
    apiUrl: base,
    deviceId: device.id,
    projectId: options.projectId,
    ...(options.threadId ? { threadId: options.threadId } : {}),
    credentialHandle,
    scopes,
    workspaces: carried,
  } satisfies LocalState);
  if (carried.length)
    announce(`Re-registered ${carried.length} workspace root(s) on the new device.`);
  const chatLabel =
        typeof consent.thread_label === "string"
      ? `${consent.thread_label.replace(/[\p{Cc}\p{Cf}\u2028\u2029]/gu, "")} `
      : "";
  announce(
    options.threadId
      ? `Connected to project ${options.projectId}, chat ${chatLabel}(${options.threadId}).`
      : `Connected to project ${options.projectId}.`,
  );
  return { deviceId: device.id, credentialHandle };
}
/**
 * Revoke this device's grant (and its consent) in NOUS, then forget the local
 * credentials. A grant NOUS already refuses is cleared locally with a pointer
 * to the browser page; a network or server failure keeps everything so the
 * command can be retried.
 */
export async function disconnect(
  options: ClientOptions,
): Promise<{ revoked: boolean }> {
  const store = new CredentialStore(options.stateDir);
  const value = await store.readLocal("connection").catch(() => null);
  if (!record(value) || typeof value.apiUrl !== "string" || typeof value.credentialHandle !== "string")
    throw new Error("this device is not connected");
  const state = value as LocalState;
  const base = apiBase(state.apiUrl);
  const announce = options.announce ?? console.log;
  const fetchFn = options.fetchFn ?? fetch;
  const forget = async (): Promise<void> => {
    await store.removeLocal(state.credentialHandle);
    await store.removeLocal("connection");
  };
  let credentials;
  try {
    credentials = await new GrantKeeper(store, state.credentialHandle, base, fetchFn).current();
  } catch (error) {
    if (!(error instanceof GrantExpired)) throw error;
    await forget();
    announce(`This device's access had already ended; local credentials removed. Review devices in NOUS at /integrations/devices.`);
    return { revoked: false };
  }
  if (!uuid(credentials.grantId)) {
    await forget();
    announce("This connection predates grant renewal and cannot revoke itself; local credentials removed. Revoke it in NOUS at /integrations/devices.");
    return { revoked: false };
  }
  let response: Response;
  try {
    response = await fetchFn(`${base}/integrations/grants/${credentials.grantId}`, {
      method: "DELETE",
      redirect: "error",
      headers: {
        Authorization: `Bearer ${credentials.accessToken}`,
        "X-NOUS-Integration-Grant": credentials.grantToken,
      },
      signal: AbortSignal.timeout(30_000),
    });
  } catch (error) {
    throw new Error(`could not reach NOUS to revoke this device; nothing was removed (${error instanceof Error ? error.message : String(error)})`);
  }
  if (response.status === 204) {
    await forget();
    announce("Disconnected: NOUS revoked this device's access and local credentials were removed.");
    return { revoked: true };
  }
  if (response.status === 401 || response.status === 403 || response.status === 404) {
    await forget();
    announce("NOUS no longer accepts this device's grant; local credentials removed. Review devices in NOUS at /integrations/devices.");
    return { revoked: false };
  }
  throw new Error(`NOUS could not revoke this device (${response.status}); nothing was removed, try again`);
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
export async function runBridge(
  stateDir: string,
  signal: AbortSignal,
): Promise<void> {
  const store = new CredentialStore(stateDir);
  const value = await store.readLocal("connection");
  if (
    !record(value) ||
    !uuid(value.deviceId) ||
    !Array.isArray(value.workspaces) ||
    typeof value.apiUrl !== "string" ||
    typeof value.credentialHandle !== "string"
  )
    throw new Error("connect this device first");
  const state = value as LocalState;
  const adapters = new Map<string, CodexAdapter>();
  if (!state.scopes?.includes("tools:read"))
    console.error(
      "NOUS tools are not enabled for managed sessions; reconnect with `nous-harness connect --tools` to expose them.",
    );
  const journal = new Journal(join(stateDir, "journal.sqlite"), (workspaceId) =>
    sessionOptionsFor(stateDir, state, workspaceId),
  );
  const adapterFor = (_workspaceId: string, runId: string): CodexAdapter => {
    let adapter = adapters.get(runId);
    if (!adapter) {
      adapter = new CodexAdapter();
      adapters.set(runId, adapter);
    }
    return adapter;
  };
  const url = new URL(apiBase(state.apiUrl) + "/harness/connect");
  url.protocol = "wss:";
  // The socket re-checks the grant on every frame; keep it renewed before the
  // 15-minute expiry. A renewal revokes the old token, so the open socket is
  // closed on its next frame and the loop below reconnects with the new one.
  const keeper = new GrantKeeper(store, state.credentialHandle, apiBase(state.apiUrl));
  const stopRenewal = keeper.keepFresh((error) =>
    console.error(error instanceof Error ? error.message : String(error)),
  );
  try {
    while (!signal.aborted) {
      // Local native evidence and the expiry watchdog must work even while the
      // network is unavailable or authentication cannot open a new connection.
      for (const c of journal.activeCommands()) {
        if (journal.state(c.commandId) === "recovering")
          try {
            await journal.reconcile(
              c.commandId,
              adapterFor(c.workspaceId, c.runId),
            );
          } catch {
            journal.quarantine(c.commandId);
          }
      }
      try {
        await connectBridge({
          url: url.href,
          deviceId: state.deviceId,
          credentials: await keeper.current(),
          journal,
          signal,
          adapterFor,
        });
      } catch {
        if (!signal.aborted)
          console.error(
            "Bridge disconnected; preserving journal and workspace ownership.",
          );
      }
      if (!signal.aborted)
        await new Promise<void>((resolve) => {
          const done = () => {
            clearTimeout(timer);
            signal.removeEventListener("abort", done);
            resolve();
          };
          const timer = setTimeout(done, 5000);
          signal.addEventListener("abort", done, { once: true });
        });
    }
  } finally {
    stopRenewal();
    // No local unlock on shutdown: app-server children may outlive this process.
    for (const adapter of adapters.values()) await adapter.closeSession();
    journal.close();
  }
}
function mcpSession(
  stateDir: string,
  state: LocalState,
  outputRoot?: string,
): McpSession {
  return {
    apiOrigin: apiBase(state.apiUrl),
    credentialHandle: state.credentialHandle,
    // Codex launches the MCP child from the workspace cwd, never from here.
    stateDir: resolve(stateDir),
    // Publication is bound to a registered root only; never to the cwd.
    ...(outputRoot && state.scopes?.includes("artifacts:publish")
      ? { outputRoot }
      : {}),
    ...(state.scopes?.includes("tools:write") ? { actions: true } : {}),
  };
}
/** Managed sessions get the NOUS MCP server only when the grant carries tools:read. */
export function sessionOptionsFor(
  stateDir: string,
  state: LocalState,
  workspaceId: string,
): SessionOptions {
  const workspace = state.workspaces.find((w) => w.id === workspaceId);
  if (!workspace) throw new Error("unregistered local workspace");
  return {
    cwd: workspace.root,
    workspaceId,
    policy: {
      sandbox: "workspace-write",
      approvalPolicy: "on-request",
      reviewer: "user",
      networkAccess: false,
      writableRoots: [workspace.root],
    },
    ...(state.scopes?.includes("tools:read")
      ? {
          mcpConfig: buildManagedMcpConfig(
            mcpSession(stateDir, state, workspace.root),
          ),
        }
      : {}),
  };
}
/** Prints the standalone Codex registration; never edits global Codex config. */
export async function mcpInstallCommand(
  stateDir: string,
  options: { root?: string; announce?: (message: string) => void } = {},
): Promise<string> {
  const value = await new CredentialStore(stateDir).readLocal("connection");
  if (
    !record(value) ||
    typeof value.apiUrl !== "string" ||
    typeof value.credentialHandle !== "string"
  )
    throw new Error("connect this device first");
  const state = value as LocalState;
  if (!state.scopes?.includes("tools:read"))
    throw new Error("reconnect with --tools to authorize NOUS tools");
  // Publication binds one registered root, chosen explicitly when ambiguous.
  let root: string | undefined;
  if (!state.scopes.includes("artifacts:publish") && options.root !== undefined)
    throw new Error(
      "--root requires artifacts:publish; reconnect with nous-harness connect --tools --publish",
    );
  if (state.scopes.includes("artifacts:publish")) {
    const roots = state.workspaces.map((w) => w.root);
    if (options.root !== undefined) {
      // Registered roots are stored realpath'd (e.g. /tmp -> /private/tmp).
      const chosen = await realpath(options.root).catch(() => resolve(options.root!));
      if (!roots.includes(chosen))
        throw new Error("--root must name a registered workspace root");
      root = chosen;
    } else if (roots.length === 1) root = roots[0];
    else if (roots.length > 1)
      throw new Error(
        `several workspaces are registered; pass --root with one of: ${roots.join(", ")}`,
      );
    (options.announce ?? console.error)(
      root
        ? `artifacts_publish will publish from ${root}`
        : "no workspace registered; artifacts_publish will not be offered",
    );
  }
  return standaloneInstallCommand(mcpSession(stateDir, state, root));
}
export function recoverInterrupt(
  stateDir: string,
  commandId?: string,
): string[] {
  if (commandId !== undefined && !uuid(commandId))
    throw new Error("interrupt command UUID required");
  const journal = new Journal(
    join(stateDir, "journal.sqlite"),
    () => {
      throw new Error("recovery cannot start native work");
    },
    { recoveryOnly: true },
  );
  try {
    if (commandId) journal.recoverInterrupt(commandId);
    return journal.uncertainInterrupts();
  } finally {
    journal.close();
  }
}
const help = `Usage: nous-harness connect --api https://host/api/v1 --project UUID --label NAME [--chat UUID] [--tools [--publish] [--write]] | workspace add --root PATH [--label NAME] | run
  connect --chat UUID    Bind this device to one NOUS chat in --project: harness runs are leased and files are published only in that chat; if the chat is deleted or moved, reconnect. Reconnect without --chat to unbind.
  nous-harness disconnect    Revoke this device's NOUS access and remove its local credentials.
  nous-harness mcp install [--root PATH]    Print the Codex command that registers NOUS tools for a --tools connection; --root picks the publish folder.
  nous-harness mcp --api URL --session HANDLE [--store PATH] [--root PATH] [--actions]    Serve NOUS tools over stdio (Codex launches this); --root enables artifacts_publish, --actions enables request_action.
  nous-harness recover-interrupt [--command UUID] [--store PATH]
List uncertain interrupt IDs, or recover exactly one after a verified reboot on the same machine.
Stop the bridge, run recovery once to record any missing legacy boot baseline, wait at least ten seconds, and reboot this machine.
After reboot, rerun with --command UUID, then restart the bridge for reconciliation.
Same boot, another machine, or unavailable OS evidence refuses recovery. Recovery keeps workspace ownership and run state unchanged.
Do not edit the journal database; unsupported platforms or unreadable OS identity require restoring OS evidence before retrying.`;
async function main(): Promise<void> {
  const { positionals, values } = parseArgs({
    allowPositionals: true,
    options: {
      api: { type: "string" },
      project: { type: "string" },
      chat: { type: "string" },
      label: { type: "string" },
      store: { type: "string" },
      root: { type: "string" },
      command: { type: "string" },
      session: { type: "string" },
      tools: { type: "boolean" },
      publish: { type: "boolean" },
      write: { type: "boolean" },
      actions: { type: "boolean" },
      help: { type: "boolean" },
    },
  });
  const stateDir = values.store ?? join(homedir(), ".nous", "harness-bridge");
  if (values.help) {
    console.log(help);
    return;
  }
  if (positionals.join(" ") === "recover-interrupt") {
    const remaining = recoverInterrupt(stateDir, values.command);
    if (values.command)
      console.log(
        "Interrupt delivery blocker cleared; restart the bridge for reconciliation. Workspace ownership and run state retained.",
      );
    console.log(
      remaining.length ? remaining.join("\n") : "No uncertain interrupts.",
    );
  } else if (positionals.join(" ") === "connect" && values.chat && !values.project)
    throw new Error("--chat requires --project");
  else if (
    positionals.join(" ") === "connect" &&
    values.api &&
    values.project &&
    values.label
  )
    await connect({
      stateDir,
      apiUrl: values.api,
      projectId: values.project,
      threadId: values.chat,
      label: values.label,
      tools: values.tools,
      publish: values.publish,
      write: values.write,
    });
  else if (positionals.join(" ") === "disconnect")
    await disconnect({ stateDir });
  else if (positionals.join(" ") === "mcp install")
    console.log(await mcpInstallCommand(stateDir, { root: values.root }));
  else if (positionals.join(" ") === "mcp") {
    if (!values.session || !values.api)
      throw new Error("mcp requires --api and --session");
    await runStdioMcp({
      apiOrigin: apiBase(values.api),
      credentialHandle: values.session,
      stateDir: resolve(stateDir),
      ...(values.root ? { outputRoot: resolve(values.root) } : {}),
      ...(values.actions ? { actions: true } : {}),
    });
  } else if (positionals.join(" ") === "workspace add" && values.root)
    await addWorkspace({ stateDir, root: values.root, label: values.label });
  else if (positionals.join(" ") === "run") {
    const controller = new AbortController();
    process.once("SIGINT", () => controller.abort());
    process.once("SIGTERM", () => controller.abort());
    await runBridge(stateDir, controller.signal);
  } else throw new Error(help);
}
if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href)
  void main().catch((error) => {
    console.error(
      error instanceof Error ? error.message : "Bridge command failed",
    );
    process.exitCode = 1;
  });
