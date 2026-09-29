import { spawn, type ChildProcessWithoutNullStreams } from "node:child_process";
import type { NativeRequestId } from "./contracts.ts";

export const MAX_FRAME_BYTES = 1024 * 1024;
export type RpcMessage = {
  id?: NativeRequestId;
  method?: string;
  params?: unknown;
  result?: unknown;
  error?: unknown;
};
export function record(value: unknown): value is Record<string, any> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}
export class TransportLoss extends Error {
  constructor(reason: string) {
    super(`Codex transport lost: ${reason}`);
  }
}

/** A failed termination is not permission to replay a native side effect. */
export class NativeExitUnconfirmed extends TransportLoss {
  constructor() {
    super("app-server exit unconfirmed; recovery required");
  }
}
const TERMINATION_GRACE_MS = 250;
const EXIT_CONFIRMATION_MS = 1000;

/** A single producer/consumer queue. Awaiting push pauses stdout reads. */
export class BoundedQueue<T> {
  private values: T[] = [];
  private wake?: () => void;
  private space?: () => void;
  private failure?: Error;
  highWaterMark = 0;
  private capacity: number;
  constructor(capacity = 32) {
    this.capacity = capacity;
  }
  async push(value: T): Promise<void> {
    while (this.values.length >= this.capacity && !this.failure)
      await new Promise<void>((r) => {
        this.space = r;
      });
    if (this.failure) throw this.failure;
    this.values.push(value);
    this.highWaterMark = Math.max(this.highWaterMark, this.values.length);
    this.wake?.();
    this.wake = undefined;
  }
  fail(error: Error): void {
    this.failure = error;
    this.wake?.();
    this.space?.();
  }
  async next(signal: AbortSignal): Promise<IteratorResult<T>> {
    while (!signal.aborted) {
      if (this.failure) throw this.failure;
      const value = this.values.shift();
      if (value !== undefined) {
        this.space?.();
        this.space = undefined;
        return { value, done: false };
      }
      await new Promise<void>((resolve) => {
        const done = () => {
          signal.removeEventListener("abort", done);
          resolve();
        };
        this.wake = done;
        signal.addEventListener("abort", done, { once: true });
        if (signal.aborted) done();
      });
    }
    return { value: undefined, done: true };
  }
}

