import assert from "node:assert/strict";
import { test, type TestContext } from "node:test";
import {
  mkdtempSync,
  writeFileSync,
  readFileSync,
  rmSync,
  chmodSync,
  symlinkSync,
  statSync,
  realpathSync,
} from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { randomUUID } from "node:crypto";
import { once } from "node:events";
import type { ChildProcessWithoutNullStreams } from "node:child_process";
import { JsonRpcProcess, NativeExitUnconfirmed } from "../src/rpc.ts";
import { Journal } from "../src/journal.ts";
import type { BridgeCommand } from "../src/connection.ts";
import { CodexAdapter } from "../src/adapters/codex.ts";
import { CredentialStore, integrationHeaders } from "../src/credentials.ts";
import { connect, addWorkspace } from "../src/cli.ts";
import type { SessionOptions } from "../src/contracts.ts";
import { MCP_TOOL_TIMEOUT_SEC } from "../src/mcp/client.ts";
import { buildManagedMcpConfig } from "../src/mcp/config.ts";

// Wire shapes captured from `codex-cli 0.153.4 app-server generate-ts`.
const fixture = String.raw`
import fs from 'node:fs';
import readline from 'node:readline';
const root = process.argv[2];
const config = () => JSON.parse(fs.readFileSync(root+'/config.json','utf8'));
if (process.argv.includes('--version')) { console.log('codex-cli '+(config().version || '0.153.4')); process.exit(); }
fs.writeFileSync(root+'/pid', String(process.pid));
if(config().ignoreTerm) process.on('SIGTERM', () => fs.appendFileSync(root+'/signals', 'TERM\n'));
const send = v => process.stdout.write(JSON.stringify(v)+'\n');
for await (const line of readline.createInterface({input:process.stdin})) {
 const m = JSON.parse(line), c = config();
 fs.appendFileSync(root+'/calls.jsonl',JSON.stringify(m)+'\n');
 if (m.method === 'initialize') send({id:m.id,result:{userAgent:'codex/0.153.4',codexHome:root,platformFamily:'unix',platformOs:'macos'}});
 if (m.method === 'thread/start' || m.method === 'thread/resume') {
   send({id:m.id,result:{thread:{id:'s',turns:[]},cwd:root,approvalPolicy:'on-request',approvalsReviewer:'user',sandbox:{type:'workspaceWrite',writableRoots:[root],networkAccess:false,excludeTmpdirEnvVar:true,excludeSlashTmp:true},...c.effective}});
 }
 if (m.method === 'turn/start') {
   if(c.mode==='exit') process.exit(7);
   if(c.mode==='eof') { process.stdout.end(); continue; }
   if(c.mode==='malformed') { process.stdout.write('{broken}\n'); continue; }
   if(c.mode==='oversized') { process.stdout.write('x'.repeat(1048577)); continue; }
   if(c.request) send({id:41,method:c.request.method,params:c.request.params});
   if(c.resolveRequest) send({method:'serverRequest/resolved',params:{threadId:c.resolveThread || 's',requestId:41}});
   if(c.resolveCount) for(let id=0;id<c.resolveCount;id++) send({method:'serverRequest/resolved',params:{threadId:'s',requestId:id}});
   const msg = JSON.stringify({method:'item/agentMessage/delta',params:{threadId:'s',turnId:'t',itemId:'i',delta:c.text || 'early'}})+'\n';
   if(c.mode==='split') { process.stdout.write(msg.slice(0,25)); await new Promise(r=>setTimeout(r,10)); process.stdout.write(msg.slice(25)); } else process.stdout.write(msg);
   if(c.mode==='flood') for(let i=0;i<500;i++) send({method:'item/agentMessage/delta',params:{threadId:'s',turnId:'t',delta:'part'+i}});
   send({id:m.id,result:{turn:{id:'t',status:'inProgress',items:[],error:null}}});
   if(c.terminal) send({method:'turn/completed',params:{threadId:'s',turn:{id:'t',status:c.terminal,items:[],error:null}}});
 }
 if(m.id===41 && m.result && c.replayRequest) {
   send({id:41,method:c.request.method,params:c.request.params});
   send({method:'turn/completed',params:{threadId:'s',turn:{id:'t',status:'completed',items:[],error:null}}});
 }
 if(m.method==='thread/read') send({id:m.id,result:{thread:c.history || {id:'s',turns:[]}}});
 if(m.method==='turn/interrupt' && !c.hangInterrupt) send({id:m.id,result:{}});
}
`;
class FakeAppServer {
  root = realpathSync(mkdtempSync(join(tmpdir(), "nous-codex-")));
  config: Record<string, unknown> = {};
  constructor() {
    writeFileSync(join(this.root, "server.mjs"), fixture);
    this.configure({});
  }
  configure(value: object) {
    Object.assign(this.config, value);
    writeFileSync(join(this.root, "config.json"), JSON.stringify(this.config));
  }
  replyToStart(value: object): void {
    this.configure({ effective: value });
  }
  calls(method: string): object[] {
    try {
      return readFileSync(join(this.root, "calls.jsonl"), "utf8")
        .trim()
        .split("\n")
        .map((l) => JSON.parse(l))
        .filter((m) => m.method === method);
    } catch {
      return [];
    }
  }
}
function setup(t: TestContext) {
  const server = new FakeAppServer();
  const adapter = new CodexAdapter({
    command: process.execPath,
    args: [join(server.root, "server.mjs"), server.root],
  });
  const options: SessionOptions = {
    cwd: server.root,
    workspaceId: "workspace",
    policy: {
      sandbox: "workspace-write",
      approvalPolicy: "on-request",
      reviewer: "user",
      networkAccess: false,
      writableRoots: [server.root],
    },
  };
  t.after(async () => {
    await adapter.closeSession();
    try {
      process.kill(
        Number(readFileSync(join(server.root, "pid"), "utf8")),
        "SIGKILL",
      );
    } catch {}
    rmSync(server.root, { recursive: true, force: true });
  });
  return { server, adapter, options };
}
test("rejects weaker effective permissions", async (t) => {
  const { server, adapter, options } = setup(t);
  server.replyToStart({ sandbox: { type: "dangerFullAccess" } });
  await assert.rejects(adapter.startSession(options), /policy mismatch/);
  assert.equal(server.calls("turn/start").length, 0);
});
for (const operation of ["start", "resume"])
  for (const drift of [
    { approvalPolicy: "never" },
    { approvalsReviewer: "guardian_subagent" },
    { cwd: "/tmp" },
    {
      sandbox: {
        type: "workspaceWrite",
        writableRoots: ["/"],
        networkAccess: false,
        excludeTmpdirEnvVar: true,
        excludeSlashTmp: true,
      },
    },
  ]) {
    test(
      operation + " rejects effective drift " + JSON.stringify(drift),
      async (t) => {
        const { server, adapter, options } = setup(t);
        server.replyToStart(drift);
        await assert.rejects(
          operation === "start"
            ? adapter.startSession(options)
            : adapter.resumeSession("s", options),
          /policy mismatch/,
        );
        await assert.rejects(adapter.startTurn("s", "hello", "c"), /session/);
      },
    );
  }
