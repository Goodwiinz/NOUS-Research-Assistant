import { execFile } from "node:child_process";
import { readFile, stat, mkdir, writeFile, rename, rm } from "node:fs/promises";
import { homedir } from "node:os";
import { basename, join, resolve } from "node:path";
import { createHash } from "node:crypto";
import type { components } from "../../frontend/src/types/generated/api";
import {
  getApiBase,
  getCliAuthHeaders,
} from "../../frontend/cli/services/client";
import { loadConfig } from "../../frontend/cli/auth/store";
import type { RuntimeMessage } from "@nous/chat-runtime/types";
import type { AttachmentAdapter } from "@assistant-ui/core";

type Schemas = components["schemas"];
export type Thread = Schemas["ThreadResponse"];
export type Message = Schemas["ChatMessageResponse"];
export type TerminalMessage = RuntimeMessage & {
  serverId?: string;
  threadId?: string;
};

export async function request<T>(
  path: string,
  method = "GET",
  body?: unknown,
  signal?: AbortSignal,
): Promise<T> {
  const base = getApiBase().replace(/\/api\/v1\/?$/, "/api/v2");
  const response = await fetch(`${base}${path}`, {
    method,
    headers: getCliAuthHeaders(),
    ...(body === undefined ? {} : { body: JSON.stringify(body) }),
    signal: signal
      ? AbortSignal.any([signal, AbortSignal.timeout(30000)])
      : AbortSignal.timeout(30000),
  });
  if (!response.ok)
    throw new Error(`${method} ${path}: HTTP ${response.status}`);
  return response.status === 204 ? (undefined as T) : response.json();
}

/** Follow the existing paginated workspace API, including non-agent fork threads. */
export async function listThreads(signal?: AbortSignal): Promise<Thread[]> {
  const workspaces = await request<Schemas["WorkspaceResponse"][]>(
    "/workspaces",
    "GET",
    undefined,
    signal,
  );
  const result: Thread[] = [];
  for (const workspace of workspaces) {
    for (let page = 1; ; page++) {
      const conversations = await request<Schemas["ConversationListResponse"]>(
        `/workspaces/${encodeURIComponent(workspace.id)}/conversations?limit=100&page=${page}`,
        "GET",
        undefined,
        signal,
      );
      for (const conversation of conversations.conversations) {
        for (let threadPage = 1; ; threadPage++) {
          const threads = await request<
            Schemas["src__schemas__chat__ThreadListResponse"]
          >(
            `/conversations/${encodeURIComponent(conversation.id)}/threads?limit=100&page=${threadPage}`,
            "GET",
            undefined,
            signal,
          );
          result.push(...threads.threads);
          if (!threads.has_more || !threads.threads.length) break;
        }
      }
      if (!conversations.has_more || !conversations.conversations.length) break;
    }
  }
  return result.sort((a, b) => b.updated_at.localeCompare(a.updated_at));
}

export const updateThread = (id: string, data: Schemas["ThreadUpdate"]) =>
  request<Thread>(`/threads/${encodeURIComponent(id)}`, "PATCH", data);
export const deleteThread = (id: string) =>
  request<void>(`/threads/${encodeURIComponent(id)}`, "DELETE");
export const getThread = (id: string, signal?: AbortSignal) =>
  request<Schemas["ThreadDetailResponse"]>(
    `/threads/${encodeURIComponent(id)}?include_messages=false`,
    "GET",
    undefined,
    signal,
  );

export function fromServer(message: Message): TerminalMessage[] {
  if (message.role !== "user" && message.role !== "assistant") return [];
  return [
    {
      runtimeId: message.client_message_id || message.id,
      serverId: message.id,
      threadId: message.thread_id,
      role: message.role,
      content: message.content,
      timestamp: Date.parse(message.created_at) || 0,
      attachments: message.attachments?.map((a) => ({
        id: a.id,
        document_id: a.document_id,
        display_name: a.display_name ?? undefined,
        document_title: a.document_title ?? undefined,
        document_type: a.document_type ?? undefined,
        mime_type: a.mime_type ?? undefined,
        thumbnail_url: a.thumbnail_url ?? undefined,
      })),
      contexts: message.citations,
      toolExecutions: (message.tool_executions ?? []).map((tool, index) => ({
        id: String(tool.id ?? index),
        tool: String(tool.tool_name ?? tool.tool ?? "tool"),
        label: String(tool.tool_display_name ?? tool.tool_name ?? "Tool"),
        status: tool.status === "error" ? "error" : "done",
        args:
          tool.args && typeof tool.args === "object"
            ? (tool.args as Record<string, unknown>)
            : {},
        argsSummary:
          typeof tool.args === "string"
            ? tool.args
            : JSON.stringify(tool.args ?? {}),
        result: tool.result,
        resultSummary: typeof tool.error === "string" ? tool.error : undefined,
      })),
      metadata: { stopped: message.stopped ?? false },
      timing: { tokenCount: message.token_count },
      feedback: message.feedback_rating
        ? {
            rating: message.feedback_rating,
            comment: message.feedback_text ?? null,
          }
        : undefined,
    },
  ];
}

