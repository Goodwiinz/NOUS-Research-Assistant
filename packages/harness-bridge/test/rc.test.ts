import assert from "node:assert/strict";
import { test } from "node:test";
import { existsSync, mkdtempSync, readFileSync, realpathSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { basename, join } from "node:path";
import { connect, rc } from "../src/cli.ts";

const PROJECT_A = "11111111-1111-4111-8111-111111111111";
const PROJECT_B = "12121212-1212-4121-8121-121212121212";
const NEW_PROJECT = "13131313-1313-4131-8131-131313131313";
const WORKSPACE = "77777777-7777-4777-8777-777777777777";
const WORKSPACE_2 = "78787878-7878-4787-8787-878787878787";
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

/** Wraps a fake so one URL test answers with `reply` instead. */
function override(inner: typeof fetch, match: (url: string, init: any) => boolean, reply: () => Response): typeof fetch {
  return (async (input: any, init: any = {}) => (match(String(input), init) ? reply() : inner(input, init))) as typeof fetch;
}
const jsonResponse = (data: unknown, status = 200) => new Response(JSON.stringify(data), { status });

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
  const credential = () => JSON.parse(readFileSync(join(options.stateDir, `${state().credentialHandle}.json`), "utf8"));
  const urls = () => calls.map((c) => c.url);
  const cleanup = () => {
    rmSync(dir, { recursive: true, force: true });
    rmSync(folder, { recursive: true, force: true });
  };
  return { dir, folder, calls, urls, messages, prompts, runs, options, state, credential, cleanup };
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
    // The summary names what the web chat shows: the device and folder labels.
    assert.ok(s.messages.some((m) => m.includes(`choose Local Codex → Laptop → ${basename(s.folder)}.`)));
    assert.ok(!s.messages.some((m) => m.includes("Showing the first")));
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

test("n with several workspaces asks which one and re-prompts on a bad pick", async () => {
  const calls: Call[] = [];
  const fake = override(
    fakeNous(calls),
    (url) => url.endsWith("/api/v2/workspaces"),
    () => jsonResponse([{ id: WORKSPACE, name: "Mine" }, { id: WORKSPACE_2, name: "Team" }]),
  );
  const s = setup(["n", "Idea", "5", "2"], fake);
  try {
    await rc(s.options);
    const create = calls.find((c) => c.method === "POST" && c.url.endsWith("/projects"));
    assert.deepEqual(create?.body, { workspace_id: WORKSPACE_2, name: "Idea" });
    assert.equal(s.prompts.filter((q) => q.startsWith("Pick a workspace")).length, 2);
    assert.ok(s.messages.some((m) => m.includes("1) Mine") && m.includes("2) Team")));
    assert.equal(s.state().projectId, NEW_PROJECT);
  } finally {
    s.cleanup();
  }
});

test("a bad choice re-prompts instead of exiting", async () => {
  const s = setup(["9", "x", "1abc", "1"]);
  try {
    await rc(s.options);
    assert.equal(s.prompts.length, 4);
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

test("a stored token NOUS rejects triggers exactly one new login, and the reused pairing keeps it", async () => {
  const first = setup(["1"]);
  try {
    await rc(first.options);
    const calls: Call[] = [];
    const inner = fakeNous(calls, { tokenOk: (auth) => auth === "Bearer cli-secret-2" });
    let logins = 0;
    const again = setup(["1"], (async (input: any, init: any = {}) => {
      const url = String(input);
      if (url.includes("/cli-auth/status/")) {
        logins += 1;
        return jsonResponse({ status: "approved", token: "cli-secret-2" });
      }
      // Grant renewal and folder registration sign with the stored CLI token.
      const signed = url.endsWith("/renew") || /\/integrations\/devices\/[^/]+\/workspaces$/.test(url);
      if (signed && init.headers?.Authorization !== "Bearer cli-secret-2") return jsonResponse({ detail: "expired" }, 401);
      return inner(input, init);
    }) as typeof fetch);
    try {
      again.options.stateDir = first.dir;
      // again.options.dir is a new folder, so this run registers it.
      await rc(again.options);
      assert.equal(logins, 1);
      assert.equal(again.state().projectId, PROJECT_A);
      assert.equal(calls.filter((c) => c.url.endsWith("/grant-requests")).length, 0, "the pairing was reused");
      assert.ok(again.state().workspaces.some((w: { root: string }) => w.root === again.folder));
      assert.equal(again.credential().accessToken, "cli-secret-2");
      assert.deepEqual(again.runs, [first.dir]);
    } finally {
      again.cleanup();
    }
  } finally {
    first.cleanup();
  }
});

test("a different NOUS never receives the stored sign-in", async () => {
  const first = setup(["1"]);
  try {
    await rc(first.options);
    const calls: Call[] = [];
    const fake = override(
      fakeNous(calls),
      (url) => url.includes("/cli-auth/status/"),
      () => jsonResponse({ status: "approved", token: "cli-other" }),
    );
    const again = setup(["1"], fake);
    try {
      again.options.stateDir = first.dir;
      again.options.apiUrl = "https://other.test/api/v1";
      await rc(again.options);
      const other = calls.filter((c) => c.url.startsWith("https://other.test/"));
      assert.ok(other.length > 0);
      assert.equal(other.filter((c) => c.auth === "Bearer cli-secret").length, 0);
      assert.equal(calls.filter((c) => c.url.endsWith("/cli-auth/start")).length, 1);
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
      // Named from the fetched list, and only once the new pairing exists.
      const hint = again.messages.findIndex((m) => m.includes(`"Thesis lit review" (${PROJECT_A})`));
      const connected = again.messages.findIndex((m) => m.startsWith(`Connected to project ${PROJECT_B}`));
      assert.ok(connected >= 0 && hint > connected);
    } finally {
      again.cleanup();
    }
  } finally {
    first.cleanup();
  }
});

test("a previous workspace connection gets its own revoke hint", async () => {
  const s = setup(["1"]);
  try {
    await connect({
      stateDir: s.dir,
      fetchFn: s.options.fetchFn,
      announce: () => {},
      delay: async () => {},
      apiUrl: s.options.apiUrl,
      workspaceId: WORKSPACE,
      label: "Laptop",
      tools: true,
    });
    await rc(s.options);
    assert.equal(s.state().projectId, PROJECT_A);
    assert.ok(s.messages.some((m) => m.startsWith("Your previous workspace connection") && m.includes(WORKSPACE)));
  } finally {
    s.cleanup();
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

test("a folder that does not exist fails before any network call", async () => {
  const s = setup([]);
  try {
    s.options.dir = join(s.folder, "missing");
    await assert.rejects(rc(s.options), /missing is not a folder$/);
    const file = join(s.folder, "notes.txt");
    writeFileSync(file, "x");
    s.options.dir = file;
    await assert.rejects(rc(s.options), /notes\.txt is not a folder$/);
    assert.equal(s.calls.length, 0);
    assert.equal(s.runs.length, 0);
  } finally {
    s.cleanup();
  }
});

test("a 503 from the pairing endpoints passes through unchanged", async () => {
  const calls: Call[] = [];
  const s = setup(["1"], fakeNous(calls, { status: 503 }));
  try {
    await assert.rejects(rc(s.options), /NOUS request failed \(503\) \/integrations\/devices/);
    assert.equal(s.runs.length, 0);
  } finally {
    s.cleanup();
  }
});

test("a second 401 after a fresh sign-in is an error", async () => {
  const calls: Call[] = [];
  const s = setup([], fakeNous(calls, { tokenOk: () => false }));
  try {
    await assert.rejects(rc(s.options), /^Error: NOUS refused the new sign-in$/);
    assert.equal(calls.filter((c) => c.url.endsWith("/cli-auth/start")).length, 1);
    assert.equal(s.runs.length, 0);
  } finally {
    s.cleanup();
  }
});

test("a refused approval says to run nous --rc again", async () => {
  const calls: Call[] = [];
  const fake = override(
    fakeNous(calls),
    (url) => url.includes("/grant-requests/") && !url.endsWith("/exchange"),
    () => jsonResponse({ status: "denied" }),
  );
  const s = setup(["1"], fake);
  try {
    await assert.rejects(rc(s.options), (error: Error) => {
      assert.equal(error.message, "Not approved; run nous --rc again");
      assert.equal((error.cause as Error | undefined)?.message, "authorization denied or expired");
      return true;
    });
    assert.equal(existsSync(join(s.dir, "connection.json")), false);
    assert.equal(s.runs.length, 0);
  } finally {
    s.cleanup();
  }
});

test("a refused sign-in says to run nous --rc again", async () => {
  const calls: Call[] = [];
  const fake = override(
    fakeNous(calls),
    (url) => url.includes("/cli-auth/status/"),
    () => jsonResponse({ status: "denied" }),
  );
  const s = setup([], fake);
  try {
    await assert.rejects(rc(s.options), (error: Error) => {
      assert.equal(error.message, "Not approved; run nous --rc again");
      assert.equal((error.cause as Error | undefined)?.message, "authorization denied or expired");
      return true;
    });
    assert.equal(existsSync(join(s.dir, "connection.json")), false);
    assert.equal(s.runs.length, 0);
  } finally {
    s.cleanup();
  }
});

test("creating a project fails closed on an unexpected API path or workspaces body", async () => {
  const odd = setup(["n"]);
  try {
    odd.options.apiUrl = "https://nous.test/api";
    await assert.rejects(rc(odd.options), /^Error: cannot derive the workspaces endpoint from https:\/\/nous\.test\/api$/);
  } finally {
    odd.cleanup();
  }
  const calls: Call[] = [];
  const fake = override(
    fakeNous(calls),
    (url) => url.endsWith("/api/v2/workspaces"),
    () => jsonResponse({ workspaces: [] }),
  );
  const bad = setup(["n", "Idea"], fake);
  try {
    await assert.rejects(rc(bad.options), /^Error: invalid NOUS response$/);
    assert.equal(calls.filter((c) => c.method === "POST" && c.url.endsWith("/projects")).length, 0);
  } finally {
    bad.cleanup();
  }
});

test("project names are printed without control characters", async () => {
  const calls: Call[] = [];
  const fake = override(
    fakeNous(calls),
    (url) => url.includes("/projects?"),
    () =>
      jsonResponse({
        projects: [
          { id: PROJECT_A, name: "Bad\u001b[31mname\u0007", document_count: 1 },
          { id: PROJECT_B, name: "\u001b]8;;https://x\u0007Link\u001b]8;;\u0007", document_count: 2 },
        ],
      }),
  );
  const s = setup(["1"], fake);
  try {
    await rc(s.options);
    assert.ok(s.messages.some((m) => m.includes("1) Badname")));
    assert.ok(s.messages.some((m) => m.includes("2) Link  2 docs")));
    assert.ok(!s.messages.some((m) => m.includes("\u001b") || m.includes("\u0007")));
  } finally {
    s.cleanup();
  }
});

test("a truncated project list says so", async () => {
  for (const [extra, note] of [
    [{ has_next: true, total: 73 }, "Showing the first 50 of 73 projects."],
    [{ has_next: true }, "Showing the first 50 projects."],
  ] as const) {
    const calls: Call[] = [];
    const fake = override(
      fakeNous(calls),
      (url) => url.includes("/projects?"),
      () => jsonResponse({ projects: [{ id: PROJECT_A, name: "Only", document_count: 1 }], ...extra }),
    );
    const s = setup(["1"], fake);
    try {
      await rc(s.options);
      assert.ok(s.messages.some((m) => m.includes("1) Only") && m.includes(note)), note);
    } finally {
      s.cleanup();
    }
  }
});