// Mutation: src/adapters/codex.ts:213 remove the implicit cwd; :244 omit model.
// Command: pnpm --dir packages/harness-bridge exec node --experimental-sqlite
// --import tsx --test test/codex.test.ts
test("accepts a reply that omits cwd from writableRoots (real 0.153.4)", async (t) => {
  const { server, adapter, options } = setup(t);
  server.replyToStart({
    sandbox: {
      type: "workspaceWrite",
      writableRoots: [],
      networkAccess: false,
      excludeTmpdirEnvVar: true,
      excludeSlashTmp: true,
    },
  });
  const session = await adapter.startSession(options);
  assert.equal(session.id, "s");
});
test("pins a locally configured model in thread/start config", async (t) => {
  const { server, adapter, options } = setup(t);
  await adapter.startSession({ ...options, model: "gpt-6-astra" });
  const call: any = server.calls("thread/start")[0];
  assert.equal(call.params.config.model, "gpt-6-astra");
});
test("rejects a malformed local model name", async (t) => {
  const { server, adapter, options } = setup(t);
  await assert.rejects(
    adapter.startSession({ ...options, model: "bad model/name" }),
    /invalid local Codex model name/,
  );
  assert.equal(server.calls("thread/start").length, 0);
});
test("exact pinned version only", async (t) => {
  const { server, adapter } = setup(t);
  server.configure({ version: "0.153.5" });
  await assert.rejects(adapter.probe(), /unsupported Codex version/);
  assert.equal(server.calls("initialize").length, 0);
});
for (const mode of ["split", "malformed", "oversized", "exit", "eof"])
  test("transport " + mode, async (t) => {
    const { server, adapter, options } = setup(t);
    server.configure({ mode });
    await adapter.startSession(options);
    const signal = new AbortController();
    const iterator = adapter.events(signal.signal)[Symbol.asyncIterator]();
    const first = iterator.next();
    if (mode === "split") {
      await adapter.startTurn("s", "x", "c");
      assert.equal((await first).value?.kind, "delta");
      signal.abort();
    } else {
      const observed = assert.rejects(first, /transport|frame|JSON/);
      await assert.rejects(
        adapter.startTurn("s", "x", "c"),
        /transport|frame|JSON/,
      );
      await observed;
    }
  });
