#!/usr/bin/env node
import { Journal } from "./journal.ts";
import { connectBridge } from "./connection.ts";
import { CodexAdapter } from "./adapters/codex.ts";
import { homedir, hostname } from "node:os";
import {
  join,
  basename,
  dirname,
  isAbsolute,
  relative,
  resolve,
  sep,
} from "node:path";
import { readFileSync, unlinkSync } from "node:fs";
import { mkdir, open, readFile, realpath, stat, unlink } from "node:fs/promises";
import { randomUUID } from "node:crypto";
import { pathToFileURL } from "node:url";
import { parseArgs, stripVTControlCharacters } from "node:util";
import { createInterface } from "node:readline/promises";
import { CredentialStore } from "./credentials.ts";
import { GRANT_LIFETIME_MS, GrantExpired, GrantKeeper } from "./grants.ts";
import { HandoffHttpClient, type Handoff } from "./handoffs/client.ts";
import { handoffPayload } from "./handoffs/mcp.ts";
import {
  HandoffQueue,
  queueEntries,
  type Attempt,
  type Binding,
  type FlushResult,
  type QueueEntry,
} from "./handoffs/queue.ts";
import { record } from "./rpc.ts";
import { apiBase, MCP_TOOL_TIMEOUT_SEC, type McpSession } from "./mcp/client.ts";
import {
  buildManagedMcpConfig,
  standaloneInstallCommand,
} from "./mcp/config.ts";
import { runStdioMcp } from "./mcp/stdio.ts";
import type { HarnessAdapter, SessionOptions } from "./contracts.ts";

// Harness sessions need one project; a workspace grant cannot hold harness:execute.
const WORKSPACE_MCP_ONLY =
  "workspace connections are MCP-only; reconnect with --project to run harness sessions";

export type LocalState = {
  apiUrl: string;
  deviceId: string;
  // Exactly one binding. A project connection can run harness sessions; a
  // workspace connection (every project in one NOUS workspace) is MCP-only.
  projectId?: string;
  workspaceId?: string;
  // The NOUS chat standalone publications land in; absent = project only.
  threadId?: string;
  // Absent on connections made before `status` existed.
  label?: string;
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
  headers: Record<string, string> = {},
): Promise<Record<string, any>> {
  const response = await fetchFn(base + path, {
    method: body ? "POST" : "GET",
    redirect: "error",
    headers: {
      ...headers,
      ...(token ? { Authorization: `Bearer ${token}` } : {}),
      ...(body ? { "Content-Type": "application/json" } : {}),
    },
    ...(body ? { body: JSON.stringify(body) } : {}),
    signal: AbortSignal.timeout(15_000),
  });
  if (!response.ok)
    throw new Error(
      `NOUS request failed (${response.status}) ${path.split("?")[0]}`,
    );
  let data: unknown;
  try {
    data = await response.json();
  } catch {
    throw new Error("invalid NOUS response");
  }
  if (!record(data)) throw new Error("invalid NOUS response");
  return data;
}
/**
 * A server-supplied link as the terminal prints it: http(s) only, and
 * re-serialised so control characters arrive percent-encoded, never raw.
 */
function printableUrl(value: string): string {
  let url: URL;
  try {
    url = new URL(value);
  } catch {
    throw new Error("invalid NOUS response");
  }
  if (url.protocol !== "http:" && url.protocol !== "https:")
    throw new Error("invalid NOUS response");
  return url.href;
}
/** A stored connection with the fields the reuse check and `status` read. */
function storedConnection(value: unknown): LocalState | null {
  if (
    !record(value) ||
    typeof value.apiUrl !== "string" ||
    !uuid(value.deviceId) ||
    // Exactly one binding: a project, or a workspace (MCP-only).
    uuid(value.projectId) === uuid(value.workspaceId) ||
    typeof value.credentialHandle !== "string" ||
    !Array.isArray(value.workspaces)
  )
    return null;
  return value as LocalState;
}
/**
 * Liveness probe for binding reuse: a forced renewal that must issue a new
 * grant token. A refusal, a transient failure (the keeper keeps the old
 * token) or a connection without a stored grant ID all mean "not proven".
 */
