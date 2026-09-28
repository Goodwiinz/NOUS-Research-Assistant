import { execFile } from "node:child_process";
import { promisify } from "node:util";
import { realpath } from "node:fs/promises";
import { isAbsolute } from "node:path";
import { Ajv } from "ajv";
import commandSchema from "./protocol-0.153.4/CommandExecutionRequestApprovalParams.json" with { type: "json" };
import fileSchema from "./protocol-0.153.4/FileChangeRequestApprovalParams.json" with { type: "json" };
import inputSchema from "./protocol-0.153.4/ToolRequestUserInputParams.json" with { type: "json" };
import type {
  AdapterEvent,
  Capabilities,
  HarnessAdapter,
  NativeRequestId,
  NativeResponse,
  NativeSession,
  NativeTurn,
  NativeObservation,
  SessionOptions,
} from "../contracts.ts";
import {
  BoundedQueue,
  JsonRpcProcess,
  record,
  TransportLoss,
  type RpcMessage,
} from "../rpc.ts";

export const SUPPORTED_CODEX_VERSION = "0.153.4";
// Leave 4 KiB for Task 4 durable envelope metadata within the 16 KiB limit.
const MAX_SERIALIZED_DELTA_BYTES = 12 * 1024;
const MAX_SEEN_CALLBACKS = 4096;
const MAX_CALLBACK_ID_BYTES = 64 * 1024;
const ajv = new Ajv({ strict: false });
ajv.addFormat("int64", { type: "number", validate: Number.isSafeInteger });
ajv.addFormat("uint64", {
  type: "number",
  validate: (n: number) => Number.isSafeInteger(n) && n >= 0,
});
const validators = new Map([
  ["item/commandExecution/requestApproval", ajv.compile(commandSchema)],
  ["item/fileChange/requestApproval", ajv.compile(fileSchema)],
  ["item/tool/requestUserInput", ajv.compile(inputSchema)],
]);
const exec = promisify(execFile);
const string = (v: unknown): v is string =>
  typeof v === "string" && v.length > 0;
const sameRoots = (a: unknown, b: string[]) =>
  Array.isArray(a) &&
  a.every(string) &&
  JSON.stringify([...new Set(a)].sort()) ===
    JSON.stringify([...new Set(b)].sort());