test("early events, bounded queue backpressure, and untruncated unicode chunks", async (t) => {
  const { server, adapter, options } = setup(t);
  const text = "😀".repeat(10000);
  server.configure({ text, mode: "flood", terminal: "completed" });
  await adapter.startSession(options);
  const ac = new AbortController();
  const collected: string[] = [];
  let terminal = false;
  const consumer = (async () => {
    for await (const event of adapter.events(ac.signal)) {
      if (event.kind === "delta") {
        assert.ok(Buffer.byteLength(event.text) <= 8192);
        collected.push(event.text);
        if (collected.length === 1) await new Promise((r) => setTimeout(r, 60));
      }
      if (event.kind === "terminal") {
        terminal = true;
        break;
      }
    }
  })();
  await adapter.startTurn("s", "x", "c");
  await consumer;
  assert.equal(
    collected.join(""),
    text + Array.from({ length: 500 }, (_, i) => "part" + i).join(""),
  );
  assert.equal(terminal, true);
  assert.equal(adapter.queueHighWaterMark, 32);
});
test("requires event consumer and serializes active turns", async (t) => {
  const { adapter, options } = setup(t);
  await adapter.startSession(options);
  await assert.rejects(adapter.startTurn("s", "x", "c"), /consumer/);
  const ac = new AbortController();
  const consume = (async () => {
    for await (const _ of adapter.events(ac.signal)) {
      /* drain */
    }
  })();
  await adapter.startTurn("s", "x", "c");
  await assert.rejects(adapter.startTurn("s", "x", "d"), /active|ambiguous/);
  await adapter.interruptTurn("s", "t");
  await assert.rejects(adapter.startTurn("s", "x", "d"), /active|ambiguous/);
  ac.abort();
  await consume;
});
test("unsupported and schema-invalid requests denied locally", async (t) => {
  const { server, adapter, options } = setup(t);
  server.configure({
    request: {
      method: "item/permissions/requestApproval",
      params: { threadId: "s", turnId: "t", itemId: "i" },
    },
  });
  await adapter.startSession(options);
  const ac = new AbortController();
  const events: unknown[] = [];
  const consume = (async () => {
    for await (const e of adapter.events(ac.signal)) events.push(e);
  })();
  await adapter.startTurn("s", "x", "c");
  await new Promise((r) => setTimeout(r, 30));
  assert.equal(
    events.some((e: any) => e.kind === "request"),
    false,
  );
  const rows = readFileSync(join(server.root, "calls.jsonl"), "utf8")
    .split("\n")
    .filter(Boolean)
    .map((l) => JSON.parse(l));
  assert.ok(rows.some((r) => r.id === 41 && r.error));
  ac.abort();
  await consume;
});
test("credential handles are opaque and owner-only; symlinks and unsafe modes fail closed", async (t) => {
  const dir = mkdtempSync(join(tmpdir(), "nous-creds-"));
  t.after(() => rmSync(dir, { recursive: true, force: true }));
  const store = new CredentialStore(join(dir, "vault"));
  const creds = { accessToken: "access-secret", grantToken: "grant-secret" };
  const handle = await store.save(creds);
  assert.ok(!handle.includes("secret"));
  assert.deepEqual(await store.load(handle), creds);
  assert.equal(
    statSync(join(dir, "vault", handle + ".json")).mode & 0o777,
    0o600,
  );
  assert.deepEqual(integrationHeaders(creds), {
    Authorization: "Bearer access-secret",
    "X-NOUS-Integration-Grant": "grant-secret",
  });
  chmodSync(join(dir, "vault", handle + ".json"), 0o644);
  await assert.rejects(store.load(handle), /permissions/);
  await assert.rejects(store.load("../elsewhere"), /handle/);
  const link = "11111111-1111-4111-8111-111111111111";
  symlinkSync(
    join(dir, "vault", handle + ".json"),
    join(dir, "vault", link + ".json"),
  );
  await assert.rejects(store.load(link), /symbolic|symlink|ELOOP/);
});