async function grantIsLive(
  store: CredentialStore,
  handle: string,
  base: string,
  fetchFn: typeof fetch,
): Promise<boolean> {
  try {
    const before = await store.load(handle);
    if (!uuid(before.grantId)) return false;
    const after = await new GrantKeeper(store, handle, base, fetchFn).current(0);
    return after.grantToken !== before.grantToken;
  } catch {
    return false;
  }
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
/**
 * Browser CLI login: start a session, print the link and the code the user
 * types on that page, poll until approved. Returns the CLI access token.
 */
export async function login(base: string, options: ClientOptions): Promise<string> {
  const api = apiBase(base);
  const fetchFn = options.fetchFn ?? fetch;
  const announce = options.announce ?? console.log;
  const started = await request(fetchFn, api, "/cli-auth/start", undefined, {});
  if (
    typeof started.session_id !== "string" ||
    typeof started.poll_token !== "string" ||
    typeof started.browser_url !== "string"
  )
    throw new Error("invalid CLI login response");
  // GOO-403: the link no longer carries the code; the user must type it on the
  // approval page, so a login response without a printable code is unusable.
  // The format check also keeps server-supplied text from injecting terminal
  // control sequences.
  if (
    typeof started.verification_code !== "string" ||
    !/^[A-Z0-9]{4}-[A-Z0-9]{4}$/.test(started.verification_code)
  )
    throw new Error("invalid CLI login response");
  const browserUrl = printableUrl(started.browser_url);
  announce(
    `Authorize CLI login in your browser: ${browserUrl}\nType this code on that page: ${started.verification_code}`,
  );
  const auth = await poll(
    () =>
      request(
        fetchFn,
        api,
        `/cli-auth/status/${encodeURIComponent(started.session_id)}`,
        undefined,
        undefined,
        { "X-CLI-Poll-Token": started.poll_token },
      ),
    options,
  );
  if (typeof auth.token !== "string" || !auth.token)
    throw new Error("missing CLI access token");
  return auth.token;
}
/** Login uses the existing CLI device flow; only the browser can approve the grant. */
export async function connect(
  options: ClientOptions & {
    apiUrl: string;
    // Exactly one of projectId / workspaceId.
    projectId?: string;
    workspaceId?: string;
    threadId?: string;
    label: string;
    tools?: boolean;
    publish?: boolean;
    write?: boolean;
    handoff?: boolean;
    library?: boolean;
    context?: boolean;
    // A CLI access token obtained by login(); when set, no browser login runs;
    // ignored when the stored binding is reused.
    accessToken?: string;
  },
): Promise<{ deviceId: string; credentialHandle: string }> {
  const base = apiBase(options.apiUrl);
  const projectId = options.projectId;
  if (options.threadId !== undefined) {
    if (!options.projectId) throw new Error("--chat requires --project");
    if (!uuid(options.threadId)) throw new Error("--chat must be a chat UUID");
  }
  if ((projectId === undefined) === (options.workspaceId === undefined))
    throw new Error("--project or --workspace (not both) required");
  const target = projectId !== undefined ? "project" : "workspace";
  if (!uuid(projectId ?? options.workspaceId) || !options.label.trim())
    throw new Error(`${target} UUID and device label required`);
  // NOUS capabilities are opt-in and each scope is shown on the consent page.
  if (options.publish && !options.tools)
    throw new Error("--publish requires --tools");
  if (options.write && !options.tools)
    throw new Error("--write requires --tools");
  if (options.handoff && !options.tools)
    throw new Error("--handoff requires --tools");
  // A handoff lives in one chat of one project; a workspace grant has neither.
  if (options.handoff && projectId === undefined)
    throw new Error("--workspace cannot be combined with --handoff");
  // A handoff lives in one chat; an unbound grant could never use the scopes.
  if (options.handoff && options.threadId === undefined)
    throw new Error("--handoff requires --chat");
  if (options.library && !options.tools)
    throw new Error("--library requires --tools");
  if (options.library && !options.write)
    throw new Error("--library requires --write");
  if (options.context && !options.tools)
    throw new Error("--context requires --tools");
  // A workspace grant is MCP-only: harness runs, artifact publication and a
  // project's selected memories stay bound to one project (NOUS refuses all
  // three scopes for a workspace grant), and without tools:read it could do
  // nothing at all.
  if (projectId === undefined) {
    if (options.publish)
      throw new Error("--workspace cannot be combined with --publish");
    if (options.context)
      throw new Error("--workspace cannot be combined with --context");
    if (!options.tools) throw new Error("--workspace requires --tools");
  }
  const scopes = [
    ...(projectId !== undefined ? ["harness:execute"] : []),
    ...(options.tools ? ["tools:read"] : []),
    ...(options.publish ? ["artifacts:publish"] : []),
    ...(options.write ? ["tools:write"] : []),
    ...(options.handoff ? ["handoff:read", "handoff:write"] : []),
    // A workspace grant's project tools all need a project_id, and only
    // list_library (library:read) names them (WG-3). library:write, which
    // auto-runs library changes, still needs --library.
    ...(options.library || projectId === undefined ? ["library:read"] : []),
    ...(options.library ? ["library:write"] : []),
    ...(options.context ? ["context:read"] : []),
  ];
  const fetchFn = options.fetchFn ?? fetch;
  const announce = options.announce ?? console.log;
  const store = new CredentialStore(options.stateDir);
  const previous = storedConnection(await store.readLocal("connection").catch(() => null));
  let sameApi = false;
  try {
    sameApi = previous !== null && apiBase(previous.apiUrl) === base;
  } catch {
    // An unparsable stored URL is simply a different binding.
  }
  const sameBinding =
    previous !== null &&
    sameApi &&
    previous.projectId === options.projectId &&
    previous.workspaceId === options.workspaceId &&
    (previous.threadId ?? undefined) === (options.threadId ?? undefined);
  // Same API, project and chat, every requested scope already granted, and the
  // grant still renews: keep it instead of a new browser login and consent.
  if (
    previous !== null &&
    sameBinding &&
    scopes.every((scope) => previous.scopes?.includes(scope)) &&
    (await grantIsLive(store, previous.credentialHandle, base, fetchFn))
  ) {
    // Reuse never narrows a grant (the documented superset rule): name any
    // stored scope this command did not ask for, and how to drop it (BR-3).
    const retained = (previous.scopes ?? []).filter((scope) => !scopes.includes(scope));
    announce(
      `Reusing binding: ${target} ${projectId ?? options.workspaceId}${options.threadId ? `, chat ${options.threadId}` : ""}; no new login or consent needed.` +
        (retained.length
          ? ` It keeps scopes this command did not request: ${retained.join(", ")}. To drop them (which also ${
              // Only a project connection runs Codex on the device and has
              // registered folders to lose; a workspace one only serves tools.
              projectId !== undefined
                ? "stops the device's runs"
                : "cuts this device's NOUS tools until it reconnects"
            }), revoke this device's access at /integrations/devices, or run nous-harness disconnect, then connect again` +
            (projectId !== undefined
              ? "; after disconnect, register its folders again with nous-harness workspace add."
              : ".")
          : ""),
    );
    return { deviceId: previous.deviceId, credentialHandle: previous.credentialHandle };
  }
  const token = options.accessToken ?? (await login(base, options));
  const device = await request(
    fetchFn,
    base,
    "/integrations/devices",
    token,
    { label: options.label },
  );
  if (!uuid(device.id)) throw new Error("invalid device ID");
  const consent = await request(
    fetchFn,
    base,
    "/integrations/grant-requests",
    token,
    {
      ...(projectId !== undefined
        ? { project_id: projectId }
        : { workspace_id: options.workspaceId }),
      device_id: device.id,
      scopes,
      ...(options.threadId ? { thread_id: options.threadId } : {}),
    },
  );
  if (!uuid(consent.id) || typeof consent.approval_url !== "string")
    throw new Error("invalid grant request");
  announce(
    `Approve this device and ${target} in your browser: ${printableUrl(consent.approval_url)}`,
  );
  await poll(
    () =>
      request(
        fetchFn,
        base,
        `/integrations/grant-requests/${consent.id}`,
        token,
      ),
    options,
  );
  const grant = await request(
    fetchFn,
    base,
    `/integrations/grant-requests/${consent.id}/exchange`,
    token,
    {},
  );
  const credentialHandle = await store.save({
    accessToken: token,
    grantToken: grant.token,
    ...(uuid(grant.grant_id)
      ? { grantId: grant.grant_id, renewedAt: Date.now() }
      : {}),
  });
  // A reconnect (e.g. to add --publish) must not drop registered folders:
  // re-register each root with the new device so managed runs and
  // standalone installs keep working. Roots that vanished are skipped.
  const carried: LocalState["workspaces"] = [];
  if (projectId !== undefined && previous !== null) {
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
        token,
        { workspace_id: workspaceId, label: old.label, project_id: projectId },
      );
      carried.push({ id: workspaceId, root, label: old.label, projectId });
    }
  } else if (previous !== null && previous.workspaces.length) {
    // Only reached for a workspace connection: registered folders are
    // project-bound write roots, so there is no project to carry them to.
    announce(
      "Registered local folders were not carried over: workspace connections are MCP-only. Register them again after reconnecting with --project.",
    );
  }
  await store.writeLocal("connection", {
    apiUrl: base,
    deviceId: device.id,
    ...(projectId !== undefined
      ? { projectId }
      : { workspaceId: options.workspaceId }),
    ...(options.threadId ? { threadId: options.threadId } : {}),
    label: options.label,
    credentialHandle,
    scopes,
    workspaces: carried,
  } satisfies LocalState);
  // Only after persisting the new connection can its superseded credential go,
  // including a full reconnect to the same binding after expiry/scope changes.
  // Its server-side grant and consent stay valid until `disconnect` or expiry.
  if (previous !== null && previous.credentialHandle !== credentialHandle)
    await store.removeLocal(previous.credentialHandle).catch((error: unknown) =>
      announce(
        `Could not remove the previous binding's local credential: ${error instanceof Error ? error.message : String(error)}`,
      ),
    );
  if (carried.length)
    announce(`Re-registered ${carried.length} workspace root(s) on the new device.`);
  const chatLabel =
        typeof consent.thread_label === "string"
      ? `${consent.thread_label.replace(/[\p{Cc}\p{Cf}\u2028\u2029]/gu, "")} `
      : "";
  announce(
    options.threadId
      ? `Connected to project ${projectId}, chat ${chatLabel}(${options.threadId}).`
      : `Connected to ${target} ${projectId ?? options.workspaceId}.`,
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
  // A registered folder is a write root bound to one project.
  if (record(value) && uuid(value.workspaceId))
    throw new Error(
      "workspace connections are MCP-only; reconnect with --project to register local folders",
    );
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
  const projectId = value.projectId;
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
    { workspace_id: workspaceId, label, project_id: projectId },
  );
  state.workspaces.push({
    id: workspaceId,
    root,
    label,
    projectId,
  });
  await store.writeLocal("connection", state);
  (options.announce ?? console.log)(
    "Registered local write/output root. Folder registration does not isolate reads of other local files.",
  );
  return { workspaceId, root };
}
/** One NOUS project as `rc` lists it. */
type RcProject = { id: string; name: string; documentCount?: number };
/** The `/projects` page `rc` shows; `more` when NOUS has projects beyond it. */
type RcProjectPage = { projects: RcProject[]; total?: number; more: boolean };
/** `rc` lists one page of projects. */
const PROJECT_PAGE = 50;
/**
 * Terminal-safe display name: NOUS project names are user data. Whole CSI
 * sequences go first, as a terminal reads them: stripVTControlCharacters alone
 * takes `ESC [31m name BEL` for one sequence and drops "name". It then removes
 * OSC and the other escape sequences, and the last pass any stray control or
 * format character.
 */
