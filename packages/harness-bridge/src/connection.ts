import type {
  HarnessAdapter,
  NativeRequestId,
  NativeResponse,
} from "./contracts.ts";
import type { IntegrationCredentials } from "./credentials.ts";
import { integrationHeaders } from "./credentials.ts";
import { Journal } from "./journal.ts";

export type BridgeIdentity = {
  deviceId: string;
  runId: string;
  commandId: string;
  workspaceId: string;
  generation: number;
};
export type BridgeCommand = BridgeIdentity & {
  expiresAt: string;
  body:
    | { kind: "start"; input: string; sessionId?: string }
    | { kind: "interrupt"; sessionId: string; turnId: string }
    | {
        kind: "respond";
        requestId: NativeRequestId;
        response: NativeResponse;
        approvalRecordId: string;
      };
};
export type ProducerBody =
  | { kind: "command_ack"; approvalRecordId: string }
  | {
      kind: "request";
      sessionId: string;
      turnId: string;
      itemId: string;
      requestId: NativeRequestId;
      approvalId?: string;
      method: string;
      params: Record<string, unknown>;
    }
  | { kind: "event"; eventType: "assistant.delta"; payload: { text: string } }
  | {
      kind: "event";
      eventType: "tool.started";
      payload: {
        tool_call_id: string;
        name: string;
        args?: Record<string, unknown>;
      };
    }
  | {
      kind: "event";
      eventType: "tool.completed";
      payload: {
        tool_call_id: string;
        name: string;
        status: string;
        result_preview?: string;
        error?: string;
      };
    }
  | {
      kind: "event";
      eventType: "approval.required";
      payload: {
        approval_id: string;
        tool_call_id: string;
        name: string;
        args?: Record<string, unknown>;
      };
    }
  | {
      kind: "event";
      eventType: "approval.resolved";
      payload: { approval_id: string; decision: string };
    }
  | {
      kind: "event";
      eventType: "usage.updated";
      payload: {
        input_tokens?: number;
        output_tokens?: number;
        total_tokens?: number;
      };
    };
export type BridgeEvent = BridgeIdentity & {
  sourceId: string;
  sourceSeq: number;
  body:
    | ProducerBody
    | {
        kind: "observation";
        state: "unknown" | "running" | "completed" | "failed" | "interrupted";
        sessionId: string;
        turnId: string | null;
      };
};
export type Ack = {
  runId: string;
  sourceId: string;
  sourceSeq: number;
  generation: number;
  canonicalSeq: number;
};
/** NOUS refused this run's events while the device grant stayed valid. */
export type Rejection = Omit<Ack, "canonicalSeq"> & { code: "run_access_denied" };
const uuid = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
const obj = (v: unknown): v is Record<string, any> =>
  typeof v === "object" && v !== null && !Array.isArray(v);
const exact = (v: Record<string, any>, keys: string[]) =>
  Object.keys(v).every((k) => keys.includes(k));