test("connect exchanges CLI-owned grant; workspace sends only opaque IDs and labels", async (t) => {
  const dir = realpathSync(mkdtempSync(join(tmpdir(), "nous-connect-")));
  t.after(() => rmSync(dir, { recursive: true, force: true }));
  const project = "11111111-1111-4111-8111-111111111111",
    device = "22222222-2222-4222-8222-222222222222",
    grant = "33333333-3333-4333-8333-333333333333";
  const calls: { url: string; body: any; headers: any }[] = [];
  const announcements: string[] = [];
  const fetchFn = (async (input: any, init: any) => {
    const url = String(input);
    calls.push({
      url,
      body: init.body ? JSON.parse(init.body) : undefined,
      headers: init.headers,
    });
    let data: object;
    if (url.endsWith("/cli-auth/start"))
      data = {
        session_id: "login",
        poll_token: "poll-secret",
        browser_url: "https://nous.test/login",
        verification_code: "ABCD-1234",
      };
    else if (url.includes("/cli-auth/status/"))
      data = { status: "approved", token: "cli-secret" };
    else if (url.endsWith("/integrations/devices")) data = { id: device };
    else if (url.endsWith("/grant-requests"))
      data = { id: grant, approval_url: "https://nous.test/approval" };
    else if (url.endsWith("/exchange"))
      data = { token: "grant-secret", grant_id: grant };
    else if (url.endsWith("/workspaces")) data = {};
    else data = { status: "approved" };
    return new Response(JSON.stringify(data), { status: 200 });
  }) as typeof fetch;
  const options = {
    stateDir: join(dir, "state"),
    fetchFn,
    announce: (m: string) => announcements.push(m),
  };
  const paired = await connect({
    ...options,
    apiUrl: "https://nous.test/api/v1",
    projectId: project,
    label: "Laptop",
  });
  assert.equal(paired.deviceId, device);
  assert.equal(calls.filter((c) => c.url.endsWith("/exchange")).length, 1);
  // The grant id is stored so the grant can be renewed before it expires.
  const stored = await new CredentialStore(options.stateDir).load(
    paired.credentialHandle,
  );
  assert.equal(stored.grantId, grant);
  assert.equal(typeof stored.renewedAt, "number");
  const created = await addWorkspace({
    ...options,
    root: dir,
    label: "Research",
  });
  const again = await addWorkspace({
    ...options,
    root: dir,
    label: "Research",
  });
  assert.deepEqual(again, created);
  const registration = calls.find((c) => c.url.endsWith("/workspaces"))!;
  assert.deepEqual(registration.body, {
    workspace_id: created.workspaceId,
    label: "Research",
    project_id: project,
  });
  assert.equal(registration.headers.Authorization, "Bearer cli-secret");
  const statusPoll = calls.find((c) => c.url.includes("/cli-auth/status/"))!;
  assert.ok(!statusPoll.url.includes("poll-secret"));
  assert.equal(statusPoll.headers["X-CLI-Poll-Token"], "poll-secret");
  assert.equal(calls.filter((c) => c.url.endsWith("/workspaces")).length, 1);
  assert.ok(!JSON.stringify(calls.map((c) => c.body)).includes(dir));
  assert.ok(!announcements.join("").includes("secret"));
  const state = readFileSync(join(dir, "state", "connection.json"), "utf8");
  assert.ok(!state.includes("secret"));
  assert.ok(state.includes(paired.credentialHandle));
});

test("denied browser consent never exchanges or stores credentials", async (t) => {
  const dir = mkdtempSync(join(tmpdir(), "nous-deny-"));
  t.after(() => rmSync(dir, { recursive: true, force: true }));
  const calls: string[] = [];
  const fetchFn = (async (input: any) => {
    const url = String(input);
    calls.push(url);
    return new Response(
      JSON.stringify(
        url.endsWith("/start")
          ? {
              session_id: "s",
              poll_token: "p",
              browser_url: "https://nous.test",
              verification_code: "ABCD-1234",
            }
          : { status: "denied" },
      ),
    );
  }) as typeof fetch;
  await assert.rejects(
    connect({
      stateDir: join(dir, "state"),
      apiUrl: "https://nous.test/api/v1",
      projectId: "11111111-1111-4111-8111-111111111111",
      label: "Laptop",
      fetchFn,
      announce: () => {},
    }),
    /denied/,
  );
  assert.equal(calls.length, 2);
});
for (const method of [
  "item/commandExecution/requestApproval",
  "item/fileChange/requestApproval",
  "item/tool/requestUserInput",
])
  test("validates and answers exact callback " + method, async (t) => {
    const { server, adapter, options } = setup(t);
    const params: Record<string, unknown> = {
      threadId: "s",
      turnId: "t",
      itemId: "i",
      ...(method.includes("UserInput")
        ? {
            questions: [
              {
                id: "q",
                header: "Choose",
                question: "Which?",
                isOther: false,
                isSecret: false,
                options: null,
              },
            ],
            isBlocking: true,
            autoResolutionMs: null,
          }
        : {
            startedAtMs: 1,
            ...(method.includes("command")
              ? {
                  approvalId: "a",
                  kind: "command",
                  environmentId: null,
                  command: "pwd",
                  cwd: server.root,
                }
              : method.includes("fileChange")
                ? {}
              : {}),
          }),
    };
    server.configure({ request: { method, params } });
    await adapter.startSession(options);
    const ac = new AbortController();
    let seen = false;
    const consume = (async () => {
      for await (const event of adapter.events(ac.signal)) {
        if (event.kind === "request") {
          seen = true;
          assert.deepEqual(event.params, params);
          await adapter.respondToRequest(
            event.requestId,
            method.includes("UserInput")
              ? { kind: "answers", answers: { q: ["yes"] } }
              : { kind: "decision", allow: false },
          );
          await assert.rejects(
            adapter.respondToRequest(event.requestId, {
              kind: "decision",
              allow: true,
            }),
            /stale/,
          );
        }
      }
    })();
    await adapter.startTurn("s", "x", "c");
    await new Promise((r) => setTimeout(r, 30));
    assert.equal(seen, true);
    const rows = readFileSync(join(server.root, "calls.jsonl"), "utf8")
      .trim()
      .split("\n")
      .map((l) => JSON.parse(l));
    assert.deepEqual(
      rows.find((r) => r.id === 41).result,
      method.includes("UserInput")
        ? { answers: { q: { answers: ["yes"] } } }
        : { decision: "decline" },
    );
    ac.abort();
    await consume;
  });