export class JsonRpcProcess {
  private child: ChildProcessWithoutNullStreams;
  private nextId = 1;
  private exitConfirmed = false;
  private resolveExit!: () => void;
  private exited = new Promise<void>((resolve) => {
    this.resolveExit = resolve;
  });
  private termination?: Promise<void>;
  private pending = new Map<
    NativeRequestId,
    {
      resolve: (v: unknown) => void;
      reject: (e: Error) => void;
      timer: NodeJS.Timeout;
    }
  >();
  private failure?: Error;
  private writing = false;
  private incoming: (message: RpcMessage) => Promise<void>;
  private onFailure: (e: Error) => void;
  constructor(
    command: string,
    args: string[],
    incoming: (message: RpcMessage) => Promise<void>,
    onFailure: (e: Error) => void,
  ) {
    this.incoming = incoming;
    this.onFailure = onFailure;
    this.child = spawn(command, args, {
      stdio: ["pipe", "pipe", "pipe"],
      shell: false,
    });
    // Do not log provider output: it can contain prompts or credentials.
    this.child.stderr.resume();
    this.child.on("error", () => {
      // A failed spawn has no child whose exit could be observed.
      if (this.child.pid === undefined) this.confirmExit();
      this.fail(new TransportLoss("process spawn failed"));
    });
    this.child.on("exit", () => {
      this.confirmExit();
      this.fail(new TransportLoss("process exited"));
    });
    this.child.stdin.on("error", () =>
      this.fail(new TransportLoss("stdin failed")),
    );
    void this.read().catch((error) =>
      this.fail(
        error instanceof Error ? error : new TransportLoss("invalid stream"),
      ),
    );
  }
  private async read(): Promise<void> {
    let pending = Buffer.alloc(0);
    for await (const raw of this.child.stdout) {
      const chunk = Buffer.from(raw);
      let offset = 0;
      while (offset < chunk.length) {
        const newline = chunk.indexOf(10, offset);
        const end = newline === -1 ? chunk.length : newline;
        if (pending.length + end - offset > MAX_FRAME_BYTES)
          throw new TransportLoss("frame exceeds 1 MiB");
        pending = Buffer.concat([pending, chunk.subarray(offset, end)]);
        offset = end + 1;
        if (newline === -1) break;
        let message: unknown;
        try {
          message = JSON.parse(
            new TextDecoder("utf-8", { fatal: true }).decode(pending),
          );
        } catch {
          throw new TransportLoss("malformed JSON frame");
        }
        pending = Buffer.alloc(0);
        if (
          !record(message) ||
          (message.id !== undefined &&
            typeof message.id !== "string" &&
            !Number.isSafeInteger(message.id))
        )
          throw new TransportLoss("invalid JSON-RPC frame");
        if (typeof message.method === "string") await this.incoming(message);
        else {
          const call = this.pending.get(message.id);
          if (!call || "result" in message === "error" in message)
            throw new TransportLoss("unexpected RPC response");
          this.pending.delete(message.id);
          clearTimeout(call.timer);
          // Never expose native error data, which can contain secrets.
          if ("error" in message) call.reject(new Error("Codex RPC rejected"));
          else call.resolve(message.result);
        }
      }
    }
    throw new TransportLoss(pending.length ? "EOF within frame" : "stdout EOF");
  }
  private async write(message: object): Promise<void> {
    if (this.failure) throw this.failure;
    const data = JSON.stringify(message) + "\n";
    if (Buffer.byteLength(data) > MAX_FRAME_BYTES)
      throw new Error("RPC frame exceeds 1 MiB");
    // Reject concurrent saturation instead of retaining an unbounded write queue.
    if (this.writing) throw new TransportLoss("stdin backpressure");
    this.writing = true;
    try {
      await new Promise<void>((resolve, reject) =>
        this.child.stdin.write(data, (error) =>
          error ? reject(new TransportLoss("stdin failed")) : resolve(),
        ),
      );
    } finally {
      this.writing = false;
    }
  }
  async call(method: string, params: object): Promise<unknown> {
    if (this.failure) {
      await this.terminate();
      throw this.failure;
    }
    if (this.pending.size >= 32)
      throw new Error("too many pending RPC requests");
    const id = this.nextId++;
    try {
      return await new Promise((resolve, reject) => {
        const timer = setTimeout(
          () =>
            this.fail(
              new TransportLoss(
                "RPC acknowledgment timeout; execution may be ambiguous",
              ),
            ),
          30_000,
        );
        this.pending.set(id, { resolve, reject, timer });
        void this.write({ id, method, params }).catch((error) =>
          this.fail(error),
        );
      });
    } catch (error) {
      // Pending RPCs fail immediately internally, but callers may only retry
      // after the failed transport's child is confirmed gone.
      if (this.failure) await this.terminate();
      throw error;
    }
  }
  notify(method: string): Promise<void> {
    return this.write({ method });
  }
  respond(id: NativeRequestId, result: unknown): Promise<void> {
    return this.write({ id, result });
  }
  deny(id: NativeRequestId): Promise<void> {
    return this.write({
      id,
      error: { code: -32601, message: "Unsupported or invalid request" },
    });
  }
  private confirmExit(): void {
    this.exitConfirmed = true;
    this.resolveExit();
  }
  private terminate(): Promise<void> {
    if (this.exitConfirmed) return Promise.resolve();
    this.termination ??= new Promise<void>((resolve, reject) => {
      const signal = (value: NodeJS.Signals) => {
        try {
          this.child.kill(value);
        } catch {
          /* Exit, not kill(), is authoritative. */
        }
      };
      const escalate = setTimeout(
        () => signal("SIGKILL"),
        TERMINATION_GRACE_MS,
      );
      const deadline = setTimeout(
        () => reject(new NativeExitUnconfirmed()),
        TERMINATION_GRACE_MS + EXIT_CONFIRMATION_MS,
      );
      void this.exited.then(() => {
        clearTimeout(escalate);
        clearTimeout(deadline);
        resolve();
      });
      signal("SIGTERM");
    });
    return this.termination;
  }
  fail(error: Error): void {
    if (this.failure) return;
    this.failure = error;
    // Start supervision even when no RPC caller is waiting (e.g. shutdown).
    void this.terminate().catch(() => {});
    for (const call of this.pending.values()) {
      clearTimeout(call.timer);
      call.reject(error);
    }
    this.pending.clear();
    this.onFailure(error);
  }
  async close(): Promise<void> {
    this.fail(new TransportLoss("session closed"));
    await this.terminate();
  }
}
