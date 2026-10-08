import { DatabaseSync } from "node:sqlite";
import { closeSync, constants, lstatSync, openSync } from "node:fs";
import { dirname } from "node:path";
import { createHash, randomUUID } from "node:crypto";
import { NativeExitUnconfirmed } from "./rpc.ts";
import {
  readBootIdentity,
  validateBootIdentity,
  type BootIdentity,
} from "./recovery.ts";
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
  type Rejection,
} from "./connection.ts";
import { nativeRequestBody } from "./approvals.ts";

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
  private executions = new Map<string, Promise<void>>();
  private timers = new Map<string, ReturnType<typeof setTimeout>>();
  private consumers = new WeakSet<HarnessAdapter>();
  constructor(
    path: string,
    private optionsFor: (workspaceId: string) => SessionOptions,
    // Trusted local composition/test hooks; never deserialize from bridge input.
    private local: {
      readBootIdentity?: () => BootIdentity;
      recoveryOnly?: boolean;
    } = {},
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
      CREATE TABLE IF NOT EXISTS interrupt_claims(command TEXT PRIMARY KEY REFERENCES commands(id), owner TEXT NOT NULL, pid INTEGER NOT NULL);
      CREATE TABLE IF NOT EXISTS interrupt_exit_uncertainty(command TEXT PRIMARY KEY REFERENCES commands(id));
      CREATE TABLE IF NOT EXISTS interrupt_recovery_boot(command TEXT PRIMARY KEY REFERENCES commands(id), evidence TEXT NOT NULL);`);
    if (!local.recoveryOnly)
      this.db.exec(
        "UPDATE commands SET state='recovering' WHERE state IN ('intent','running')",
      );
  }
  uncertainInterrupts(): string[] {
    return (
      this.db
        .prepare(
          "SELECT command FROM interrupt_exit_uncertainty ORDER BY command",
        )
        .all() as { command: string }[]
    ).map((row) => row.command);
  }
  private bootIdentity(): BootIdentity {
    return validateBootIdentity(
      (this.local.readBootIdentity ?? readBootIdentity)(),
    );
  }
  /** Explicit local operator action. Reboot proves termination, never run completion. */
  recoverInterrupt(commandId: string): void {
    const current = this.bootIdentity();
    const needsReboot = this.tx(() => {
      const row = this.row(commandId);
      if (
        !row ||
        JSON.parse(row.command).body.kind !== "interrupt" ||
        !this.db
          .prepare("SELECT 1 FROM interrupt_exit_uncertainty WHERE command=?")
          .get(commandId)
      )
        throw new Error("no uncertain interrupt with this command ID");
      const stored = this.db
        .prepare("SELECT evidence FROM interrupt_recovery_boot WHERE command=?")
        .get(commandId) as { evidence: string } | undefined;
      if (!stored) {
        // Legacy/unknown markers need a new baseline, then a subsequent reboot.
        this.db
          .prepare(
            "INSERT INTO interrupt_recovery_boot(command,evidence) VALUES(?,?)",
          )
          .run(commandId, JSON.stringify(current));
        return true;
      }
      const prior = validateBootIdentity(JSON.parse(stored.evidence));
      if (prior.machineId !== current.machineId)
        throw new Error("recovery refused: different machine");
      if (prior.bootId === current.bootId)
        throw new Error(
          "recovery refused: same boot; reboot this machine first",
        );
      if (prior.observedAt + 5000 >= current.bootStartedAt)
        throw new Error(
          "recovery evidence must predate current boot by more than five seconds",
        );
      this.db
        .prepare("DELETE FROM interrupt_claims WHERE command=?")
        .run(commandId);
      this.db
        .prepare("DELETE FROM interrupt_exit_uncertainty WHERE command=?")
        .run(commandId);
      this.db
        .prepare("DELETE FROM interrupt_recovery_boot WHERE command=?")
        .run(commandId);
      return false;
    });
    if (needsReboot)
      throw new Error(
        "recovery baseline recorded; wait at least ten seconds, reboot this machine, then retry",
      );
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
  /** Whether this command still holds its workspace's local lock. */
  holdsLock(commandId: string): boolean {
    return !!this.db
      .prepare("SELECT 1 FROM locks WHERE command=?")
      .get(commandId);
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
        "UPDATE commands SET state='recovering' WHERE id=? AND state NOT IN ('completed','failed','interrupted','terminal_pending','delivered','denied')",
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
  /** Called inside the intent transaction; a live executor never loses its claim. */
  private claimInterrupt(commandId: string, owner: string): boolean {
    // Executor death cannot resolve an explicitly unconfirmed native child exit.
    if (
      this.db
        .prepare("SELECT 1 FROM interrupt_exit_uncertainty WHERE command=?")
        .get(commandId)
    )
      return false;
    const claim = this.db
      .prepare("SELECT owner,pid FROM interrupt_claims WHERE command=?")
      .get(commandId) as { owner: string; pid: number } | undefined;
    if (claim) {
      try {
        process.kill(claim.pid, 0);
        return false;
      } catch (error) {
        // Only a confirmed dead executor permits reclaim. Timeouts, permission
        // failures, and PID reuse retain uncertainty instead of overlapping calls.
        if ((error as NodeJS.ErrnoException).code !== "ESRCH") return false;
      }
      this.db
        .prepare("DELETE FROM interrupt_claims WHERE command=? AND owner=?")
        .run(commandId, claim.owner);
    }
    this.db
      .prepare("INSERT INTO interrupt_claims(command,owner,pid) VALUES(?,?,?)")
      .run(commandId, owner, process.pid);
    return true;
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
      // A refused run is final (see reject) even while NOUS still leases it:
      // let its lease lapse so the watchdog interrupts the turn.
      if (this.state(c.commandId) === "denied") continue;
      this.db
        .prepare(
          "INSERT INTO leases(run_id,device,expires,blocked) VALUES(?,?,?,0) ON CONFLICT(run_id) DO UPDATE SET expires=excluded.expires,blocked=0",
        )
        .run(c.runId, deviceId, expiresAt);
    }
  }
  async execute(value: BridgeCommand, adapter: HarnessAdapter): Promise<void> {
    if (this.local.recoveryOnly)
      throw new Error("recovery-only journal cannot dispatch commands");
    const command = parseCommand(value);
    // Serialize the complete attempt, including its native receipt, so a
    // duplicate observes the prior attempt's persisted result before deciding.
    const previous =
      this.executions.get(command.commandId) ?? Promise.resolve();
    const current = previous
      .catch(() => {})
      .then(() => this.executeCommand(command, adapter));
    this.executions.set(command.commandId, current);
    try {
      await current;
    } finally {
      if (this.executions.get(command.commandId) === current)
        this.executions.delete(command.commandId);
    }
  }
  private async executeCommand(
    command: BridgeCommand,
    adapter: HarnessAdapter,
  ): Promise<void> {
    const { expiresAt, ...identity } = command;
    const digest = hash(identity);
    const existing = this.row(command.commandId);
    if (existing) {
      if (existing.digest !== digest)
        throw new Error("command payload conflict");
      // An exact targeted interrupt is safe to redeliver until the native
      // receipt is persisted. Ambiguous starts/responses are never replayed.
      if (
        command.body.kind !== "interrupt" ||
        !["intent", "recovering"].includes(existing.state)
      )
        return;
    }
    const lease = this.db
      .prepare("SELECT expires,blocked FROM leases WHERE run_id=?")
      .get(command.runId) as { expires: string; blocked: number } | undefined;
    // NOUS can keep leasing a run the bridge refused (see reject), whose own
    // lease is left to lapse. An interrupt of it only stops work, and an
    // approval for it is dropped below, so neither needs a live lease.
    const refused =
      command.body.kind !== "start" &&
      this.owner(command)?.state === "denied";
    if (
      !refused &&
      (Date.parse(expiresAt) <= Date.now() ||
        (lease && (lease.blocked || Date.parse(lease.expires) <= Date.now())))
    )
      throw new Error("lease expired; verified renewal required");
    const b = command.body;
    const interruptOwner = randomUUID();
    const claimed = this.tx(() => {
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
        // A refused run is final: its approvals are dropped, not forwarded.
        if (b.kind === "respond" && owner.state === "denied") return false;
        if (b.kind === "respond" && owner.state !== "running")
          throw new Error("approval blocked during recovery");
      }
      if (existing) {
        // Another journal/process can have recorded success since our read.
        // Both this claim and failure recovery preserve that durable receipt.
        if (
          this.db
            .prepare(
              "UPDATE commands SET state='intent' WHERE id=? AND state IN ('intent','recovering')",
            )
            .run(command.commandId).changes !== 1
        )
          return false;
      } else {
        this.db
          .prepare(
            "INSERT INTO commands(id,command,digest,state) VALUES(?,?,?,?)",
          )
          .run(command.commandId, JSON.stringify(command), digest, "intent");
      }
      if (b.kind === "start")
        this.db
          .prepare("INSERT INTO locks(workspace,command) VALUES(?,?)")
          .run(command.workspaceId, command.commandId);
      return (
        b.kind !== "interrupt" ||
        this.claimInterrupt(command.commandId, interruptOwner)
      );
    });
    if (!claimed) return;
    let retainInterruptClaim = false;
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
        const owner = this.owner(command);
        if (!owner) throw new Error("response owner disappeared");
        this.tx(() => {
          this.db
            .prepare("UPDATE commands SET state='delivered' WHERE id=?")
            .run(command.commandId);
          this.append(
            owner.id,
            { kind: "command_ack", approvalRecordId: b.approvalRecordId },
            command.commandId,
          );
        });
      }
    } catch (error) {
      retainInterruptClaim =
        b.kind === "interrupt" && error instanceof NativeExitUnconfirmed;
      if (retainInterruptClaim) {
        this.tx(() => {
          this.db
            .prepare(
              "INSERT OR IGNORE INTO interrupt_exit_uncertainty(command) VALUES(?)",
            )
            .run(command.commandId);
          let boot: BootIdentity;
          try {
            boot = this.bootIdentity();
          } catch {
            return;
          }
          this.db
            .prepare(
              "INSERT OR IGNORE INTO interrupt_recovery_boot(command,evidence) VALUES(?,?)",
            )
            .run(command.commandId, JSON.stringify(boot));
        });
      }
      this.quarantine(command.commandId);
      const row = this.row(command.commandId)!;
      if (row.session && b.kind === "start") {
        this.observe(command.commandId, "unknown", row.session, row.turn);
        if (row.turn)
          void this.watchLease(expiresAt, adapter, row.session, row.turn).catch(
            () => this.quarantine(command.commandId),
          );
      }
    } finally {
      // Release only this attempt, after its receipt or recovery state is durable.
      // Closing a journal while the call is pending retains its claim until the
      // executor exits; closing is not evidence that its native call has stopped.
      if (b.kind === "interrupt" && !this.closed && !retainInterruptClaim)
        this.db
          .prepare("DELETE FROM interrupt_claims WHERE command=? AND owner=?")
          .run(command.commandId, interruptOwner);
    }
  }
  private append(
    id: string,
    body: BridgeEvent["body"],
    eventCommandId?: string,
  ): BridgeEvent | undefined {
    return this.tx(() => {
      const row = this.row(id);
      if (!row) throw new Error("unknown command");
      // NOUS refused this run (see reject): nothing more of it is uploaded.
      if (row.state === "denied") return undefined;
      const command: BridgeCommand = JSON.parse(row.command);
      const { expiresAt, body: ignored, ...identity } = command;
      const event: BridgeEvent = {
        ...identity,
        ...(eventCommandId ? { commandId: eventCommandId } : {}),
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
      if (
        row.state === "terminal_pending" ||
        row.state === "denied" ||
        terminal(row.state)
      )
        return;
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
  // events.acked: 0 unsent, 1 acknowledged by NOUS, 2 refused by NOUS (reject).
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
  /** NOUS refused this run's events while the device grant stayed valid: its
   * chat was deleted or linked to another project, its folder binding was
   * removed, its consent was superseded, or a renewed grant had not yet
   * re-leased the run (NOUS may then keep leasing it, so renewLease skips a
   * 'denied' run and its lease still lapses). Final for the upload only: drop
   * every unsent event of the run and journal no more (append and observe skip
   * a 'denied' command). A refusal is not evidence that the native turn
   * stopped, so mid-run the workspace lock stays, as NOUS keeps its own, and
   * the lease watchdog still interrupts the turn. Once the run's terminal
   * observation is journaled, Codex has stopped writing and the lock is
   * released: NOUS authorizes an event before its receipt lookup, so the
   * refused event can be the replay of one it stored, after which it may have
   * finalized the run and released its own lock. Returns whether the run was
   * newly refused: false when it was already refused or already finished. */
  reject(rejection: Rejection): boolean {
    return this.tx(() => {
      const row =
        typeof rejection.sourceId === "string"
          ? this.row(rejection.sourceId)
          : undefined;
      if (!row) throw new Error("unknown rejection");
      const c: BridgeCommand = JSON.parse(row.command);
      if (
        rejection.code !== "run_access_denied" ||
        c.runId !== rejection.runId ||
        c.generation !== rejection.generation ||
        !Number.isSafeInteger(rejection.sourceSeq) ||
        rejection.sourceSeq < 1 ||
        rejection.sourceSeq > row.seq
      )
        throw new Error("rejection identity mismatch");
      this.db
        .prepare("UPDATE events SET acked=2 WHERE command=? AND acked=0")
        .run(row.id);
      // NOUS acknowledged the run's terminal observation (that ack released the
      // lock); the refused event was journaled after it, e.g. a respond's
      // command_ack. Drop it, and keep the run's final state.
      if (terminal(row.state)) return false;
      if (row.state === "denied") return false;
      this.db
        .prepare("UPDATE commands SET state='denied' WHERE id=?")
        .run(row.id);
      // 'terminal_pending': the terminal observation is journaled, not yet
      // acknowledged (observe() sets both in one transaction).
      if (row.state === "terminal_pending")
        this.db.prepare("DELETE FROM locks WHERE command=?").run(row.id);
      return true;
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
    else this.append(id, nativeRequestBody(event));
  }
  async reconcile(id: string, adapter: HarnessAdapter): Promise<void> {
    const row = this.row(id);
    if (!row || terminal(row.state) || row.state === "terminal_pending") return;
    const c: BridgeCommand = JSON.parse(row.command);
    if (row.state === "denied") {
      // NOUS refused this run: report nothing, but keep the expiry watchdog
      // armed so the native turn is still interrupted once its lease lapses.
      // Once per turn: skip it while a watchdog waits or after one claimed.
      if (
        !row.session ||
        !row.turn ||
        this.timers.has(`${row.session}/${row.turn}`) ||
        this.db
          .prepare("SELECT 1 FROM interrupts WHERE session=? AND turn=?")
          .get(row.session, row.turn)
      )
        return;
      // A fresh adapter (after a restart) interrupts only a turn it started or
      // read back, so read it back. A thread Codex no longer knows must not
      // fail the reconnect; the run stays refused either way.
      await adapter.inspectTurn(row.session, id).catch(() => undefined);
      void this.watchLease(c.expiresAt, adapter, row.session, row.turn).catch(
        () => this.quarantine(id),
      );
      return;
    }
    if (!row.session) {
      this.quarantine(id);
      return;
    }
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
    // reject() released a refused run's lock only after its terminal
    // observation was journaled: Codex has stopped, nothing to interrupt.
    if (owner?.state === "denied" && !this.holdsLock(owner.id)) return;
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