const plain = (value: string): string =>
  stripVTControlCharacters(
    value.replace(/\x1b\[[0-?]*[ -/]*[@-~]/g, ""),
  ).replace(/[\p{Cc}\p{Cf}\u2028\u2029]/gu, "");
/** A GET signed with the CLI token; request()'s redirect and time limits. */
function authorizedGet(
  fetchFn: typeof fetch,
  url: string,
  token: string,
): Promise<Response> {
  return fetchFn(url, {
    headers: { Authorization: `Bearer ${token}` },
    redirect: "error",
    signal: AbortSignal.timeout(15_000),
  });
}
/** A 1-based menu answer: digits only, since parseInt takes "1abc" for 1. */
function menuChoice(answer: string, count: number): number | null {
  const trimmed = answer.trim();
  if (!/^\d+$/.test(trimmed)) return null;
  const index = Number.parseInt(trimmed, 10);
  return index >= 1 && index <= count ? index - 1 : null;
}
/** The first `/projects` page, or null once NOUS rejects the CLI token. */
async function listProjects(
  fetchFn: typeof fetch,
  base: string,
  token: string,
): Promise<RcProjectPage | null> {
  const response = await authorizedGet(
    fetchFn,
    `${base}/projects?limit=${PROJECT_PAGE}`,
    token,
  );
  if (response.status === 401) return null;
  if (!response.ok)
    throw new Error(`NOUS request failed (${response.status}) /projects`);
  const data: unknown = await response.json().catch(() => null);
  if (!record(data) || !Array.isArray(data.projects))
    throw new Error("invalid NOUS response");
  const total = typeof data.total === "number" ? data.total : undefined;
  return {
    projects: data.projects.flatMap((p: unknown): RcProject[] =>
      record(p) && uuid(p.id) && typeof p.name === "string"
        ? [
            {
              id: p.id,
              name: plain(p.name),
              ...(typeof p.document_count === "number"
                ? { documentCount: p.document_count }
                : {}),
            },
          ]
        : [],
    ),
    ...(total !== undefined ? { total } : {}),
    more:
      data.has_next === true ||
      (total !== undefined && total > data.projects.length),
  };
}
/** Create a project, asking which workspace only when the user has several. */
async function createProject(
  fetchFn: typeof fetch,
  base: string,
  token: string,
  prompt: (question: string) => Promise<string>,
  announce: (message: string) => void,
): Promise<RcProject> {
  // Projects live in a NOUS workspace (not a local folder), listed by the v2
  // API beside v1; any other API path fails closed instead of guessing.
  if (!/\/api\/v1$/.test(base))
    throw new Error(`cannot derive the workspaces endpoint from ${base}`);
  const response = await authorizedGet(
    fetchFn,
    `${base.replace(/\/api\/v1$/, "/api/v2")}/workspaces`,
    token,
  );
  if (!response.ok)
    throw new Error(`NOUS request failed (${response.status}) /workspaces`);
  const list: unknown = await response.json().catch(() => null);
  if (!Array.isArray(list)) throw new Error("invalid NOUS response");
  const choices = list.flatMap((w: unknown) =>
    record(w) && uuid(w.id) && typeof w.name === "string"
      ? [{ id: w.id, name: plain(w.name) }]
      : [],
  );
  if (!choices.length)
    throw new Error("no NOUS workspace to create the project in");
  let name = "";
  while (!name) name = plain(await prompt("Project name: ")).trim();
  let workspace = choices[0];
  if (choices.length > 1) {
    announce(
      [
        "Workspaces:",
        ...choices.map((w, i) => `  ${i + 1}) ${w.name}`),
      ].join("\n"),
    );
    for (;;) {
      const index = menuChoice(
        await prompt(`Pick a workspace [1-${choices.length}]: `),
        choices.length,
      );
      if (index !== null) {
        workspace = choices[index];
        break;
      }
      announce("Not a listed choice.");
    }
  }
  const created = await request(fetchFn, base, "/projects", token, {
    workspace_id: workspace.id,
    name,
  });
  if (!uuid(created.id)) throw new Error("invalid NOUS response");
  return { id: created.id, name, documentCount: 0 };
}
/** Numbered picker: a listed project by number, or `n` for a new one. */
async function pickProject(
  page: RcProjectPage,
  prompt: (question: string) => Promise<string>,
  announce: (message: string) => void,
  create: () => Promise<RcProject>,
): Promise<RcProject> {
  const { projects } = page;
  announce(
    [
      "Your projects:",
      ...projects.map(
        (p, i) =>
          `  ${i + 1}) ${p.name}${p.documentCount !== undefined ? `  ${p.documentCount} docs` : ""}`,
      ),
      "  n) New project",
      ...(page.more
        ? [
            page.total !== undefined
              ? `Showing the first ${PROJECT_PAGE} of ${page.total} projects.`
              : `Showing the first ${PROJECT_PAGE} projects.`,
          ]
        : []),
    ].join("\n"),
  );
  const range = projects.length ? `1-${projects.length}, n` : "n";
  for (;;) {
    const answer = await prompt(`Pick a project [${range}]: `);
    if (answer.trim().toLowerCase() === "n") return create();
    const index = menuChoice(answer, projects.length);
    if (index !== null) return projects[index];
    announce("Not a listed choice.");
  }
}
/**
 * One browser approval step (sign-in or pairing consent); poll()'s refusal or
 * timeout becomes the sentence `rc` prints.
 */
async function approved<T>(step: () => Promise<T>): Promise<T> {
  try {
    return await step();
  } catch (error) {
    if (
      error instanceof Error &&
      ["authorization denied or expired", "authorization expired"].includes(
        error.message,
      )
    )
      throw new Error("Not approved; run nous --rc again", { cause: error });
    throw error;
  }
}
/** `fetchFn`, naming the NOUS it could not reach instead of "fetch failed". */
function reaching(fetchFn: typeof fetch, base: string): typeof fetch {
  return (async (...args: Parameters<typeof fetch>) => {
    try {
      return await fetchFn(...args);
    } catch (error) {
      // fetch rejects with a TypeError only when no response arrived at all;
      // HTTP statuses and timeouts reach the caller unchanged.
      if (!(error instanceof TypeError)) throw error;
      const cause: unknown = error.cause;
      const detail =
        record(cause) && typeof cause.code === "string"
          ? cause.code
          : cause instanceof Error
            ? cause.message
            : error.message;
      throw new Error(`Could not reach NOUS at ${base} (${detail})`, {
        cause: error,
      });
    }
  }) as typeof fetch;
}
/** The real path of `path`, resolved through its deepest existing ancestor. */
async function existingRealpath(path: string): Promise<string> {
  const absolute = resolve(path);
  const real = await realpath(absolute).catch(() => null);
  if (real !== null) return real;
  const parent = dirname(absolute);
  return parent === absolute
    ? absolute
    : join(await existingRealpath(parent), basename(absolute));
}
/** Whether `path` is `folder` itself or inside it. */
function within(path: string, folder: string): boolean {
  const rel = relative(folder, path);
  return (
    rel === "" ||
    (rel !== ".." && !rel.startsWith(`..${sep}`) && !isAbsolute(rel))
  );
}
export type RcOptions = ClientOptions & {
  apiUrl: string;
  /** The folder Codex works in: the caller's current directory. */
  dir: string;
  /** The home folder, which is too broad to work in; defaults to homedir(). */
  home?: string;
  label?: string;
  prompt: (question: string) => Promise<string>;
  /** Checks the local Codex before anything else; resolves with its version. */
  probe: () => Promise<{ version: string }>;
  /** Runs the bridge in the foreground until the caller stops it. */
  run: (stateDir: string) => Promise<void>;
};
/**
 * `nous --rc`: sign in, pick or create a project, pair this computer for
 * harness runs with read access to the project's documents, register the
 * current folder, run. Reuses the stored pairing when it is for the same
 * project; a different project replaces it (one pairing per computer).
 */
export async function rc(options: RcOptions): Promise<void> {
  const base = apiBase(options.apiUrl);
  const fetchFn = reaching(options.fetchFn ?? fetch, base);
  const announce = options.announce ?? console.log;
  // Codex and the folder first, so the user never approves a pairing that
  // cannot run.
  const { version } = await options.probe();
  const root = await realpath(options.dir).catch(() => null);
  if (root === null || !(await stat(root)).isDirectory())
    throw new Error(`${options.dir} is not a folder`);
  // Codex may write anywhere in the folder: never the filesystem root, the
  // home folder, or a folder holding this bridge's own credentials.
  const home = await existingRealpath(options.home ?? homedir());
  if (
    dirname(root) === root ||
    root === home ||
    within(await existingRealpath(options.stateDir), root)
  )
    throw new Error(
      `${root} is too broad for Codex to work in; run nous --rc in a project folder`,
    );
  announce(`NOUS: ${base}`);
  const store = new CredentialStore(options.stateDir);
  const previous = storedConnection(
    await store.readLocal("connection").catch(() => null),
  );
  let sameApi = false;
  try {
    sameApi = previous !== null && apiBase(previous.apiUrl) === base;
  } catch {
    // An unparsable stored URL is a different NOUS.
  }
  // The stored CLI token first, and only for the NOUS that issued it; a 401
  // means sign in again, once.
  let token: string | undefined =
    sameApi && previous !== null
      ? await store
          .load(previous.credentialHandle)
          .then((c) => c.accessToken)
          .catch(() => undefined)
      : undefined;
  let page =
    token === undefined ? null : await listProjects(fetchFn, base, token);
  if (token === undefined || page === null) {
    token = await approved(() => login(base, { ...options, fetchFn }));
    page = await listProjects(fetchFn, base, token);
    if (page === null) throw new Error("NOUS refused the new sign-in");
    // connect keeps a reused pairing's stored credential, which grant renewal
    // and folder registration sign with: give it the new sign-in.
    if (sameApi && previous !== null) {
      const stored = await store
        .load(previous.credentialHandle)
        .catch(() => null);
      if (stored !== null)
        await store.update(previous.credentialHandle, {
          ...stored,
          accessToken: token,
        });
    }
  }
  const signedIn = token;
  const { projects } = page;
  announce("Signed in.");
  const project = await pickProject(page, options.prompt, announce, () =>
    createProject(fetchFn, base, signedIn, options.prompt, announce),
  );
  const label = options.label ?? hostname();
  await approved(() =>
    connect({
      stateDir: options.stateDir,
      fetchFn,
      announce,
      delay: options.delay,
      apiUrl: base,
      projectId: project.id,
      label,
      tools: true,
      accessToken: signedIn,
    }),
  );
  // The new pairing replaced the stored one; its consent outlives it in NOUS.
  if (sameApi && previous !== null && previous.projectId !== project.id) {
    const until =
      "stays active until you revoke it under Settings → Connected devices (/integrations/devices) in NOUS.";
    if (previous.projectId !== undefined) {
      const old = projects.find((p) => p.id === previous.projectId);
      announce(
        `Your approval for project ${old ? `"${old.name}" (${old.id})` : previous.projectId} ${until}`,
      );
    } else
      announce(
        `Your previous workspace connection (${previous.workspaceId}) ${until}`,
      );
  }
  const { workspaceId } = await addWorkspace({
    stateDir: options.stateDir,
    fetchFn,
    // addWorkspace's own notice is about write roots; rc prints its summary
    // below.
    announce: () => {},
    root,
  });
  // Name the device and folder as the web chat lists them: the stored labels.
  const stored = storedConnection(
    await store.readLocal("connection").catch(() => null),
  );
  const device = stored?.label ?? label;
  const folder =
    stored?.workspaces.find((w) => w.id === workspaceId)?.label ??
    basename(root);
  announce(`Connected: "${project.name}" ↔ ${root}  (Codex ${version})`);
  announce(
    `In a chat for this project on NOUS, choose Local Codex → ${device} → ${folder}.\nPress Ctrl-C to stop.`,
  );
  await options.run(options.stateDir);
}
/**
 * A line prompt over a terminal for `rc`. An open question rejects with
 * `Cancelled` whenever the prompt closes: on Node 24 a bare `rl.question()`
 * rejects on Ctrl-D in terminal mode, but stays pending on end of input, on
 * `close()`, and on Ctrl-C when a SIGINT listener exists. Ctrl-C, Ctrl-D or end
 * of input, with or without a question open, then calls `onInterrupt` to cancel
 * the whole command; `close()` releases the terminal for the running bridge
 * without calling it.
 */
export function terminalPrompt(
  input: NodeJS.ReadableStream,
  output: NodeJS.WritableStream,
  onInterrupt?: () => void,
): {
  prompt: (question: string) => Promise<string>;
  close: () => void;
} {
  const rl = createInterface({
    input,
    output,
    terminal: (output as { isTTY?: boolean }).isTTY === true,
    // readline redraws its prompt on a terminal resize even with no question
    // open; the default would print a stray "> ".
    prompt: "",
  });
  let closed = false;
  // close() was called: the caller is done with the prompt, nothing to cancel.
  let released = false;
  let cancel: ((error: Error) => void) | undefined;
  // End of input, Ctrl-D, Ctrl-C and close() all land here; readline has
  // already left raw mode.
  rl.on("close", () => {
    closed = true;
    const reject = cancel;
    cancel = undefined;
    if (reject !== undefined) {
      // End the question's line so the caller's message starts on its own.
      output.write("\n");
      reject(new Error("Cancelled"));
    }
    if (!released) onInterrupt?.();
  });
  // Ctrl-C in a terminal reaches readline as a key, not a process signal, even
  // when no question is open.
  rl.on("SIGINT", () => rl.close());
  // After Ctrl-Z and fg readline pauses its input and resumes only if asked;
  // paused, an open question lets the process exit 0 and Ctrl-C is lost.
  rl.on("SIGCONT", () => rl.resume());
  return {
    prompt: (question) => {
      if (closed) return Promise.reject(new Error("Cancelled"));
      return new Promise<string>((resolve, reject) => {
        cancel = reject;
        rl.question(question).then(
          (answer) => {
            if (cancel === reject) cancel = undefined;
            resolve(answer);
          },
          (error: unknown) => {
            if (cancel === reject) cancel = undefined;
            reject(closed ? new Error("Cancelled") : error);
          },
        );
      });
    },
    close: () => {
      released = true;
      if (!closed) rl.close();
    },
  };
}
/** `rc`'s Codex check: the tested version, or a hint when Codex cannot run. */
export async function probeCodex(
  adapter: CodexAdapter = new CodexAdapter(),
): Promise<{ version: string }> {
  try {
    await adapter.probe();
  } catch (error) {
    // Also a non-zero exit or the 5 s timeout, not only a missing binary.
    if (error instanceof Error && error.message === "Codex version probe failed")
      throw new Error(
        "Could not run `codex --version`; check that Codex is installed, on your PATH and working",
        { cause: error },
      );
    throw error;
  }
  return { version: adapter.version ?? "unknown" };
}
/** The pid a lock file names, or null when it names none. */
function lockPid(text: string): number | null {
  try {
    const pid: unknown = JSON.parse(text)?.pid;
    // process.kill reads 0 and negative pids as process groups.
    return typeof pid === "number" && Number.isSafeInteger(pid) && pid > 0
      ? pid
      : null;
  } catch {
    return null;
  }
}
/** Signal 0 only checks: EPERM means the process exists under another user. */
function pidAlive(pid: number): boolean {
  try {
    process.kill(pid, 0);
    return true;
  } catch (error) {
    return (error as NodeJS.ErrnoException).code === "EPERM";
  }
}
/**
 * One bridge per state directory: a second `nous --rc` or `nous-harness run`
 * would force-renew the running bridge's grant, reopen its journal and poll
 * as the same device. Creates `<stateDir>/bridge.lock` (owner-only) naming
 * this process; a lock whose process is gone, or that cannot be read, is
 * stale and replaced. Resolves with the release, which removes the lock only
 * while it still names this process; `process.exit` releases it too.
 */
export async function acquireStateLock(
  stateDir: string,
): Promise<() => Promise<void>> {
  const path = join(stateDir, "bridge.lock");
  await mkdir(stateDir, { recursive: true, mode: 0o700 });
  for (let attempt = 0; attempt < 3; attempt += 1) {
    try {
      const handle = await open(path, "wx", 0o600);
      try {
        await handle.writeFile(
          JSON.stringify({
            pid: process.pid,
            startedAt: new Date().toISOString(),
          }),
        );
      } finally {
        await handle.close();
      }
      // Synchronous, so an "exit" listener can run it.
      const release = (): void => {
        process.off("exit", release);
        try {
          if (lockPid(readFileSync(path, "utf8")) === process.pid)
            unlinkSync(path);
        } catch {
          // Gone already, or unreadable: not this process's lock to remove.
        }
      };
      process.on("exit", release);
      return async () => release();
    } catch (error) {
      if ((error as NodeJS.ErrnoException).code !== "EEXIST") throw error;
    }
    const seen = await readFile(path, "utf8").catch(() => null);
    const pid = seen === null ? null : lockPid(seen);
    if (pid !== null && pidAlive(pid))
      throw new Error(
        `Another nous --rc or nous-harness run (pid ${pid}) is already using ${stateDir}; stop it first`,
      );
    // Stale. Remove it only if it is still the file judged stale, so two
    // processes clearing one stale lock do not remove each other's new one.
    if ((await readFile(path, "utf8").catch(() => null)) === seen)
      await unlink(path).catch(() => {});
  }
  throw new Error(`could not lock ${stateDir}; try again`);
}
/** One local reconciliation pass over the locked runs, without the network. */
export async function reconcileLocal(
  journal: Journal,
  adapterFor: (workspaceId: string, runId: string) => HarnessAdapter,
): Promise<void> {
  for (const c of journal.activeCommands()) {
    const state = journal.state(c.commandId);
    // A refused run is not recovered: reconcile only reads its turn back and
    // re-arms its watchdog (Journal.reconcile).
    if (state === "recovering" || state === "denied")
      try {
        await journal.reconcile(
          c.commandId,
          adapterFor(c.workspaceId, c.runId),
        );
      } catch {
        journal.quarantine(c.commandId);
      }
  }
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
  // The bridge socket serves harness sessions only. Refuse now rather than
  // reconnect forever against a backend that will never accept this grant.
  if (state.workspaceId !== undefined) throw new Error(WORKSPACE_MCP_ONLY);
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
  // Same loopback-only exception as the MCP facade: plain ws for a local dev
  // backend, wss everywhere else.
  url.protocol =
    url.protocol === "http:" &&
    ["localhost", "127.0.0.1", "[::1]"].includes(url.hostname)
      ? "ws:"
      : "wss:";
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
      await reconcileLocal(journal, adapterFor);
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
    // Publication is bound to a registered root only; never to the cwd, and
    // never on a workspace connection, whatever its stored scopes say.
    ...(outputRoot &&
    state.workspaceId === undefined &&
    state.scopes?.includes("artifacts:publish")
      ? { outputRoot }
      : {}),
    // `actions` offers request_action to a grant holding tools:write, on either
    // binding: a project grant for notes and library actions, a workspace grant
    // for library actions (NOUS answers 422 to a note there). Without
    // library:write each one waits for the user's approval.
    ...(state.scopes?.includes("tools:write") ? { actions: true } : {}),
    ...(state.scopes?.includes("handoff:write") ? { handoff: true } : {}),
    ...(state.scopes?.includes("library:write") ? { library: true } : {}),
    // read_selected_context reads the memories chosen for the granted project.
    // A workspace grant has none and NOUS refuses it context:read, so the tool
    // is not offered there whatever the stored scopes say.
    ...(state.workspaceId === undefined && state.scopes?.includes("context:read")
      ? { context: true }
      : {}),
  };
}
/** Managed sessions get the NOUS MCP server only when the grant carries tools:read. */
export function sessionOptionsFor(
  stateDir: string,
  state: LocalState,
  workspaceId: string,
): SessionOptions {
  // `workspaceId` here is a registered local folder; a NOUS workspace connection has none.
  if (state.workspaceId !== undefined) throw new Error(WORKSPACE_MCP_ONLY);
  const workspace = state.workspaces.find((w) => w.id === workspaceId);
  if (!workspace) throw new Error("unregistered local workspace");
  // Local operator choice: the pinned Codex CLI may reject the user's global
  // default model under their login (e.g. a ChatGPT account), so allow pinning
  // one for bridge sessions without touching ~/.codex/config.toml.
  const model = process.env.NOUS_HARNESS_CODEX_MODEL?.trim();
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
    ...(model ? { model } : {}),
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
  const publish = state.scopes.includes("artifacts:publish");
  let root: string | undefined;
  if (!publish && options.root !== undefined)
    throw new Error(
      "--root requires artifacts:publish; reconnect with nous-harness connect --tools --publish",
    );
  if (publish) {
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
  }
  // Build first: a bad stored API URL or state path throws here, before any
  // hint points at a command that is never shown.
  const command = standaloneInstallCommand(mcpSession(stateDir, state, root));
  const announce = options.announce ?? console.error;
  // RT-2: this package never edits ~/.codex/config.toml, so say what to add,
  // once the command is certain. The publish line stays last: artifacts.test.ts
  // reads it.
  announce(
    `After you run the command below, add tool_timeout_sec = ${MCP_TOOL_TIMEOUT_SEC} under [mcp_servers.nous] in ~/.codex/config.toml: Codex stops waiting for an MCP tool after 60 s by default, and NOUS's arXiv tools can take up to 125 s.`,
  );
  if (publish)
    announce(
      root
        ? `artifacts_publish will publish from ${root}`
        : "no workspace registered; artifacts_publish will not be offered",
    );
  return command;
}
/** Offline summary of the local binding, grant expiry and handoff queue. */
export async function status(options: {
  stateDir: string;
  announce?: (message: string) => void;
}): Promise<void> {
  const announce = options.announce ?? console.log;
  const store = new CredentialStore(options.stateDir);
  const entries = await queueEntries(store);
  const count = (state: QueueEntry["state"]) => entries.filter((e) => e.state === state).length;
  const extra = [
    count("conflicted") ? `${count("conflicted")} conflicted` : "",
    count("rejected") ? `${count("rejected")} rejected` : "",
  ].filter(Boolean);
  const queueLine = `Pending handoffs: ${count("pending")}${extra.length ? ` (${extra.join(", ")})` : ""}`;
  const state = storedConnection(await store.readLocal("connection").catch(() => null));
  if (state === null) {
    announce(["This device is not connected (run nous-harness connect).", ...(entries.length ? [queueLine] : [])].join("\n"));
    return;
  }
  let grant: string;
  try {
    const credentials = await store.load(state.credentialHandle);
    if (typeof credentials.renewedAt !== "number")
      grant = "connection predates grant renewal; reconnect to keep it alive";
    else {
      const expiry = credentials.renewedAt + GRANT_LIFETIME_MS;
      grant =
        expiry > Date.now()
          ? `expires ${new Date(expiry).toISOString()} unless a running bridge or MCP session renews it`
          : `expired ${new Date(expiry).toISOString()}; reconnect with nous-harness connect`;
    }
  } catch {
    grant = "local credentials missing; reconnect with nous-harness connect";
  }
  announce(
    [
      ...(state.projectId !== undefined
        ? [
            `Project: ${state.projectId}`,
            `Chat: ${state.threadId ?? "none (project only)"}`,
          ]
        : [`Workspace: ${state.workspaceId} (every project in it; MCP-only)`]),
      `Device: ${state.label ?? "unlabelled"} (${state.deviceId})`,
      `API: ${state.apiUrl}`,
      `Scopes: ${state.scopes?.join(" ") ?? "unknown"}`,
      `Grant: ${grant}`,
      queueLine,
    ].join("\n"),
  );
}
const RECONNECT_HANDOFF = "reconnect with nous-harness connect --chat UUID --tools --handoff";
/** The chat-bound connection, its queue and a keeper-backed handoff client. */
async function handoffContext(
  options: ClientOptions,
  scope: "handoff:read" | "handoff:write",
): Promise<{ binding: Binding; queue: HandoffQueue; client: HandoffHttpClient; announce: (m: string) => void }> {
  const store = new CredentialStore(options.stateDir);
  const state = storedConnection(await store.readLocal("connection").catch(() => null));
  if (state === null) throw new Error("connect this device first");
  // A handoff belongs to one chat of one project; a workspace connection has neither.
  if (state.projectId === undefined)
    throw new Error(
      "this device is connected to a workspace, which is MCP-only and has no chat handoff; reconnect with nous-harness connect --project UUID --chat UUID --tools --handoff",
    );
  if (!uuid(state.threadId)) throw new Error(`this device is not bound to a chat; ${RECONNECT_HANDOFF}`);
  if (!state.scopes?.includes(scope)) throw new Error(`this connection lacks ${scope}; ${RECONNECT_HANDOFF}`);
  const base = apiBase(state.apiUrl);
  const keeper = new GrantKeeper(store, state.credentialHandle, base, options.fetchFn ?? fetch);
  let credentials;
  try {
    credentials = await store.load(state.credentialHandle);
  } catch {
    throw new Error(`this connection's local credentials are missing or unreadable; ${RECONNECT_HANDOFF}`);
  }
  const client = new HandoffHttpClient(base, credentials, keeper.fetch);
  return {
    binding: { threadId: state.threadId, projectId: state.projectId },
    queue: new HandoffQueue(store, client),
    client,
    announce: options.announce ?? console.log,
  };
}
function reportAttempt(handoffId: string, attempt: Attempt, announce: (m: string) => void): void {
  if (attempt.state === "done")
    announce(`Saved handoff version ${attempt.saved.version} (${handoffId}).`);
  else if (attempt.state === "conflicted")
    announce(
      attempt.latest === null
        ? `Not saved (${handoffId}): ${attempt.detail}. Kept as conflicted; read the latest with nous-harness handoff show and merge it yourself.`
        : `Not saved (${handoffId}): the chat's latest handoff is version ${attempt.latest.version}. Kept as conflicted; merge your changes into it yourself and save again with --parent ${attempt.latest.version} and a new handoff_id. Latest:\n${JSON.stringify(attempt.latest, null, 2)}`,
    );
  else if (attempt.state === "rejected")
    announce(`Rejected by NOUS (${handoffId}): ${attempt.error.message}. Kept as rejected; it is not retried.`);
  else
    announce(
      `Not confirmed (${handoffId}): ${attempt.error.message}. Queued as pending; run nous-harness handoff flush to retry.` +
        (attempt.reconnect ? ` Then ${RECONNECT_HANDOFF} before flushing.` : ""),
    );
}
/** `handoff show`: the chat's latest handoff, read over HTTPS. */
export async function handoffShow(options: ClientOptions): Promise<Handoff | null> {
  const context = await handoffContext(options, "handoff:read");
  const latest = await context.client.getLatest();
  context.announce(latest ? JSON.stringify(latest, null, 2) : "No handoff yet in this chat.");
  return latest;
}
/** `handoff save`: validate the file, journal it, then POST it. */
export async function handoffSave(
  options: ClientOptions & { file: string; parent?: string },
): Promise<Attempt> {
  let parsed: unknown;
  try {
    parsed = JSON.parse(await readFile(options.file, "utf8"));
  } catch {
    throw new Error("--file must name a readable JSON handoff file");
  }
  if (!record(parsed)) throw new Error("--file must contain a JSON object");
  const args: Record<string, unknown> = { expected_parent_version: null, ...parsed };
  if (options.parent !== undefined) {
    if (!/^[1-9][0-9]{0,9}$/.test(options.parent))
      throw new Error("--parent must be the latest handoff version (a positive integer)");
    args.expected_parent_version = Number(options.parent);
  }
  const payload = handoffPayload(args, "nous-harness");
  if (typeof payload === "string") throw new Error(payload);
  const context = await handoffContext(options, "handoff:write");
  const attempt = await context.queue.submit(context.binding, payload);
  reportAttempt(payload.handoff_id, attempt, context.announce);
  return attempt;
}
/** `handoff flush`: retry pending entries journaled for the current chat. */
export async function handoffFlush(options: ClientOptions): Promise<FlushResult> {
  const context = await handoffContext(options, "handoff:write");
  const result = await context.queue.flush(context.binding);
  for (const entry of result.skipped)
    context.announce(
      `Skipped ${entry.handoff_id}: journaled for chat ${entry.thread_id} in project ${entry.project_id}, not this device's current binding.`,
    );
  for (const { entry, attempt } of result.attempted) reportAttempt(entry.handoff_id, attempt, context.announce);
  if (result.attempted.length === 0) context.announce("No pending handoffs for this chat.");
  return result;
}
/** `handoff discard ID`: drop one journaled entry in any state; offline. */
export async function handoffDiscard(options: ClientOptions & { handoffId: string }): Promise<QueueEntry> {
  const store = new CredentialStore(options.stateDir);
  const entry = await new HandoffQueue(store, {
    save: async () => {
      throw new Error("discard never sends");
    },
  }).discard(options.handoffId);
  if (entry === null) throw new Error(`no queued handoff ${options.handoffId}; see nous-harness handoff list`);
  (options.announce ?? console.log)(
    `Discarded ${entry.handoff_id} (${entry.state}, chat ${entry.thread_id}, goal: ${JSON.stringify(entry.body.goal.slice(0, 120))}).`,
  );
  return entry;
}
/** `handoff list`: every journaled entry and its state; offline. */
export async function handoffList(options: ClientOptions): Promise<QueueEntry[]> {
  const entries = await queueEntries(new CredentialStore(options.stateDir));
  (options.announce ?? console.log)(
    entries.length
      ? entries
          .map((e) =>
            [e.handoff_id, e.state, `chat ${e.thread_id}`, e.created_at, e.last_error ?? ""].join("  ").trimEnd(),
          )
          .join("\n")
      : "No queued handoffs.",
  );
  return entries;
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
const help = `Usage: nous-harness connect --api https://host/api/v1 (--project UUID | --workspace UUID) --label NAME [--chat UUID] [--tools [--publish] [--write [--library]] [--handoff] [--context]] | workspace add --root PATH [--label NAME] | run | rc --api URL --root PATH [--label NAME] | status | handoff show|save|flush|list|discard ID
  connect --chat UUID    Bind this device to one NOUS chat in --project: harness runs are leased and files are published only in that chat; if the chat is deleted or moved, reconnect. Reconnect without --chat to unbind.
  connect --chat UUID --tools --handoff also lets sessions read and save the chat's structured handoff (get_nous_handoff / save_nous_handoff).
  connect --workspace UUID binds the grant to every project in one NOUS workspace (not a local folder; see workspace add). It is MCP-only: it needs --tools, cannot take --publish, --chat, --handoff or --context, and cannot run harness sessions. It always requests library:read, so list_library can name the projects its tools need.
  connect --library (needs --tools and --write) also requests library:read and library:write.
  connect reuses the stored binding (no browser login or consent) when the API, project or workspace, and chat match, every requested scope is already granted, and the grant still renews; otherwise it runs the full flow. Reuse never drops a scope and names any it keeps; to narrow scopes, revoke this device's access at /integrations/devices (or disconnect), then connect.
  nous-harness status    Show the binding (project or workspace, chat, device, grant expiry) and the handoff queue; works offline.
  nous-harness rc --api URL --root PATH [--label NAME]    Interactive: sign in, pick or create a project, pair this computer (harness:execute + tools:read), register PATH as the folder Codex works in, and run. The repository's \`nous --rc\` launcher calls this with the current folder.
  nous-harness handoff show    Print the bound chat's latest handoff.
  nous-harness handoff save --file handoff.json [--parent N]    Journal the handoff locally, then save it; --parent sets expected_parent_version (default null, the first handoff).
  nous-harness handoff flush    Retry pending saves journaled for the current chat; a conflict (409) or rejection (403/422) is kept, never retried or merged.
  nous-harness handoff list    List journaled handoff saves and their states.
  nous-harness handoff discard HANDOFF_ID    Drop one journaled save (pending, conflicted or rejected) and print what was dropped.
  nous-harness disconnect    Revoke this device's NOUS access and remove its local credentials.
  nous-harness mcp install [--root PATH]    Print the Codex command that registers NOUS tools for a --tools connection; --root picks the publish folder.
  nous-harness mcp --api URL --session HANDLE [--store PATH] [--root PATH] [--actions] [--handoff] [--library] [--context]    Serve NOUS tools over stdio (Codex launches this); --root enables artifacts_publish, --actions enables request_action and get_action_status for a tools:write grant, --handoff enables the chat handoff tools, --library enables them for a library:write grant too, --context enables read_selected_context.
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
      workspace: { type: "string" },
      chat: { type: "string" },
      label: { type: "string" },
      store: { type: "string" },
      root: { type: "string" },
      command: { type: "string" },
      session: { type: "string" },
      tools: { type: "boolean" },
      publish: { type: "boolean" },
      write: { type: "boolean" },
      library: { type: "boolean" },
      actions: { type: "boolean" },
      handoff: { type: "boolean" },
      file: { type: "string" },
      parent: { type: "string" },
      context: { type: "boolean" },
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
    (values.project || values.workspace) &&
    values.label
  )
    await connect({
      stateDir,
      apiUrl: values.api,
      projectId: values.project,
      workspaceId: values.workspace,
      threadId: values.chat,
      label: values.label,
      tools: values.tools,
      publish: values.publish,
      write: values.write,
      handoff: values.handoff,
      library: values.library,
      context: values.context,
    });
  else if (positionals.join(" ") === "disconnect")
    await disconnect({ stateDir });
  else if (positionals.join(" ") === "status") await status({ stateDir });
  else if (positionals.join(" ") === "handoff show") await handoffShow({ stateDir });
  else if (positionals.join(" ") === "handoff save" && values.file) {
    // Anything but a confirmed save exits non-zero; the entry stays journaled.
    const attempt = await handoffSave({ stateDir, file: values.file, parent: values.parent });
    if (attempt.state !== "done") process.exitCode = 1;
  } else if (positionals.join(" ") === "handoff flush") {
    const result = await handoffFlush({ stateDir });
    if (result.attempted.some(({ attempt }) => attempt.state !== "done")) process.exitCode = 1;
  } else if (positionals.join(" ") === "handoff list") await handoffList({ stateDir });
  else if (positionals.length === 3 && positionals[0] === "handoff" && positionals[1] === "discard")
    await handoffDiscard({ stateDir, handoffId: positionals[2] });
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
      ...(values.handoff ? { handoff: true } : {}),
      ...(values.library ? { library: true } : {}),
      ...(values.context ? { context: true } : {}),
    });
  } else if (positionals.join(" ") === "workspace add" && values.root)
    await addWorkspace({ stateDir, root: values.root, label: values.label });
  else if (positionals.join(" ") === "rc") {
    if (!values.api || !values.root)
      throw new Error("rc requires --api and --root");
    // Refuse before probing Codex or touching the network.
    if (!process.stdin.isTTY || !process.stdout.isTTY)
      throw new Error("nous --rc needs an interactive terminal");
    // One bridge per state directory, also before Codex or the network.
    const release = await acquireStateLock(stateDir);
    // Until the bridge starts, any Ctrl-C or end of input stops the whole
    // command, even mid-wait.
    const cancelCommand = () => {
      // connection.json is always the old or the new version (atomic rename);
      // the worst case of exiting mid-pairing is an orphaned owner-only
      // credential file or one more browser approval. The lock's "exit"
      // listener removes it.
      process.stderr.write("\nCancelled\n");
      process.exit(130);
    };
    // Backstops for signals that do not come through the prompt: a SIGINT
    // from outside, a kill, or the terminal closing.
    const signals = ["SIGINT", "SIGTERM", "SIGHUP"] as const;
    for (const signal of signals) process.on(signal, cancelCommand);
    const terminal = terminalPrompt(
      process.stdin,
      process.stdout,
      cancelCommand,
    );
    const controller = new AbortController();
    try {
      await rc({
        stateDir,
        apiUrl: values.api,
        dir: resolve(values.root),
        label: values.label,
        prompt: terminal.prompt,
        probe: () => probeCodex(),
        run: (stateDir) => {
          // Hand the terminal back so Ctrl-C reaches the process, and stops
          // the bridge gracefully, while it runs.
          terminal.close();
          for (const signal of signals) process.off(signal, cancelCommand);
          process.once("SIGINT", () => controller.abort());
          process.once("SIGTERM", () => controller.abort());
          return runBridge(stateDir, controller.signal);
        },
      });
    } finally {
      terminal.close();
      for (const signal of signals) process.off(signal, cancelCommand);
      await release();
    }
  } else if (positionals.join(" ") === "run") {
    const release = await acquireStateLock(stateDir);
    try {
      const controller = new AbortController();
      process.once("SIGINT", () => controller.abort());
      process.once("SIGTERM", () => controller.abort());
      await runBridge(stateDir, controller.signal);
    } finally {
      await release();
    }
  } else throw new Error(help);
}
if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href)
  void main().catch((error) => {
    console.error(
      error instanceof Error ? error.message : "Bridge command failed",
    );
    process.exitCode = 1;
  });
