import { DatabaseSync } from "node:sqlite";
import { closeSync, constants, lstatSync, openSync } from "node:fs";
import { dirname } from "node:path";
import { createHash } from "node:crypto";
import type {
  AdapterEvent,
  HarnessAdapter,
  SessionOptions,
} from "./contracts.ts";
import {
  parseCommand,
  type Ack,
  type BridgeCommand,
  type BridgeEvent,
} from "./connection.ts";

type Row = {
  id: string;
  command: string;
  digest: string;
  state: string;
  session: string | null;
  turn: string | null;
  seq: number;
  acked: number;
  cursor: number;
};
const hash = (value: unknown) =>
  createHash("sha256").update(JSON.stringify(value)).digest("hex");
const terminal = (state: string) =>
  ["completed", "failed", "interrupted"].includes(state);

/** FULL synchronous SQLite transactions precede every native side effect.
 * This journal is local authority, not a cache: losing it requires manual recovery.
 */
export class Journal {
  private db: DatabaseSync;
  private inTransaction = false;
  private closed = false;
  private timers = new Map<string, ReturnType<typeof setTimeout>>();
  private consumers = new WeakSet<HarnessAdapter>();
  constructor(
    path: string,
    private optionsFor: (workspaceId: string) => SessionOptions,
  ) {
    const dir = lstatSync(dirname(path));
    if (
      !dir.isDirectory() ||
      dir.isSymbolicLink() ||
      dir.uid !== process.getuid?.() ||
      (dir.mode & 0o077) !== 0
    )
      throw new Error("unsafe journal directory");
    const fd = openSync(
      path,
      constants.O_CREAT | constants.O_RDWR | constants.O_NOFOLLOW,
      0o600,
    );
    closeSync(fd);
    const st = lstatSync(path);
    if (
      !st.isFile() ||
      st.uid !== process.getuid?.() ||
      (st.mode & 0o077) !== 0
    )
      throw new Error("unsafe journal file");
    this.db = new DatabaseSync(path);
    this.db
      .exec(`PRAGMA synchronous=FULL; PRAGMA foreign_keys=ON; PRAGMA busy_timeout=5000;
      CREATE TABLE IF NOT EXISTS commands(id TEXT PRIMARY KEY, command TEXT NOT NULL, digest TEXT NOT NULL, state TEXT NOT NULL, session TEXT, turn TEXT, seq INTEGER NOT NULL DEFAULT 0, acked INTEGER NOT NULL DEFAULT 0, cursor INTEGER NOT NULL DEFAULT 0);
      CREATE TABLE IF NOT EXISTS locks(workspace TEXT PRIMARY KEY, command TEXT NOT NULL REFERENCES commands(id));
      CREATE TABLE IF NOT EXISTS events(command TEXT NOT NULL REFERENCES commands(id), seq INTEGER NOT NULL, event TEXT NOT NULL, acked INTEGER NOT NULL DEFAULT 0, PRIMARY KEY(command,seq));
      CREATE TABLE IF NOT EXISTS leases(run_id TEXT PRIMARY KEY, device TEXT NOT NULL, expires TEXT NOT NULL, blocked INTEGER NOT NULL DEFAULT 0);
      CREATE TABLE IF NOT EXISTS interrupts(session TEXT NOT NULL, turn TEXT NOT NULL, PRIMARY KEY(session,turn));
      UPDATE commands SET state='recovering' WHERE state IN ('intent','running');`);
  }
  close(): void {
    this.closed = true;
    for (const timer of this.timers.values()) clearTimeout(timer);
    this.timers.clear();
    this.db.close();
  }
  private row(id: string): Row | undefined {
    return this.db.prepare("SELECT * FROM commands WHERE id=?").get(id) as
      Row | undefined;
  }
  state(id: string): string {
    return this.row(id)?.state ?? "missing";
  }
  workspaceLocked(id: string): boolean {
    return !!this.db.prepare("SELECT 1 FROM locks WHERE workspace=?").get(id);
  }
  activeCommands(): BridgeCommand[] {
    return (
      this.db
        .prepare(
          "SELECT c.command FROM commands c JOIN locks l ON c.id=l.command",
        )
        .all() as { command: string }[]
    ).map((r) => JSON.parse(r.command));
  }
  quarantine(id: string): void {
    if (this.closed) return;
    this.db
      .prepare(
        "UPDATE commands SET state='recovering' WHERE id=? AND state NOT IN ('completed','failed','interrupted','terminal_pending')",
      )
      .run(id);
  }
  private tx<T>(fn: () => T): T {
    if (this.inTransaction) return fn();
    this.db.exec("BEGIN IMMEDIATE");
    this.inTransaction = true;
    try {
      const result = fn();
      this.db.exec("COMMIT");
      return result;
    } catch (e) {
      this.db.exec("ROLLBACK");
      throw e;
    } finally {
      this.inTransaction = false;
    }
  }
  private owner(command: BridgeCommand): Row | undefined {
    const lock = this.db
      .prepare("SELECT command FROM locks WHERE workspace=?")
      .get(command.workspaceId) as { command: string } | undefined;
    return lock ? this.row(lock.command) : undefined;
  }
  renewLease(
    deviceId: string,
    expiresAt: string,
    runs: Record<string, number>,
  ): void {
    if (
      !Number.isFinite(Date.parse(expiresAt)) ||
      Date.parse(expiresAt) <= Date.now()
    )
      throw new Error("invalid verified lease");
    for (const c of this.activeCommands()) {
      if (c.deviceId !== deviceId || runs[c.runId] !== c.generation) continue;
      this.db
        .prepare(
          "INSERT INTO leases(run_id,device,expires,blocked) VALUES(?,?,?,0) ON CONFLICT(run_id) DO UPDATE SET expires=excluded.expires,blocked=0",
        )
        .run(c.runId, deviceId, expiresAt);
    }
  }
  async execute(value: BridgeCommand, adapter: HarnessAdapter): Promise<void> {
    const command = parseCommand(value);
    const { expiresAt, ...identity } = command;
    const digest = hash(identity);
    const existing = this.row(command.commandId);
    if (existing) {
      if (existing.digest !== digest)
        throw new Error("command payload conflict");
      return;
    }
    const lease = this.db
      .prepare("SELECT expires,blocked FROM leases WHERE run_id=?")
      .get(command.runId) as { expires: string; blocked: number } | undefined;
    if (
      Date.parse(expiresAt) <= Date.now() ||
      (lease && (lease.blocked || Date.parse(lease.expires) <= Date.now()))
    )
      throw new Error("lease expired; verified renewal required");
    const b = command.body;
    this.tx(() => {
      const owner = this.owner(command);
      if (b.kind === "start") {
        if (owner) throw new Error("workspace quarantined");
      } else {
        if (!owner) throw new Error("no active target");
        const start: BridgeCommand = JSON.parse(owner.command);
        if (
          start.runId !== command.runId ||
          start.deviceId !== command.deviceId ||
          start.generation !== command.generation
        )
          throw new Error("generation or run mismatch");
        if (
          b.kind === "interrupt" &&
          (owner.session !== b.sessionId || owner.turn !== b.turnId)
        )
          throw new Error("interrupt target mismatch");
        if (b.kind === "respond" && owner.state !== "running")
          throw new Error("approval blocked during recovery");
      }
      this.db
        .prepare(
          "INSERT INTO commands(id,command,digest,state) VALUES(?,?,?,?)",
        )
        .run(command.commandId, JSON.stringify(command), digest, "intent");
      if (b.kind === "start")
        this.db
          .prepare("INSERT INTO locks(workspace,command) VALUES(?,?)")
          .run(command.workspaceId, command.commandId);
    });
    try {
      if (b.kind === "start") {
        const options = this.optionsFor(command.workspaceId);
        const session = b.sessionId
          ? await adapter.resumeSession(b.sessionId, options)
          : await adapter.startSession(options);
        this.db
          .prepare("UPDATE commands SET session=? WHERE id=?")
          .run(session.id, command.commandId);
        const turn = await adapter.startTurn(
          session.id,
          b.input,
          command.commandId,
        );
        const current = this.row(command.commandId)!;
        if (current.turn && current.turn !== turn.id)
          throw new Error("turn identity mismatch");
        this.db
          .prepare(
            "UPDATE commands SET turn=?,state=CASE WHEN state='intent' THEN 'running' ELSE state END WHERE id=?",
          )
          .run(turn.id, command.commandId);
        if (this.state(command.commandId) !== "terminal_pending")
          this.observe(command.commandId, "running", session.id, turn.id);
        void this.watchLease(expiresAt, adapter, session.id, turn.id).catch(
          () => this.quarantine(command.commandId),
        );
      } else if (b.kind === "interrupt") {
        await adapter.interruptTurn(b.sessionId, b.turnId);
        this.db
          .prepare("UPDATE commands SET state='delivered' WHERE id=?")
          .run(command.commandId);
        // Empty interrupt acknowledgement is not terminal evidence.
      } else {
        await adapter.respondToRequest(b.requestId, b.response);
        this.db
          .prepare("UPDATE commands SET state='delivered' WHERE id=?")
          .run(command.commandId);
      }
    } catch {
      this.quarantine(command.commandId);
      const row = this.row(command.commandId)!;
      if (row.session && b.kind === "start") {
        this.observe(command.commandId, "unknown", row.session, row.turn);
        if (row.turn)
          void this.watchLease(expiresAt, adapter, row.session, row.turn).catch(
            () => this.quarantine(command.commandId),
          );
      }
    }
  }
  private append(id: string, body: BridgeEvent["body"]): BridgeEvent {
    return this.tx(() => {
      const row = this.row(id);
      if (!row) throw new Error("unknown command");
      const command: BridgeCommand = JSON.parse(row.command);
      const { expiresAt, body: ignored, ...identity } = command;
      const event: BridgeEvent = {
        ...identity,
        sourceId: command.commandId,
        sourceSeq: row.seq + 1,
        body,
      };
      const raw = JSON.stringify(event);
      if (Buffer.byteLength(raw) > 16 * 1024)
        throw new Error("durable event exceeds 16 KiB");
      this.db
        .prepare("INSERT INTO events(command,seq,event) VALUES(?,?,?)")
        .run(id, event.sourceSeq, raw);
      this.db
        .prepare("UPDATE commands SET seq=? WHERE id=?")
        .run(event.sourceSeq, id);
      return event;
    });
  }
  private observe(
    id: string,
    state: "unknown" | "running" | "completed" | "failed" | "interrupted",
    sessionId: string,
    turnId: string | null,
  ): void {
    this.tx(() => {
      const row = this.row(id)!;
      if (row.state === "terminal_pending" || terminal(row.state)) return;
      this.append(id, { kind: "observation", state, sessionId, turnId });
      this.db
        .prepare("UPDATE commands SET session=?,turn=?,state=? WHERE id=?")
        .run(
          sessionId,
          turnId,
          terminal(state)
            ? "terminal_pending"
            : state === "unknown"
              ? "recovering"
              : row.state === "intent"
                ? "running"
                : row.state,
          id,
        );
    });
  }
  pending(limit = 64): BridgeEvent[] {
    return (
      this.db
        .prepare(
          "SELECT event FROM events WHERE acked=0 ORDER BY rowid LIMIT ?",
        )
        .all(Math.min(Math.max(limit, 1), 100)) as { event: string }[]
    ).map((r) => JSON.parse(r.event));
  }
  acknowledge(ack: Ack): void {
    this.tx(() => {
      const row = this.row(ack.sourceId);
      if (!row) throw new Error("unknown acknowledgement");
      const c: BridgeCommand = JSON.parse(row.command);
      if (
        c.runId !== ack.runId ||
        c.generation !== ack.generation ||
        !Number.isSafeInteger(ack.sourceSeq) ||
        !Number.isSafeInteger(ack.canonicalSeq) ||
        ack.canonicalSeq < 1 ||
        ack.sourceSeq < 1 ||
        ack.sourceSeq > row.seq
      )
        throw new Error("acknowledgement identity mismatch");
      if (ack.sourceSeq <= row.acked) return;
      if (ack.sourceSeq !== row.acked + 1 || ack.canonicalSeq < row.cursor)
        throw new Error("acknowledgement gap");
      const event = JSON.parse(
        (
          this.db
            .prepare("SELECT event FROM events WHERE command=? AND seq=?")
            .get(row.id, ack.sourceSeq) as { event: string }
        ).event,
      ) as BridgeEvent;
      this.db
        .prepare("UPDATE events SET acked=1 WHERE command=? AND seq=?")
        .run(row.id, ack.sourceSeq);
      this.db
        .prepare("UPDATE commands SET acked=?,cursor=? WHERE id=?")
        .run(ack.sourceSeq, ack.canonicalSeq, row.id);
      if (event.body.kind === "observation" && terminal(event.body.state)) {
        this.db
          .prepare("UPDATE commands SET state=? WHERE id=?")
          .run(event.body.state, row.id);
        this.db.prepare("DELETE FROM locks WHERE command=?").run(row.id);
      }
    });
  }
  private delta(id: string, text: string): void {
    // Existing canonical payload validator caps at 4000 characters. Split before
    // upload so no text is lost, including JSON escapes and supplementary Unicode.
    let part = "";
    for (const char of text) {
      if (part.length + char.length > 1500) {
        this.append(id, {
          kind: "event",
          eventType: "assistant.delta",
          payload: { text: part },
        });
        part = "";
      }
      part += char;
    }
    if (part)
      this.append(id, {
        kind: "event",
        eventType: "assistant.delta",
        payload: { text: part },
      });
  }
  async consume(
    id: string,
    adapter: HarnessAdapter,
    signal: AbortSignal,
  ): Promise<void> {
    if (this.consumers.has(adapter)) return;
    this.consumers.add(adapter);
    try {
      for await (const event of adapter.events(signal)) {
        this.recordNative(id, event);
        if (event.kind === "terminal") return;
      }
    } finally {
      this.consumers.delete(adapter);
      this.quarantine(id);
    }
  }
  recordNative(id: string, event: AdapterEvent): void {
    const row = this.row(id);
    if (!row) throw new Error("native event without durable intent");
    if (
      row.session !== event.sessionId ||
      (row.turn !== null && row.turn !== event.turnId)
    )
      throw new Error("native identity mismatch");
    if (terminal(row.state) || row.state === "terminal_pending") return;
    this.db
      .prepare("UPDATE commands SET turn=? WHERE id=?")
      .run(event.turnId, id);
    if (event.kind === "delta") this.delta(id, event.text);
    else if (event.kind === "terminal")
      this.observe(id, event.status, event.sessionId, event.turnId);
    else if (event.kind === "usage")
      this.append(id, {
        kind: "event",
        eventType: "usage.updated",
        payload: {
          input_tokens: event.inputTokens,
          output_tokens: event.outputTokens,
        },
      });
    else if (event.kind === "tool")
      this.append(
        id,
        event.phase === "started"
          ? {
              kind: "event",
              eventType: "tool.started",
              payload: { tool_call_id: event.itemId, name: event.name },
            }
          : {
              kind: "event",
              eventType: "tool.completed",
              payload: {
                tool_call_id: event.itemId,
                name: event.name,
                status: event.status ?? "unknown",
                ...(event.preview ? { result_preview: event.preview } : {}),
              },
            },
      );
    else {
      // Native approval binding/decisions are handled by Task 5. Do not turn an
      // unvalidated native request dictionary into a durable approval record.
      this.quarantine(id);
    }
  }
  async reconcile(id: string, adapter: HarnessAdapter): Promise<void> {
    const row = this.row(id);
    if (!row || terminal(row.state) || row.state === "terminal_pending") return;
    if (!row.session) {
      this.quarantine(id);
      return;
    }
    const c: BridgeCommand = JSON.parse(row.command);
    const history = await adapter.inspectTurn(row.session, id);
    if (
      history.sessionId !== row.session ||
      (row.turn && history.turnId !== row.turn) ||
      history.state === "unknown" ||
      !history.turnId
    ) {
      this.quarantine(id);
      return;
    }
    if (terminal(history.state)) {
      const texts = (
        this.db
          .prepare("SELECT event FROM events WHERE command=? ORDER BY seq")
          .all(id) as { event: string }[]
      )
        .map((r) => JSON.parse(r.event) as BridgeEvent)
        .filter(
          (e) =>
            e.body.kind === "event" && e.body.eventType === "assistant.delta",
        )
        .map((e) => (e.body as { payload: { text: string } }).payload.text)
        .join("");
      if (
        history.assistantText === undefined ||
        !history.assistantText.startsWith(texts)
      ) {
        this.quarantine(id);
        return;
      }
      this.delta(id, history.assistantText.slice(texts.length));
      this.observe(id, history.state, row.session, history.turnId);
    } else this.observe(id, "running", row.session, history.turnId);
    void this.watchLease(
      c.expiresAt,
      adapter,
      row.session,
      history.turnId,
    ).catch(() => this.quarantine(id));
  }
  async watchLease(
    expiresAt: string,
    adapter: HarnessAdapter,
    sessionId: string,
    turnId: string,
  ): Promise<void> {
    if (!Number.isFinite(Date.parse(expiresAt)))
      throw new Error("invalid lease");
    const key = `${sessionId}/${turnId}`;
    if (this.timers.has(key)) {
      if (Date.parse(expiresAt) > Date.now()) return;
      clearTimeout(this.timers.get(key)!);
      this.timers.delete(key);
    }
    const wait = Math.max(0, Date.parse(expiresAt) - Date.now());
    if (wait > 0) {
      await new Promise<void>((resolve) => {
        const timer = setTimeout(resolve, Math.min(wait, 2147483647));
        timer.unref();
        this.timers.set(key, timer);
      });
      this.timers.delete(key);
    }
    const rows = this.db
      .prepare("SELECT * FROM commands WHERE session=?")
      .all(sessionId) as Row[];
    const owner =
      rows.find((r) => r.turn === turnId) || rows.find((r) => r.turn === null);
    if (owner && (terminal(owner.state) || owner.state === "terminal_pending"))
      return;
    const command = owner
      ? (JSON.parse(owner.command) as BridgeCommand)
      : undefined;
    const lease = command
      ? (this.db
          .prepare("SELECT expires FROM leases WHERE run_id=?")
          .get(command.runId) as { expires: string } | undefined)
      : undefined;
    if (lease && Date.parse(lease.expires) > Date.now())
      return this.watchLease(lease.expires, adapter, sessionId, turnId);
    const claimed = this.tx(() => {
      if (command)
        this.db
          .prepare(
            "INSERT INTO leases(run_id,device,expires,blocked) VALUES(?,?,?,1) ON CONFLICT(run_id) DO UPDATE SET blocked=1",
          )
          .run(command.runId, command.deviceId, expiresAt);
      if (owner) this.quarantine(owner.id);
      return (
        this.db
          .prepare("INSERT OR IGNORE INTO interrupts(session,turn) VALUES(?,?)")
          .run(sessionId, turnId).changes === 1
      );
    });
    if (claimed)
      try {
        await adapter.interruptTurn(sessionId, turnId);
      } catch {
        /* uncertain interrupt remains quarantined; never assume process death */
      }
  }
}