for (const request of [
  {
    method: "item/commandExecution/requestApproval",
    params: { threadId: "s", turnId: "t", itemId: "i", startedAtMs: "invalid" },
  },
  {
    method: "item/tool/requestUserInput",
    params: {
      threadId: "s",
      turnId: "t",
      itemId: "i",
      questions: [{}],
      isBlocking: true,
    },
  },
  {
    method: "item/fileChange/requestApproval",
    params: {
      threadId: "s",
      turnId: "t",
      itemId: "i",
      startedAtMs: 1,
      grantRoot: "/outside-workspace",
    },
  },
])
  test("denies invalid or persistent approval " + request.method, async (t) => {
    const { server, adapter, options } = setup(t);
    server.configure({ request });
    await adapter.startSession(options);
    const ac = new AbortController();
    const events: any[] = [];
    const consume = (async () => {
      for await (const e of adapter.events(ac.signal)) events.push(e);
    })();
    await adapter.startTurn("s", "x", "c");
    await new Promise((r) => setTimeout(r, 30));
    assert.ok(!events.some((e) => e.kind === "request"));
    const rows = readFileSync(join(server.root, "calls.jsonl"), "utf8")
      .trim()
      .split("\n")
      .map((l) => JSON.parse(l));
    assert.ok(rows.find((r) => r.id === 41).error);
    ac.abort();
    await consume;
  });
for (const status of ["failed", "interrupted"])
  test("native terminal " + status, async (t) => {
    const { server, adapter, options } = setup(t);
    server.configure({ terminal: status });
    await adapter.startSession(options);
    const events: any[] = [];
    const consume = (async () => {
      for await (const e of adapter.events(new AbortController().signal)) {
        events.push(e);
        if (e.kind === "terminal") break;
      }
    })();
    await adapter.startTurn("s", "x", "c");
    await consume;
    assert.equal(events.at(-1).status, status);
  });
test("start/resume propagate local MCP and restrictive turn settings only", async (t) => {
  const { server, adapter, options } = setup(t);
  options.mcpConfig = {
    nous: { command: "nous-mcp", args: ["--credential-handle", "opaque"] },
  };
  await adapter.resumeSession("s", options);
  const resume: any = server.calls("thread/resume")[0];
  assert.equal(resume.params.config.mcp_servers.nous.command, "nous-mcp");
  assert.equal(resume.params.sandbox, "workspace-write");
  options.policy.writableRoots.push("/"); // External mutation cannot widen the verified snapshot.
  const ac = new AbortController();
  const consume = (async () => {
    for await (const e of adapter.events(ac.signal)) {
    }
  })();
  await adapter.startTurn("s", "x", "c");
  const call: any = server.calls("turn/start")[0];
  assert.deepEqual(call.params.sandboxPolicy, {
    type: "workspaceWrite",
    writableRoots: [server.root],
    networkAccess: false,
    excludeTmpdirEnvVar: true,
    excludeSlashTmp: true,
  });
  assert.equal(call.params.approvalsReviewer, "user");
  ac.abort();
  await consume;
});