export async function loadTranscript(
  id: string,
  signal?: AbortSignal,
): Promise<TerminalMessage[]> {
  const messages: TerminalMessage[] = [];
  let offset = 0;
  while (true) {
    const page = await request<Schemas["ChatMessageListResponse"]>(
      `/threads/${encodeURIComponent(id)}/messages?limit=100&offset=${offset}&order=asc`,
      "GET",
      undefined,
      signal,
    );
    messages.push(...page.messages.flatMap(fromServer));
    if (!page.has_more || !page.messages.length) break;
    offset += page.messages.length;
  }
  return messages;
}

/** A branch is a real server thread; copy only the selected prefix before sending.
 * No live turn is persisted here: the agent endpoint remains its sole writer. */
export async function forkThread(
  id: string,
  prefix: TerminalMessage[],
  signal?: AbortSignal,
): Promise<string> {
  const source = await getThread(id, signal);
  signal?.throwIfAborted();
  const branch = await request<Thread>("/threads", "POST", {
    conversation_id: source.conversation_id,
    title: `${source.title || "Chat"} · branch`,
    ...(source.source_project_id
      ? { project_id: source.source_project_id }
      : {}),
  } satisfies Schemas["ThreadCreate"]);
  try {
    for (const message of prefix) {
      signal?.throwIfAborted();
      await request<Message>("/messages", "POST", {
        thread_id: branch.id,
        role: message.role,
        content: message.content,
        ...(message.attachments?.length
          ? { attachment_ids: message.attachments.map((a) => a.document_id) }
          : {}),
      } satisfies Schemas["ChatMessageCreate"]);
    }
    signal?.throwIfAborted();
  } catch (error) {
    // Leave the original and the recoverable partial branch intact on uncertain writes.
    throw new Error(
      `Branch ${branch.id} could not be completed. Original thread unchanged. ${String(error)}`,
    );
  }
  return branch.id;
}

export async function fileFromPath(value: string): Promise<File> {
  const path = resolve(
    value.startsWith("~/") ? join(homedir(), value.slice(2)) : value,
  );
  const info = await stat(path);
  if (!info.isFile() || info.size > 50 * 1024 * 1024)
    throw new Error("Choose a regular file up to 50 MiB.");
  return new File([await readFile(path)], basename(path), {
    type: "application/octet-stream",
  });
}

export const attachmentAdapter: AttachmentAdapter = {
  accept: "*",
  async add({ file }) {
    if (file.size > 50 * 1024 * 1024)
      throw new Error("Maximum upload size is 50 MiB.");
    const form = new FormData();
    form.append("file", file);
    form.append("title", file.name);
    const headers = getCliAuthHeaders();
    delete headers["Content-Type"];
    const response = await fetch(`${getApiBase()}/files/upload`, {
      method: "POST",
      headers,
      body: form,
      signal: AbortSignal.timeout(120000),
    });
    if (!response.ok) throw new Error(`Upload failed: HTTP ${response.status}`);
    const result = (await response.json()) as Schemas["FileUploadResponse"];
    if (typeof result.document_id !== "string" || !result.document_id)
      throw new Error("Upload returned no document ID.");
    return {
      id: result.document_id,
      name: file.name,
      type: "document",
      file,
      contentType: file.type,
      status: { type: "requires-action", reason: "composer-send" },
    };
  },
  async send(attachment) {
    return { ...attachment, status: { type: "complete" }, content: [] };
  },
  async remove() {
    /* Detach from this prompt; keep the uploaded document. */
  },
};