export function parseCommand(value: unknown): BridgeCommand {
  if (
    !obj(value) ||
    !exact(value, [
      "deviceId",
      "runId",
      "commandId",
      "workspaceId",
      "generation",
      "expiresAt",
      "body",
    ]) ||
    ![value.deviceId, value.runId, value.commandId, value.workspaceId].every(
      (x) => typeof x === "string" && uuid.test(x),
    ) ||
    !Number.isSafeInteger(value.generation) ||
    value.generation < 1 ||
    typeof value.expiresAt !== "string" ||
    !/(Z|\+00:00)$/.test(value.expiresAt) ||
    !Number.isFinite(Date.parse(value.expiresAt)) ||
    !obj(value.body)
  )
    throw new Error("invalid bridge command");
  const b = value.body;
  const id = (v: any) =>
    typeof v === "string" && v.length > 0 && v.length <= 255;
  if (b.kind === "start") {
    if (
      !exact(b, ["kind", "input", "sessionId"]) ||
      typeof b.input !== "string" ||
      !b.input.length ||
      b.input.length > 65536 ||
      (b.sessionId !== undefined && b.sessionId !== null && !id(b.sessionId))
    )
      throw new Error("invalid start");
  } else if (b.kind === "interrupt") {
    if (
      !exact(b, ["kind", "sessionId", "turnId"]) ||
      !id(b.sessionId) ||
      !id(b.turnId)
    )
      throw new Error("invalid interrupt");
  } else if (b.kind === "respond") {
    if (
      !exact(b, ["kind", "requestId", "response", "approvalRecordId"]) ||
      !(typeof b.requestId === "string" || Number.isSafeInteger(b.requestId)) ||
      !uuid.test(b.approvalRecordId) ||
      !obj(b.response)
    )
      throw new Error("invalid response");
    const r = b.response;
    if (r.kind === "decision") {
      if (!exact(r, ["kind", "allow"]) || typeof r.allow !== "boolean")
        throw new Error("invalid decision");
    } else if (r.kind === "answers") {
      if (
        !exact(r, ["kind", "answers"]) ||
        !obj(r.answers) ||
        !Object.values(r.answers).every(
          (v) => Array.isArray(v) && v.every((x) => typeof x === "string"),
        )
      )
        throw new Error("invalid answers");
    } else throw new Error("invalid response kind");
  } else throw new Error("invalid command kind");
  return structuredClone(value) as BridgeCommand;
}

/** One connection attempt. Call again after loss with the same durable Journal.
 * It owns no native process shutdown: losing a parent is not terminal evidence.
 */