/** Local executable selection is trusted startup configuration, never a remote command. */
export class CodexAdapter implements HarnessAdapter {
  private rpc?: JsonRpcProcess;
  private transportFailed = false;
  private historyReader?: CodexAdapter;
  private boot?: Promise<void>;
  private queue = new BoundedQueue<AdapterEvent>(32);
  private consuming = false;
  private session?: { id: string; options: SessionOptions };
  private opening = false;
  private observedTurnId?: string;
  private recoveredTarget?: { sessionId: string; turnId: string };
  private active?: string; // 'starting' also quarantines ambiguous native acceptance.
  private requests = new Map<
    NativeRequestId,
    { method: string; params: Record<string, any> }
  >();
  // Tombstones last for this native process, including answered/denied callbacks.
  private seenRequestIds = new Set<NativeRequestId>();
  private seenRequestIdBytes = 0;
  private executable: { command: string; args?: string[] };
  constructor(
    executable: { command: string; args?: string[] } = {
      command: "codex",
    },
  ) {
    this.executable = executable;
  }
  get queueHighWaterMark(): number {
    return this.queue.highWaterMark;
  }
  async probe(): Promise<Capabilities> {
    let stdout: string;
    try {
      ({ stdout } = await exec(
        this.executable.command,
        [...(this.executable.args ?? []), "--version"],
        { timeout: 5000, maxBuffer: 4096, encoding: "utf8" },
      ));
    } catch {
      throw new Error("Codex version probe failed");
    }
    if (stdout.trim() !== `codex-cli ${SUPPORTED_CODEX_VERSION}`)
      throw new Error("unsupported Codex version; expected 0.153.4");
    return {
      resume: true,
      stream: true,
      approval: true,
      input: true,
      cancel: true,
      MCP: true,
      usage: true,
      publish: false,
    };
  }
  private initialize(): Promise<void> {
    this.boot ??= (async () => {
      await this.probe();
      this.rpc = new JsonRpcProcess(
        this.executable.command,
        [...(this.executable.args ?? []), "app-server"],
        (m) => this.receive(m),
        (e) => {
          this.transportFailed = true;
          this.queue.fail(e);
        },
      );
      const reply = await this.rpc.call("initialize", {
        clientInfo: { name: "nous_harness_bridge", version: "0.1.0" },
        capabilities: { experimentalApi: true },
      });
      if (
        !record(reply) ||
        !string(reply.userAgent) ||
        !reply.userAgent.includes("/0.153.4")
      )
        throw new Error("unsupported Codex handshake");
      await this.rpc.notify("initialized");
    })();
    return this.boot;
  }
  private async options(value: SessionOptions): Promise<SessionOptions> {
    const p = value.policy;
    if (
      !string(value.workspaceId) ||
      !isAbsolute(value.cwd) ||
      !p ||
      p.sandbox !== "workspace-write" ||
      p.approvalPolicy !== "on-request" ||
      p.reviewer !== "user" ||
      p.networkAccess !== false ||
      !Array.isArray(p.writableRoots) ||
      !p.writableRoots.length
    )
      throw new Error("policy mismatch: invalid local options");
    const cwd = await realpath(value.cwd);
    const roots = await Promise.all(
      p.writableRoots.map(async (r) => {
        if (!isAbsolute(r))
          throw new Error("policy mismatch: root must be absolute");
        return realpath(r);
      }),
    );
    // Codex always includes cwd as a writable root. Refuse a policy that omits it.
    if (!roots.includes(cwd))
      throw new Error("policy mismatch: cwd missing from writable roots");
    if (value.mcpConfig)
      for (const [name, config] of Object.entries(value.mcpConfig)) {
        if (
          !/^[a-zA-Z0-9_-]+$/.test(name) ||
          !string(config.command) ||
          !Array.isArray(config.args) ||
          !config.args.every((a) => typeof a === "string") ||
          Object.keys(config).some((k) => !["command", "args"].includes(k))
        )
          throw new Error("invalid local MCP configuration");
      }
    return structuredClone({
      ...value,
      cwd,
      policy: { ...p, writableRoots: [...new Set(roots)] },
    });
  }
  private sandbox(options: SessionOptions) {
    return {
      type: "workspaceWrite",
      writableRoots: options.policy.writableRoots,
      networkAccess: false,
      excludeTmpdirEnvVar: true,
      excludeSlashTmp: true,
    };
  }
  private verify(
    reply: unknown,
    options: SessionOptions,
    expectedId?: string,
  ): string {
    if (
      !record(reply) ||
      !record(reply.thread) ||
      !string(reply.thread.id) ||
      (expectedId && reply.thread.id !== expectedId) ||
      reply.cwd !== options.cwd ||
      reply.approvalPolicy !== "on-request" ||
      reply.approvalsReviewer !== "user" ||
      !record(reply.sandbox) ||
      reply.sandbox.type !== "workspaceWrite" ||
      reply.sandbox.networkAccess !== false ||
      reply.sandbox.excludeTmpdirEnvVar !== true ||
      reply.sandbox.excludeSlashTmp !== true ||
      !sameRoots(reply.sandbox.writableRoots, options.policy.writableRoots)
    )
      throw new Error("policy mismatch: effective Codex permissions");
    if (
      Array.isArray(reply.thread.turns) &&
      reply.thread.turns.some((t: any) => t.status === "inProgress")
    )
      throw new Error("session has active turn; reconciliation required");
    return reply.thread.id;
  }
  private async open(
    options: SessionOptions,
    id?: string,
  ): Promise<NativeSession> {
    if (this.session || this.opening)
      throw new Error("session already open or ambiguous");
    this.opening = true;
    try {
      const local = await this.options(options);
      await this.initialize();
      const config: Record<string, unknown> = {
        sandbox_workspace_write: {
          writable_roots: local.policy.writableRoots,
          network_access: false,
          exclude_tmpdir_env_var: true,
          exclude_slash_tmp: true,
        },
      };
      if (local.mcpConfig) config.mcp_servers = local.mcpConfig;
      const reply = await this.rpc!.call(
        id ? "thread/resume" : "thread/start",
        {
          ...(id ? { threadId: id } : {}),
          cwd: local.cwd,
          approvalPolicy: "on-request",
          approvalsReviewer: "user",
          sandbox: "workspace-write",
          config,
        },
      );
      const sessionId = this.verify(reply, local, id);
      this.session = { id: sessionId, options: local };
      return { id: sessionId };
    } finally {
      this.opening = false;
    }
  }
  startSession(options: SessionOptions): Promise<NativeSession> {
    return this.open(options);
  }
  resumeSession(id: string, options: SessionOptions): Promise<NativeSession> {
    return this.open(options, id);
  }
  async startTurn(
    sessionId: string,
    input: string,
    commandId: string,
  ): Promise<NativeTurn> {
    if (!this.session || this.session.id !== sessionId)
      throw new Error("no verified session");
    if (!this.consuming)
      throw new Error("event consumer required before turn/start");
    if (this.active)
      throw new Error("active or ambiguous turn; reconciliation required");
    if (!string(commandId) || !string(input))
      throw new Error("input and command ID required");
    this.active = "starting";
    this.observedTurnId = undefined;
    const p = this.session.options;
    const reply = await this.rpc!.call("turn/start", {
      threadId: sessionId,
      clientUserMessageId: commandId,
      input: [{ type: "text", text: input, text_elements: [] }],
      cwd: p.cwd,
      approvalPolicy: "on-request",
      approvalsReviewer: "user",
      sandboxPolicy: this.sandbox(p),
    });
    if (!record(reply) || !record(reply.turn) || !string(reply.turn.id))
      throw new TransportLoss("invalid turn/start acknowledgment");
    if (this.observedTurnId && this.observedTurnId !== reply.turn.id) {
      const error = new TransportLoss("turn acknowledgment identity mismatch");
      this.rpc!.fail(error);
      throw error;
    }
    if (this.active) this.active = reply.turn.id;
    return { id: reply.turn.id };
  }
  async interruptTurn(sessionId: string, turnId: string): Promise<void> {
    if (this.transportFailed && this.historyReader)
      return this.historyReader.interruptTurn(sessionId, turnId);
    if (
      (this.session?.id !== sessionId ||
        (this.active !== turnId && this.observedTurnId !== turnId)) &&
      (this.recoveredTarget?.sessionId !== sessionId ||
        this.recoveredTarget?.turnId !== turnId)
    )
      throw new Error("unknown active turn");
    await this.rpc!.call("turn/interrupt", { threadId: sessionId, turnId });
    // Receipt is not terminal evidence. Keep active until turn/completed.
  }
  async respondToRequest(
    id: NativeRequestId,
    response: NativeResponse,
  ): Promise<void> {
    const request = this.requests.get(id);
    if (!request) throw new Error("unknown or stale native request");
    let result: object;
    if (request.method === "item/tool/requestUserInput") {
      if (response.kind !== "answers" || !record(response.answers))
        throw new Error("input response required");
      const ids = request.params.questions.map((q: any) => q.id);
      if (
        Object.keys(response.answers).length !== ids.length ||
        ids.some(
          (key: string) =>
            !Array.isArray(response.answers[key]) ||
            !response.answers[key].every((a) => typeof a === "string"),
        )
      )
        throw new Error("invalid question answers");
      result = {
        answers: Object.fromEntries(
          Object.entries(response.answers).map(([key, answers]) => [
            key,
            { answers },
          ]),
        ),
      };
    } else {
      if (response.kind !== "decision" || typeof response.allow !== "boolean")
        throw new Error("decision required");
      result = { decision: response.allow ? "accept" : "decline" };
    }
    this.requests.delete(id); // A failed send is ambiguous; never replay the callback.
    await this.rpc!.respond(id, result);
  }
  async *events(signal: AbortSignal): AsyncIterable<AdapterEvent> {
    if (this.consuming) throw new Error("only one event consumer supported");
    this.consuming = true;
    try {
      while (!signal.aborted) {
        const next = await this.queue.next(signal);
        if (next.done) return;
        yield next.value;
      }
    } finally {
      this.consuming = false;
      if (this.active)
        this.rpc?.fail(
          new TransportLoss("event consumer detached during active turn"),
        );
    }
  }
  async inspectTurn(
    sessionId: string,
    commandId: string,
  ): Promise<NativeObservation> {
    if (this.transportFailed) {
      // A dead app-server is not evidence its native child stopped. Open only a
      // history reader; do not resume a session or issue another turn/start.
      await this.historyReader?.closeSession();
      this.historyReader = new CodexAdapter(this.executable);
      return this.historyReader.inspectTurn(sessionId, commandId);
    }
    await this.initialize();
    const reply = await this.rpc!.call("thread/read", {
      threadId: sessionId,
      includeTurns: true,
    });
    const unknown: NativeObservation = {
      state: "unknown",
      sessionId,
      turnId: null,
    };
    if (
      !record(reply) ||
      !record(reply.thread) ||
      reply.thread.id !== sessionId ||
      !Array.isArray(reply.thread.turns)
    )
      return unknown;
    const matches = reply.thread.turns.filter(
      (turn: any) =>
        record(turn) &&
        turn.itemsView === "full" &&
        Array.isArray(turn.items) &&
        turn.items.some(
          (item: any) =>
            record(item) &&
            item.type === "userMessage" &&
            item.clientId === commandId,
        ),
    );
    if (matches.length !== 1) return unknown;
    const turn = matches[0];
    if (
      !string(turn.id) ||
      !["inProgress", "completed", "failed", "interrupted"].includes(
        turn.status,
      )
    )
      return unknown;
    const texts = turn.items.filter(
      (item: any) => item.type === "agentMessage",
    );
    if (texts.some((item: any) => typeof item.text !== "string"))
      return unknown;
    if (turn.status === "inProgress")
      this.recoveredTarget = { sessionId, turnId: turn.id };
    return {
      state: turn.status === "inProgress" ? "running" : turn.status,
      sessionId,
      turnId: turn.id,
      assistantText: texts.map((item: any) => item.text).join(""),
    };
  }
  async closeSession(): Promise<void> {
    await this.historyReader?.closeSession();
    this.requests.clear();
    this.seenRequestIds.clear();
    this.seenRequestIdBytes = 0;
    this.rpc?.close();
    this.session = undefined;
  }
  private rememberRequestId(id: NativeRequestId): void {
    if (this.seenRequestIds.has(id)) return;
    const bytes = typeof id === "string" ? Buffer.byteLength(id) : 8;
    if (
      this.seenRequestIds.size >= MAX_SEEN_CALLBACKS ||
      this.seenRequestIdBytes + bytes > MAX_CALLBACK_ID_BYTES
    )
      throw new TransportLoss("native callback identity capacity exceeded");
    this.seenRequestIds.add(id);
    this.seenRequestIdBytes += bytes;
  }
  private async receive(message: RpcMessage): Promise<void> {
    const p = message.params;
    if (message.id !== undefined) {
      if (this.seenRequestIds.has(message.id))
        throw new TransportLoss("duplicate native callback ID");
      this.rememberRequestId(message.id);
      const validate = validators.get(message.method!);
      const supported =
        validate &&
        validate(p) &&
        record(p) &&
        this.session?.id === p.threadId &&
        this.active &&
        (this.active === "starting" || this.active === p.turnId) &&
        this.consuming &&
        this.requests.size < 32 &&
        !this.requests.has(message.id);
      // Persistent grants/amendments are outside the one-shot response contract.
      if (
        !supported ||
        !record(p) ||
        p.grantRoot != null ||
        p.proposedExecpolicyAmendment != null ||
        p.proposedNetworkPolicyAmendments != null ||
        p.networkApprovalContext != null
      ) {
        await this.rpc!.deny(message.id);
        return;
      }
      if (this.observedTurnId && this.observedTurnId !== p.turnId) {
        await this.rpc!.deny(message.id);
        return;
      }
      this.observedTurnId = p.turnId as string;
      this.requests.set(message.id, { method: message.method!, params: p });
      await this.queue.push({
        kind: "request",
        sessionId: p.threadId as string,
        turnId: p.turnId as string,
        itemId: p.itemId as string,
        requestId: message.id,
        ...(string(p.approvalId) ? { approvalId: p.approvalId } : {}),
        method: message.method!,
        params: p,
      });
      return;
    }
    // This pinned notification has no turnId and must precede turn filtering.
    if (message.method === "serverRequest/resolved") {
      if (
        record(p) &&
        this.session &&
        p.threadId === this.session.id &&
        (typeof p.requestId === "string" || Number.isSafeInteger(p.requestId))
      ) {
        this.rememberRequestId(p.requestId);
        this.requests.delete(p.requestId);
      }
      return;
    }
    if (!record(p) || p.threadId !== this.session?.id || !this.active) return;
    const turnId =
      message.method === "turn/completed" && record(p.turn)
        ? p.turn.id
        : p.turnId;
    if (
      !string(turnId) ||
      (this.active !== "starting" && this.active !== turnId)
    )
      return;
    if (this.observedTurnId && this.observedTurnId !== turnId) return;
    this.observedTurnId = turnId;
    const position = { sessionId: p.threadId as string, turnId };
    if (
      message.method === "item/agentMessage/delta" &&
      typeof p.delta === "string"
    ) {
      // JSON escaping can expand control characters sixfold. Count the actual
      // serialized event, including identities, while preserving Unicode points.
      const overhead = Buffer.byteLength(
        JSON.stringify({ ...position, kind: "delta", text: "" }),
      );
      const textBudget = MAX_SERIALIZED_DELTA_BYTES - overhead;
      if (textBudget < 6)
        throw new TransportLoss("delta identity exceeds event budget");
      let text = "",
        rawBytes = 0,
        serializedBytes = 0;
      for (const character of p.delta) {
        const rawSize = Buffer.byteLength(character);
        const serializedSize = Buffer.byteLength(JSON.stringify(character)) - 2;
        if (
          rawBytes + rawSize > 8192 ||
          serializedBytes + serializedSize > textBudget
        ) {
          await this.queue.push({ ...position, kind: "delta", text });
          text = "";
          rawBytes = 0;
          serializedBytes = 0;
        }
        text += character;
        rawBytes += rawSize;
        serializedBytes += serializedSize;
      }
      if (text) await this.queue.push({ ...position, kind: "delta", text });
    } else if (
      message.method === "turn/completed" &&
      record(p.turn) &&
      ["completed", "failed", "interrupted"].includes(p.turn.status)
    ) {
      this.active = undefined;
      this.requests.clear();
      await this.queue.push({
        ...position,
        kind: "terminal",
        status: p.turn.status,
      });
    } else if (
      message.method === "thread/tokenUsage/updated" &&
      record(p.tokenUsage) &&
      record(p.tokenUsage.last)
    ) {
      const { inputTokens, outputTokens } = p.tokenUsage.last;
      if (
        Number.isSafeInteger(inputTokens) &&
        inputTokens >= 0 &&
        Number.isSafeInteger(outputTokens) &&
        outputTokens >= 0
      )
        await this.queue.push({
          ...position,
          kind: "usage",
          inputTokens,
          outputTokens,
        });
    } else if (
      ["item/started", "item/completed"].includes(message.method!) &&
      record(p.item) &&
      string(p.item.id) &&
      [
        "commandExecution",
        "fileChange",
        "mcpToolCall",
        "dynamicToolCall",
      ].includes(p.item.type)
    ) {
      const status = ["completed", "failed", "declined"].includes(p.item.status)
        ? p.item.status
        : undefined;
      await this.queue.push({
        ...position,
        kind: "tool",
        itemId: p.item.id,
        name: string(p.item.tool) ? p.item.tool : p.item.type,
        phase: message.method === "item/started" ? "started" : "completed",
        ...(status ? { status } : {}),
      });
    }
  }
}