test("a local MCP tool timeout reaches Codex; a malformed one is refused", async (t) => {
  const { adapter, options } = setup(t);
  for (const bad of [0, -1, 1.5, 601, "150"]) {
    options.mcpConfig = { nous: { command: "nous-mcp", args: [], tool_timeout_sec: bad as number } };
    await assert.rejects(adapter.resumeSession("s", options), /invalid local MCP configuration/);
  }
  // The bounds 1 and 600 are accepted, and so is the real managed entry: raising
  // MCP_TOOL_TIMEOUT_SEC past the validator's cap fails here.
  const managed = buildManagedMcpConfig({
    apiOrigin: "https://nous.example",
    credentialHandle: randomUUID(),
    stateDir: tmpdir(),
  }).nous!;
  assert.equal(managed.tool_timeout_sec, MCP_TOOL_TIMEOUT_SEC);
  for (const nous of [
    { command: "nous-mcp", args: [], tool_timeout_sec: 1 },
    { command: "nous-mcp", args: [], tool_timeout_sec: 600 },
    managed,
  ]) {
    const run = setup(t); // One open session per adapter.
    run.options.mcpConfig = { nous };
    await run.adapter.resumeSession("s", run.options);
    const resume: any = run.server.calls("thread/resume")[0];
    assert.deepEqual(resume.params.config.mcp_servers.nous, nous);
  }
});

test("denies network and temporary-root policy widening", async (t) => {
  const { server, adapter, options } = setup(t);
  server.replyToStart({
    sandbox: {
      type: "workspaceWrite",
      writableRoots: [server.root],
      networkAccess: true,
      excludeTmpdirEnvVar: true,
      excludeSlashTmp: true,
    },
  });
  await assert.rejects(adapter.startSession(options), /policy mismatch/);
  server.replyToStart({
    sandbox: {
      type: "workspaceWrite",
      writableRoots: [server.root],
      networkAccess: false,
      excludeTmpdirEnvVar: false,
      excludeSlashTmp: false,
    },
  });
  await assert.rejects(adapter.resumeSession("s", options), /policy mismatch/);
  assert.equal(server.calls("turn/start").length, 0);
});

test("resuming an active native turn cannot steer or start another", async (t) => {
  const { server, adapter, options } = setup(t);
  server.replyToStart({
    thread: { id: "s", turns: [{ id: "other", status: "inProgress" }] },
  });
  await assert.rejects(adapter.resumeSession("s", options), /active turn/);
  await assert.rejects(adapter.startTurn("s", "x", "c"), /session/);
  assert.equal(server.calls("turn/start").length, 0);
});

test("malformed credential JSON errors do not reveal secrets", async (t) => {
  const dir = mkdtempSync(join(tmpdir(), "nous-secret-"));
  t.after(() => rmSync(dir, { recursive: true, force: true }));
  const store = new CredentialStore(join(dir, "vault"));
  const id = await store.save({ accessToken: "secret", grantToken: "secret" });
  writeFileSync(
    join(dir, "vault", id + ".json"),
    '{"accessToken":"secret" BROKEN',
  );
  await assert.rejects(
    store.load(id),
    (error) =>
      error instanceof Error && error.message === "invalid stored JSON",
  );
});

test("answered native callback cannot be replayed", async (t) => {
  const { server, adapter, options } = setup(t);
  server.configure({
    replayRequest: true,
    request: {
      method: "item/fileChange/requestApproval",
      params: { threadId: "s", turnId: "t", itemId: "i", startedAtMs: 1 },
    },
  });
  await adapter.startSession(options);
  let requests = 0;
  const consume = (async () => {
    for await (const event of adapter.events(new AbortController().signal)) {
      if (event.kind === "request") {
        requests++;
        await adapter.respondToRequest(event.requestId, {
          kind: "decision",
          allow: false,
        });
      }
      if (event.kind === "terminal") break;
    }
  })();
  const rejected = assert.rejects(consume, /duplicate native callback ID/);
  await adapter.startTurn("s", "x", "c");
  await rejected;
  assert.equal(requests, 1);
});

for (const threadId of ["s", "foreign"])
  test("native resolution respects session " + threadId, async (t) => {
    const { server, adapter, options } = setup(t);
    server.configure({
      resolveRequest: true,
      resolveThread: threadId,
      request: {
        method: "item/fileChange/requestApproval",
        params: { threadId: "s", turnId: "t", itemId: "i", startedAtMs: 1 },
      },
    });
    await adapter.startSession(options);
    const ac = new AbortController();
    const iterator = adapter.events(ac.signal)[Symbol.asyncIterator]();
    const first = iterator.next();
    await adapter.startTurn("s", "x", "c"); // Resolution precedes this acknowledgment on the wire.
    assert.equal((await first).value?.kind, "request");
    if (threadId === "s")
      await assert.rejects(
        adapter.respondToRequest(41, { kind: "decision", allow: true }),
        /stale/,
      );
    else await adapter.respondToRequest(41, { kind: "decision", allow: false });
    ac.abort();
    await iterator.return?.();
  });

