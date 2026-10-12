import assert from "node:assert/strict";
import { test } from "node:test";
import { mkdtempSync, readFileSync, realpathSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { rc } from "../src/cli.ts";

const PROJECT_A = "11111111-1111-4111-8111-111111111111";
const PROJECT_B = "12121212-1212-4121-8121-121212121212";
const NEW_PROJECT = "13131313-1313-4131-8131-131313131313";
const WORKSPACE = "77777777-7777-4777-8777-777777777777";
const DEVICE = "22222222-2222-4222-8222-222222222222";
const GRANT = "33333333-3333-4333-8333-333333333333";

type Call = { url: string; method: string; auth?: string; body?: any };

/**
 * Fake NOUS for rc: projects list/create, workspaces, and the login + consent
 * endpoints `connect` uses. `tokenOk` decides whether a stored CLI token is
 * still accepted on /projects.
 */
function fakeNous(calls: Call[], { tokenOk = (_auth?: string): boolean => true, status = 200 } = {}): typeof fetch {
  let grants = 0;
  return (async (input: any, init: any = {}) => {
    const url = String(input);
    const auth = (init.headers?.Authorization ?? init.headers?.authorization) as string | undefined;
    const body = init.body ? JSON.parse(init.body) : undefined;
    calls.push({ url, method: init.method ?? "GET", auth, body });
    const json = (data: object, code = 200) => new Response(JSON.stringify(data), { status: code });
    if (status !== 200 && url.includes("/integrations/")) return json({ detail: "off" }, status);
    if (url.includes("/projects?")) {
      if (!tokenOk(auth)) return json({ detail: "expired" }, 401);
      return json({
        projects: [
          { id: PROJECT_A, name: "Thesis lit review", document_count: 12 },
          { id: PROJECT_B, name: "RAG eval notes", document_count: 4 },
        ],
      });
    }
    if (url.endsWith("/projects") && init.method === "POST")
      return json({ id: NEW_PROJECT, name: body.name, document_count: 0 });
    if (url.endsWith("/api/v2/workspaces")) return json([{ id: WORKSPACE, name: "Mine" }]);
    if (url.endsWith("/cli-auth/start"))
      return json({ session_id: "s", poll_token: "p", browser_url: "https://nous.test/l", verification_code: "ABCD-1234" });
    if (url.includes("/cli-auth/status/")) return json({ status: "approved", token: "cli-secret" });
    if (url.endsWith("/integrations/devices")) return json({ id: DEVICE });
    if (url.endsWith("/grant-requests")) return json({ id: GRANT, approval_url: "https://nous.test/a" });
    if (url.endsWith("/exchange")) return json({ token: `grant-${(grants += 1)}`, grant_id: GRANT });
    if (url.endsWith("/renew")) return json({ token: `renewed-${(grants += 1)}`, grant_id: GRANT });
    if (url.includes("/workspaces") && init.method === "POST") return json({ ok: true });
    return json({ status: "approved" });
  }) as typeof fetch;
}

function setup(answers: string[], fake?: typeof fetch) {
  const dir = mkdtempSync(join(tmpdir(), "nous-rc-"));
  const folder = realpathSync(mkdtempSync(join(tmpdir(), "nous-rc-folder-")));
  const calls: Call[] = [];
  const messages: string[] = [];
  const prompts: string[] = [];
  const runs: string[] = [];
  const options = {
    stateDir: dir,
    apiUrl: "https://nous.test/api/v1",
    dir: folder,
    label: "Laptop",
    fetchFn: fake ?? fakeNous(calls),
    announce: (m: string) => messages.push(m),
    delay: async () => {},
    prompt: async (q: string) => {
      prompts.push(q);
      const next = answers.shift();
      if (next === undefined) throw new Error("test ran out of answers");
      return next;
    },
    probe: async () => ({ version: "0.161.0" }),
    run: async (stateDir: string) => {
      runs.push(stateDir);
    },
  };
  // Tests that rerun against an earlier run's store repoint options.stateDir.
  const state = () => JSON.parse(readFileSync(join(options.stateDir, "connection.json"), "utf8"));
  const urls = () => calls.map((c) => c.url);
  const cleanup = () => {
    rmSync(dir, { recursive: true, force: true });
    rmSync(folder, { recursive: true, force: true });
  };
  return { dir, folder, calls, urls, messages, prompts, runs, options, state, cleanup };
}

test("picks a listed project, pairs with execute + read, registers the folder, runs", async () => {
  const s = setup(["2"]);
  try {
    await rc(s.options);
    assert.equal(s.state().projectId, PROJECT_B);
    assert.deepEqual(s.state().scopes, ["harness:execute", "tools:read"]);
    assert.equal(s.state().workspaces.length, 1);
    assert.equal(s.state().workspaces[0].root, s.folder);
    assert.deepEqual(s.runs, [s.dir]);
    assert.ok(s.messages.some((m) => m.includes('Connected: "RAG eval notes"') && m.includes("0.161.0")));
    assert.ok(s.messages.some((m) => m.includes("1) Thesis lit review") && m.includes("n) New project")));
  } finally {
    s.cleanup();
  }
});

test("n creates a project in the only workspace and pairs with it", async () => {
  const s = setup(["n", "Fresh idea"]);
  try {
    await rc(s.options);
    const create = s.calls.find((c) => c.method === "POST" && c.url.endsWith("/projects"));
    assert.deepEqual(create?.body, { workspace_id: WORKSPACE, name: "Fresh idea" });
    assert.equal(s.state().projectId, NEW_PROJECT);
  } finally {
    s.cleanup();
  }
});

test("a bad choice re-prompts instead of exiting", async () => {
  const s = setup(["9", "x", "1"]);
  try {
    await rc(s.options);
    assert.equal(s.prompts.length, 3);
    assert.equal(s.state().projectId, PROJECT_A);
  } finally {
    s.cleanup();
  }
});

test("the same project again makes no login or consent call", async () => {
  const first = setup(["1"]);
  try {
    await rc(first.options);
    const again = setup(["1"]);
    try {
      again.options.stateDir = first.dir;
      again.options.dir = first.folder;
      await rc(again.options);
      const u = again.urls();
      assert.equal(u.filter((x) => x.includes("/cli-auth/")).length, 0);
      assert.equal(u.filter((x) => x.endsWith("/grant-requests")).length, 0);
      assert.equal(u.filter((x) => x.includes("/workspaces") && !x.includes("/api/v2/")).length, 0, "folder registered once");
      assert.ok(again.messages.some((m) => m.startsWith("Reusing binding")));
    } finally {
      again.cleanup();
    }
  } finally {
    first.cleanup();
  }
});

test("a stored token NOUS rejects triggers exactly one new login", async () => {
  const first = setup(["1"]);
  try {
    await rc(first.options);
    const calls: Call[] = [];
    const inner = fakeNous(calls, { tokenOk: (auth) => auth === "Bearer cli-secret-2" });
    let logins = 0;
    const again = setup(["1"], (async (input: any, init: any) => {
      if (String(input).includes("/cli-auth/status/")) {
        logins += 1;
        return new Response(JSON.stringify({ status: "approved", token: "cli-secret-2" }), { status: 200 });
      }
      return inner(input, init);
    }) as typeof fetch);
    try {
      again.options.stateDir = first.dir;
      await rc(again.options);
      assert.equal(logins, 1);
      assert.equal(again.state().projectId, PROJECT_A);
    } finally {
      again.cleanup();
    }
  } finally {
    first.cleanup();
  }
});

test("a different project than the stored pairing prints the revoke hint and replaces it", async () => {
  const first = setup(["1"]);
  try {
    await rc(first.options);
    const again = setup(["2"]);
    try {
      again.options.stateDir = first.dir;
      await rc(again.options);
      assert.equal(again.state().projectId, PROJECT_B);
      assert.ok(again.messages.some((m) => m.includes(PROJECT_A) && m.includes("/integrations/devices")));
      assert.ok(again.urls().some((x) => x.endsWith("/grant-requests")));
    } finally {
      again.cleanup();
    }
  } finally {
    first.cleanup();
  }
});

test("a failed Codex probe makes no network call", async () => {
  const s = setup([]);
  try {
    s.options.probe = async () => {
      throw new Error("Codex 0.158.0 is not tested with NOUS; tested versions: 0.153.4, 0.161.0");
    };
    await assert.rejects(rc(s.options), /not tested with NOUS/);
    assert.equal(s.calls.length, 0);
  } finally {
    s.cleanup();
  }
});

test("a 503 from the integration endpoints says Local Codex is off", async () => {
  const calls: Call[] = [];
  const s = setup(["1"], fakeNous(calls, { status: 503 }));
  try {
    await assert.rejects(rc(s.options), /^Error: Local Codex is turned off on this NOUS server$/);
    assert.equal(s.runs.length, 0);
  } finally {
    s.cleanup();
  }
});

test("project names are printed without control characters", async () => {
  const calls: Call[] = [];
  const plainFake = fakeNous(calls);
  const s = setup(["1"], (async (input: any, init: any) => {
    if (String(input).includes("/projects?"))
      return new Response(JSON.stringify({ projects: [{ id: PROJECT_A, name: "Bad\u001b[31mname\u0007", document_count: 1 }] }), { status: 200 });
    return plainFake(input, init);
  }) as typeof fetch);
  try {
    await rc(s.options);
    assert.ok(s.messages.some((m) => m.includes("1) Badname")));
    assert.ok(!s.messages.some((m) => m.includes("\u001b")));
  } finally {
    s.cleanup();
  }
});