export async function copyText(text: string): Promise<void> {
  const programs: [string, string[]][] =
    process.platform === "darwin"
      ? [["pbcopy", []]]
      : process.platform === "win32"
        ? [["clip", []]]
        : [
            ["wl-copy", []],
            ["xclip", ["-selection", "clipboard"]],
            ["xsel", ["--clipboard", "--input"]],
          ];
  for (const [program, args] of programs) {
    try {
      await new Promise<void>((resolve, reject) => {
        const child = execFile(program, args, { timeout: 5000 }, (error) =>
          error ? reject(error) : resolve(),
        );
        child.stdin?.on("error", reject);
        child.stdin?.end(text);
      });
      return;
    } catch {
      /* Try the next native clipboard provider. */
    }
  }
  throw new Error("No clipboard provider found. Use /export <path> instead.");
}

export interface BranchNode {
  message: TerminalMessage;
  parentId: string | null;
}
export interface BranchHistory {
  nodes: BranchNode[];
  headId: string | null;
}
export function withoutThread(
  history: BranchHistory,
  threadId: string,
): BranchHistory {
  const byId = new Map(history.nodes.map((n) => [n.message.runtimeId, n]));
  const keep = new Set<string>();
  for (const node of history.nodes) {
    if (!node.message.threadId || node.message.threadId === threadId) continue;
    let current: string | null = node.message.runtimeId;
    while (current && !keep.has(current)) {
      keep.add(current);
      current = byId.get(current)?.parentId ?? null;
    }
  }
  return {
    headId: history.headId && keep.has(history.headId) ? history.headId : null,
    nodes: history.nodes
      .filter((n) => keep.has(n.message.runtimeId))
      .map((n) =>
        n.message.threadId === threadId
          ? {
              ...n,
              message: {
                ...n.message,
                threadId: undefined,
                serverId: undefined,
              },
            }
          : n,
      ),
  };
}

/** Update every saved sibling copy before removing the deleted thread's graph. */
export async function forgetBranches(threadId: string, current: BranchHistory) {
  const saved = await loadBranches(threadId);
  const ids = new Set(
    [...current.nodes, ...(saved?.nodes ?? [])]
      .map((n) => n.message.threadId)
      .filter((id): id is string => !!id && id !== threadId),
  );
  for (const id of ids) {
    const history = await loadBranches(id);
    if (history) await writeBranches(id, withoutThread(history, threadId));
  }
  await rm(branchPath(threadId), { force: true });
}

function branchPath(threadId: string) {
  const config = loadConfig();
  const key = createHash("sha256")
    .update(
      `${getApiBase()}|${config?.organization_id}|${config?.user_email}|${threadId}`,
    )
    .digest("hex");
  return join(
    process.env.NOUS_CONFIG_DIR ?? join(homedir(), ".nous"),
    "branches",
    `${key}.json`,
  );
}
export async function saveBranches(history: BranchHistory) {
  const ids = new Set(
    history.nodes
      .map((node) => node.message.threadId)
      .filter((id): id is string => !!id),
  );
  for (const id of ids) await writeBranches(id, history);
}
async function writeBranches(id: string, history: BranchHistory) {
  const path = branchPath(id);
  await mkdir(resolve(path, ".."), { recursive: true, mode: 0o700 });
  await writeFile(`${path}.tmp`, JSON.stringify(history), { mode: 0o600 });
  await rename(`${path}.tmp`, path);
}

export async function loadBranches(
  threadId: string,
): Promise<BranchHistory | undefined> {
  try {
    const data = JSON.parse(
      await readFile(branchPath(threadId), "utf8"),
    ) as BranchHistory;
    if (
      !Array.isArray(data.nodes) ||
      data.nodes.some(
        (n) =>
          !n.message?.runtimeId ||
          typeof n.message.content !== "string" ||
          !["user", "assistant"].includes(n.message.role),
      )
    )
      return;
    const ids = new Set(data.nodes.map((n) => n.message.runtimeId));
    if (
      ids.size !== data.nodes.length ||
      (data.headId && !ids.has(data.headId)) ||
      data.nodes.some((n) => n.parentId !== null && !ids.has(n.parentId))
    )
      return;
    const parents = new Map(
      data.nodes.map((n) => [n.message.runtimeId, n.parentId]),
    );
    for (const node of data.nodes) {
      const seen = new Set<string>();
      let current: string | null = node.message.runtimeId;
      while (current) {
        if (seen.has(current)) return;
        seen.add(current);
        current = parents.get(current) ?? null;
      }
    }
    return data;
  } catch {
    return;
  }
}