export async function connectBridge(options: {
  url: string;
  deviceId: string;
  credentials: IntegrationCredentials;
  journal: Journal;
  adapterFor: (workspaceId: string, runId: string) => HarnessAdapter;
  signal: AbortSignal;
  // Test seam; production uses the defaults below.
  timing?: { pollMs?: number; livenessMs?: number; connectMs?: number };
}): Promise<void> {
  const pollMs = options.timing?.pollMs ?? 5000;
  // The server answers every poll, so silence for three poll periods means
  // the peer is gone even if no close frame ever arrives (a backend restart
  // left a half-open socket and the bridge sat idle forever). Undici's
  // WebSocket has no idle or connect timeout of its own.
  const livenessMs = options.timing?.livenessMs ?? pollMs * 3;
  const connectMs = options.timing?.connectMs ?? 15_000;
  const url = new URL(options.url);
  const loopbackWs =
    url.protocol === "ws:" &&
    ["localhost", "127.0.0.1", "[::1]"].includes(url.hostname);
  if (
    (url.protocol !== "wss:" && !loopbackWs) ||
    url.username ||
    url.password ||
    url.search
  )
    throw new Error("WSS (or local loopback ws) without URL credentials required");
  // Node 24's builtin Undici WebSocket accepts a WebSocketInit with headers.
  const Socket = WebSocket as unknown as new (
    url: string,
    init: { headers: Record<string, string> },
  ) => WebSocket;
  const socket = new Socket(url.href, {
    headers: integrationHeaders(options.credentials),
  });
  const journal = options.journal;
  let chain = Promise.resolve();
  const controllers = new Map<string, AbortController>();
  const sent = new Set<string>();
  const sendPending = () => {
    for (const event of journal.pending(64)) {
      const key = `${event.sourceId}/${event.sourceSeq}`;
      if (!sent.has(key) && socket.readyState === WebSocket.OPEN) {
        socket.send(JSON.stringify(event));
        sent.add(key);
      }
    }
  };
  await new Promise<void>((resolve, reject) => {
    let interval: ReturnType<typeof setInterval> | undefined;
    let lastFrameAt = Date.now();
    const finish = (error?: Error) => {
      if (interval) clearInterval(interval);
      clearTimeout(connectTimer);
      options.signal.removeEventListener("abort", abort);
      socket.close();
      error ? reject(error) : resolve();
    };
    const abort = () => finish();
    options.signal.addEventListener("abort", abort, { once: true });
    const connectTimer = setTimeout(
      () => finish(new Error("bridge connect timeout")),
      connectMs,
    );
    socket.addEventListener("open", () => {
      clearTimeout(connectTimer);
      lastFrameAt = Date.now();
      socket.send(JSON.stringify({ poll: true, deviceId: options.deviceId }));
      interval = setInterval(() => {
        if (Date.now() - lastFrameAt > livenessMs) {
          finish(new Error("bridge liveness timeout"));
          return;
        }
        if (socket.readyState === WebSocket.OPEN) {
          socket.send(
            JSON.stringify({ poll: true, deviceId: options.deviceId }),
          );
          sendPending();
        }
      }, pollMs);
      sendPending();
      chain = chain
        .then(async () => {
          for (const c of journal.activeCommands()) {
            const a = options.adapterFor(c.workspaceId, c.runId);
            await journal.reconcile(c.commandId, a);
          }
        })
        .catch(finish);
    });
    socket.addEventListener("message", (message) => {
      lastFrameAt = Date.now();
      chain = chain
        .then(async () => {
          if (
            typeof message.data !== "string" ||
            Buffer.byteLength(message.data) > 1024 * 1024
          )
            throw new Error("invalid server frame");
          const value = JSON.parse(message.data);
          if (value.ack) {
            journal.acknowledge(value.ack);
            sent.delete(`${value.ack.sourceId}/${value.ack.sourceSeq}`);
            sendPending();
            return;
          }
          if (value.reject) {
            // NOUS refused this run's events while the grant stayed valid
            // (backend/src/api/harness.py): final for that run; keep serving
            // the device's other runs instead of reconnecting into it again.
            // journal.reject() checks the run id against its own command, so
            // the id logged is never raw server text. It returns false for a
            // run already refused or finished: one notice per run.
            if (journal.reject(value.reject)) {
              // reject() releases the folder only once the run's terminal
              // observation is journaled; mid-run the run keeps its lock.
              const reserved = journal
                .activeCommands()
                .some((c) => c.commandId === value.reject.sourceId);
              console.error(
                reserved
                  ? `NOUS refused run ${value.reject.runId}: its output is no longer uploaded, and its folder stays reserved until you disconnect, connect and register it again.`
                  : `NOUS refused run ${value.reject.runId}: its output is no longer uploaded. The run had already finished on this device, so its folder is free.`,
              );
            }
            sent.delete(`${value.reject.sourceId}/${value.reject.sourceSeq}`);
            sendPending();
            return;
          }
          if (
            !obj(value) ||
            !Array.isArray(value.commands) ||
            !obj(value.lease) ||
            value.lease.deviceId !== options.deviceId ||
            !obj(value.lease.runs) ||
            !Object.entries(value.lease.runs).every(
              ([id, generation]) =>
                uuid.test(id) &&
                Number.isSafeInteger(generation) &&
                (generation as number) > 0,
            )
          )
            throw new Error("invalid lease response");
          journal.renewLease(
            options.deviceId,
            value.lease.expiresAt,
            value.lease.runs,
          );
          for (const raw of value.commands) {
            const c = parseCommand(raw);
            if (c.deviceId !== options.deviceId)
              throw new Error("wrong device");
            const a = options.adapterFor(c.workspaceId, c.runId);
            if (c.body.kind === "start" && !controllers.has(c.runId)) {
              const controller = new AbortController();
              controllers.set(c.runId, controller);
              void journal
                .consume(c.commandId, a, controller.signal)
                .catch(() => journal.quarantine(c.commandId));
            }
            await journal.execute(c, a);
          }
          for (const c of journal.activeCommands()) {
            const state = journal.state(c.commandId);
            // A refused run makes no Codex call; reconcile only re-arms its
            // watchdog.
            if (state === "recovering" || state === "denied")
              await journal.reconcile(
                c.commandId,
                options.adapterFor(c.workspaceId, c.runId),
              );
          }
          sendPending();
        })
        .catch((error) =>
          finish(error instanceof Error ? error : new Error("bridge failure")),
        );
    });
    socket.addEventListener("error", () =>
      finish(new Error("bridge disconnected")),
    );
    socket.addEventListener("close", () => finish());
    if (options.signal.aborted) abort();
  });
}