test("escaped deltas reserve envelope bytes and retain complete text", async (t) => {
  const { server, adapter, options } = setup(t);
  const text = '\n\u0000\t\r"\\😀'.repeat(5000);
  server.configure({ text, terminal: "completed" });
  await adapter.startSession(options);
  const chunks: string[] = [];
  const consume = (async () => {
    for await (const event of adapter.events(new AbortController().signal)) {
      if (event.kind === "delta") {
        assert.ok(
          Buffer.byteLength(JSON.stringify(event)) <= 12 * 1024,
          "serialized event must reserve 4 KiB of the 16 KiB durable limit",
        );
        chunks.push(event.text);
      }
      if (event.kind === "terminal") break;
    }
  })();
  // Attach rejection before starting native work to avoid an unhandled assertion.
  const observed = consume.then(
    () => ({ error: undefined }),
    (error) => ({ error }),
  );
  const start = adapter.startTurn("s", "x", "c").then(
    () => ({ error: undefined }),
    (error) => ({ error }),
  );
  const result = await observed;
  const started = await start;
  if (result.error) throw result.error;
  if (started.error) throw started.error;
  assert.equal(chunks.join(""), text);
});

test("native callback identity history fails closed at its resource bound", async (t) => {
  const { server, adapter, options } = setup(t);
  server.configure({ resolveCount: 4097 });
  await adapter.startSession(options);
  const iterator = adapter
    .events(new AbortController().signal)
    [Symbol.asyncIterator]();
  const rejected = assert.rejects(
    iterator.next(),
    /callback identity capacity exceeded/,
  );
  await assert.rejects(
    adapter.startTurn("s", "x", "c"),
    /callback identity capacity exceeded/,
  );
  await rejected;
});

test("pinned full history matches client command identity and recovers text", async (t) => {
  const { server, adapter } = setup(t);
  server.configure({
    history: {
      id: "s",
      turns: [
        {
          id: "t",
          itemsView: "full",
          status: "completed",
          items: [
            { type: "userMessage", clientId: "command", id: "u", content: [] },
            { type: "agentMessage", id: "a", text: "answer" },
          ],
        },
      ],
    },
  });
  assert.deepEqual(await adapter.inspectTurn("s", "command"), {
    state: "completed",
    sessionId: "s",
    turnId: "t",
    assistantText: "answer",
  });
  assert.deepEqual(
    server.calls("thread/read").map((m: any) => m.params),
    [{ threadId: "s", includeTurns: true }],
  );
});
for (const history of [
  { id: "foreign", turns: [] },
  {
    id: "s",
    turns: [
      {
        id: "t",
        itemsView: "summary",
        status: "completed",
        items: [{ type: "userMessage", clientId: "command" }],
      },
    ],
  },
  {
    id: "s",
    turns: [
      {
        id: "t",
        itemsView: "full",
        status: "completed",
        items: [{ type: "userMessage", clientId: "different" }],
      },
    ],
  },
])
  test(`history without exact full native identity remains unknown ${JSON.stringify(history)}`, async (t) => {
    const { server, adapter } = setup(t);
    server.configure({ history });
    assert.deepEqual(await adapter.inspectTurn("s", "command"), {
      state: "unknown",
      sessionId: "s",
      turnId: null,
    });
  });

test("history reconciliation uses a fresh reader after native transport loss without another start", async (t) => {
  const { server, adapter, options } = setup(t);
  server.configure({ mode: "exit" });
  await adapter.startSession(options);
  const controller = new AbortController();
  const consume = (async () => {
    try {
      for await (const _event of adapter.events(controller.signal)) {
      }
    } catch {}
  })();
  await assert.rejects(adapter.startTurn("s", "hello", "command"));
  await consume;
  server.configure({
    history: {
      id: "s",
      turns: [
        {
          id: "t",
          itemsView: "full",
          status: "completed",
          items: [
            { type: "userMessage", clientId: "command" },
            { type: "agentMessage", text: "recovered" },
          ],
        },
      ],
    },
  });
  assert.equal(
    (await adapter.inspectTurn("s", "command")).assistantText,
    "recovered",
  );
  assert.equal(server.calls("turn/start").length, 1);
});

async function waitForInterrupt(server: FakeAppServer): Promise<void> {
  const deadline = Date.now() + 2000;
  while (!server.calls("turn/interrupt").length) {
    assert.ok(
      Date.now() < deadline,
      "fake app-server did not receive interrupt",
    );
    await new Promise((resolve) => setTimeout(resolve, 5));
  }
}
function assertServerExited(server: FakeAppServer): void {
  const pid = Number(readFileSync(join(server.root, "pid"), "utf8"));
  assert.throws(() => process.kill(pid, 0), { code: "ESRCH" });
}
for (const shutdown of ["timeout", "close"] as const) {
  test(
    `hung interrupt ${shutdown} waits for app-server exit before releasing claim`,
    { timeout: 40_000 },
    async (t) => {
      const { server, adapter, options } = setup(t);
      server.configure({ hangInterrupt: true, ignoreTerm: true });
      const j = new Journal(join(server.root, "journal.sqlite"), () => options);
      const c: BridgeCommand = {
        commandId: randomUUID(),
        runId: randomUUID(),
        deviceId: randomUUID(),
        workspaceId: randomUUID(),
        generation: 1,
        expiresAt: new Date(Date.now() + 60_000).toISOString(),
        body: { kind: "start", input: "hello" },
      };
      const signal = new AbortController();
      const consumer = (async () => {
        try {
          for await (const event of adapter.events(signal.signal)) {
            void event;
          }
        } catch {}
      })();
      const stop: BridgeCommand = {
        ...c,
        commandId: randomUUID(),
        body: { kind: "interrupt", sessionId: "s", turnId: "t" },
      };
      try {
        await j.execute(c, adapter);
        const pending = j.execute(stop, adapter);
        await waitForInterrupt(server);
        if (shutdown === "close") {
          await adapter.closeSession();
          assertServerExited(server);
        }
        await pending;
        assertServerExited(server);
        assert.match(
          readFileSync(join(server.root, "signals"), "utf8"),
          /TERM/,
        );
        assert.equal(j.state(stop.commandId), "recovering");
        assert.equal(j.workspaceLocked(c.workspaceId), true);
        let retries = 0;
        await j.execute(stop, {
          interruptTurn: async () => {
            retries++;
          },
        } as unknown as import("../src/contracts.ts").HarnessAdapter);
        assert.equal(retries, 1);
        assert.equal(j.state(stop.commandId), "delivered");
      } finally {
        signal.abort();
        await consumer;
        j.close();
      }
    },
  );
}

test("unconfirmed app-server exit fails closed when termination signals cannot be delivered", async (t) => {
  const server = new FakeAppServer();
  server.configure({ hangInterrupt: true, ignoreTerm: true });
  const rpc = new JsonRpcProcess(
    process.execPath,
    [join(server.root, "server.mjs"), server.root],
    async () => {},
    () => {},
  );
  // Simulate an OS refusing delivery of signals to this real subprocess.
  const child = (rpc as unknown as { child: ChildProcessWithoutNullStreams })
    .child;
  const kill = child.kill.bind(child);
  t.after(async () => {
    child.kill = kill;
    const exited = once(child, "exit");
    kill("SIGKILL");
    await exited;
    await rpc.close();
    rmSync(server.root, { recursive: true, force: true });
  });
  await rpc.call("initialize", {});
  child.kill = () => false;
  const pending = assert.rejects(
    rpc.call("turn/interrupt", {}),
    NativeExitUnconfirmed,
  );
  await waitForInterrupt(server);
  await assert.rejects(rpc.close(), NativeExitUnconfirmed);
  await pending;
  assert.doesNotThrow(() => process.kill(child.pid!, 0));
});


// Mutation: src/cli.ts request() must not interpolate the HTTP response body.
// Command: pnpm --dir packages/harness-bridge exec node --experimental-sqlite
// --import tsx --test --test-name-pattern="request errors" test/codex.test.ts
for (const body of ["<html>echoed-credential</html>", '{"detail":"echoed-credential"}'])
  test("request errors omit arbitrary backend bodies: " + body, async (t) => {
    const dir = mkdtempSync(join(tmpdir(), "nous-error-"));
    t.after(() => rmSync(dir, { recursive: true, force: true }));
    await assert.rejects(
      connect({
        stateDir: dir,
        apiUrl: "https://nous.test/api/v1",
        projectId: "11111111-1111-4111-8111-111111111111",
        label: "Laptop",
        fetchFn: (async () => new Response(body, { status: 500 })) as typeof fetch,
        announce: () => {},
      }),
      (error: Error) => {
        assert.match(error.message, /NOUS request failed \(500\).*cli-auth\/start/);
        assert.ok(!error.message.includes("echoed-credential"));
        return true;
      },
    );
  });
