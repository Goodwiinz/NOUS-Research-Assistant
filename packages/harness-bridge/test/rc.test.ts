import assert from "node:assert/strict";
import { test } from "node:test";
import { existsSync, mkdtempSync, readFileSync, realpathSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { basename, join } from "node:path";
import { addWorkspace, connect, rc } from "../src/cli.ts";

const PROJECT_A = "11111111-1111-4111-8111-111111111111";
const WORKSPACE = "77777777-7777-4777-8777-777777777777";
const DEVICE = "22222222-2222-4222-8222-222222222222";
const GRANT = "33333333-3333-4333-8333-333333333333";
const SCOPES = ["harness:execute", "tools:read", "tools:write", "library:read", "library:write"];
const APPROVE = `Approve this device's access to your whole NOUS account in your browser: https://nous.test/integrations/approve?request_id=${GRANT}`;
const UNSUPPORTED = "this NOUS server doesn't support account connections yet";

type Call = { url: string; method: string; auth?: string; body?: any };

/**
 * Fake NOUS for rc: the sign-in check (`/auth/me`), and the login, consent
 * and folder endpoints `connect` uses. `tokenOk` decides whether a CLI token
 * is still accepted.
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
    if (url.endsWith("/auth/me")) return tokenOk(auth) ? json({ id: "user" }) : json({ detail: "expired" }, 401);
    if (url.endsWith("/cli-auth/start"))
      return json({ session_id: "s", poll_token: "p", browser_url: "https://nous.test/l", verification_code: "ABCD-1234" });
    if (url.includes("/cli-auth/status/")) return json({ status: "approved", token: "cli-secret" });
    if (url.endsWith("/integrations/devices")) return json({ id: DEVICE });
    // NOUS sends the approval page root-relative (integrations/context.py).
    if (url.endsWith("/grant-requests")) return json({ id: GRANT, approval_url: `/integrations/approve?request_id=${GRANT}` });
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
const folderCall = (c: Call) => /\/integrations\/devices\/[^/]+\/workspaces$/.test(c.url);

function setup(fake?: typeof fetch) {
  const dir = mkdtempSync(join(tmpdir(), "nous-rc-"));
  const folder = realpathSync(mkdtempSync(join(tmpdir(), "nous-rc-folder-")));
  const calls: Call[] = [];
  const messages: string[] = [];
  const runs: string[] = [];
  const options = {
    stateDir: dir,
    apiUrl: "https://nous.test/api/v1",
    dir: folder,
    label: "Laptop",
    // Unset: the real home folder.
    home: undefined as string | undefined,
    fetchFn: fake ?? fakeNous(calls),
    announce: (m: string) => messages.push(m),
    delay: async () => {},
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
  return { dir, folder, calls, urls, messages, runs, options, state, credential, cleanup };
}

test("pairs the whole account with five scopes, registers the folder as an account folder, runs", async () => {
  const s = setup();
  try {
    await rc(s.options);
    assert.equal(s.state().account, true);
    assert.equal("projectId" in s.state(), false);
    assert.deepEqual(s.state().scopes, SCOPES);
    assert.deepEqual(s.calls.find((c) => c.url.endsWith("/grant-requests"))?.body, {
      account: true,
      device_id: DEVICE,
      scopes: SCOPES,
    });
    const registered = s.calls.filter(folderCall);
    assert.equal(registered.length, 1);
    assert.equal(registered[0]!.body.label, basename(s.folder));
    assert.equal("project_id" in registered[0]!.body, false);
    assert.deepEqual(s.state().workspaces.map((w: any) => [w.root, "projectId" in w]), [[s.folder, false]]);
    assert.deepEqual(s.runs, [s.dir]);
    assert.ok(s.messages.includes(APPROVE), s.messages.join("\n"));
    assert.ok(s.messages.includes(`Connected: your NOUS account ↔ ${s.folder}  (Codex 0.161.0)`));
    assert.ok(
      s.messages.includes(`In any chat on NOUS, choose Local Codex → Laptop → ${basename(s.folder)}.\nPress Ctrl-C to stop.`),
    );
    // No project picker, list or creation any more.
    assert.equal(s.calls.some((c) => c.url.includes("/projects") || c.url.includes("/api/v2/")), false);
  } finally {
    s.cleanup();
  }
});

test("a second run reuses the account pairing with no login or consent call", async () => {
  const first = setup();
  try {
    await rc(first.options);
    const again = setup();
    try {
      again.options.stateDir = first.dir;
      again.options.dir = first.folder;
      await rc(again.options);
      const u = again.urls();
      assert.equal(u.filter((x) => x.includes("/cli-auth/")).length, 0);
      assert.equal(u.filter((x) => x.endsWith("/grant-requests")).length, 0);
      assert.equal(again.calls.filter(folderCall).length, 0, "folder registered once");
      assert.ok(again.messages.some((m) => m.startsWith("Reusing binding: your NOUS account")));
      assert.equal(again.messages.some((m) => m.includes("stays active until you revoke")), false);
    } finally {
      again.cleanup();
    }
  } finally {
    first.cleanup();
  }
});

test("a stored token NOUS rejects triggers exactly one new login, and the reused pairing keeps it", async () => {
  const first = setup();
  try {
    await rc(first.options);
    const calls: Call[] = [];
    const inner = fakeNous(calls, { tokenOk: (auth) => auth === "Bearer cli-secret-2" });
    let logins = 0;
    const again = setup((async (input: any, init: any = {}) => {
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
      assert.equal(again.state().account, true);
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
  const first = setup();
  try {
    await rc(first.options);
    const calls: Call[] = [];
    const fake = override(
      fakeNous(calls),
      (url) => url.includes("/cli-auth/status/"),
      () => jsonResponse({ status: "approved", token: "cli-other" }),
    );
    const again = setup(fake);
    try {
      again.options.stateDir = first.dir;
      again.options.apiUrl = "https://other.test/api/v1";
      await rc(again.options);
      const other = calls.filter((c) => c.url.startsWith("https://other.test/"));
      assert.ok(other.length > 0);
      assert.equal(other.filter((c) => c.auth === "Bearer cli-secret").length, 0);
      assert.equal(calls.filter((c) => c.url.endsWith("/cli-auth/start")).length, 1);
      // A server switch prints no revoke note.
      assert.equal(again.messages.some((m) => m.includes("stays active until you revoke")), false);
    } finally {
      again.cleanup();
    }
  } finally {
    first.cleanup();
  }
});

test("a stored project pairing is replaced: revoke hint after the new pairing, folders re-registered as account folders", async () => {
  const s = setup();
  const kept = realpathSync(mkdtempSync(join(tmpdir(), "nous-rc-kept-")));
  try {
    const quiet = { stateDir: s.dir, fetchFn: s.options.fetchFn, announce: () => {}, delay: async () => {} };
    await connect({ ...quiet, apiUrl: s.options.apiUrl, projectId: PROJECT_A, label: "Laptop", tools: true });
    await addWorkspace({ ...quiet, root: kept });
    const mark = s.calls.length;
    await rc(s.options);
    const after = s.calls.slice(mark);
    assert.equal(s.state().account, true);
    assert.equal(after.find((c) => c.url.endsWith("/grant-requests"))?.body.account, true);
    const registered = after.filter(folderCall);
    assert.deepEqual(registered.map((c) => [c.body.label, "project_id" in c.body]), [
      [basename(kept), false],
      [basename(s.folder), false],
    ]);
    assert.deepEqual(s.state().workspaces.map((w: any) => [w.root, "projectId" in w]), [
      [kept, false],
      [s.folder, false],
    ]);
    const connected = s.messages.indexOf("Connected to your NOUS account.");
    const hint = s.messages.findIndex(
      (m) =>
        m ===
        `Your previous approval for project ${PROJECT_A} stays active until you revoke it under Settings → Connected devices (/integrations/devices) in NOUS.`,
    );
    assert.ok(connected >= 0 && hint > connected, s.messages.join("\n"));
  } finally {
    s.cleanup();
    rmSync(kept, { recursive: true, force: true });
  }
});

test("a later run that reuses the stored sign-in still prints the full approval link", async () => {
  const s = setup();
  try {
    // A project pairing made by hand: its login recorded the website origin.
    await connect({
      stateDir: s.dir,
      fetchFn: s.options.fetchFn,
      announce: () => {},
      delay: async () => {},
      apiUrl: s.options.apiUrl,
      projectId: PROJECT_A,
      label: "Laptop",
      tools: true,
    });
    const mark = s.calls.length;
    await rc(s.options);
    assert.equal(s.calls.slice(mark).filter((c) => c.url.includes("/cli-auth/")).length, 0, "no new sign-in");
    assert.ok(s.messages.includes(APPROVE), s.messages.join("\n"));
  } finally {
    s.cleanup();
  }
});

test("a previous workspace connection gets its own revoke hint", async () => {
  const s = setup();
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
    assert.equal(s.state().account, true);
    assert.ok(s.messages.some((m) => m.startsWith("Your previous workspace connection") && m.includes(WORKSPACE)));
  } finally {
    s.cleanup();
  }
});

test("an old NOUS that answers the account request with 422 says it does not support account connections yet", async () => {
  const calls: Call[] = [];
  const s = setup(
    override(
      fakeNous(calls),
      (url, init) => url.endsWith("/grant-requests") && init.method === "POST",
      () => jsonResponse({ detail: [{ msg: "Extra inputs are not permitted" }] }, 422),
    ),
  );
  try {
    await assert.rejects(rc(s.options), { message: UNSUPPORTED });
    assert.equal(existsSync(join(s.dir, "connection.json")), false);
    assert.equal(s.runs.length, 0);
  } finally {
    s.cleanup();
  }
});

test("account connections switched off say the same; any other 503 passes through", async () => {
  for (const [detail, message] of [
    ["Account connections are disabled", UNSUPPORTED],
    ["Service unavailable", "NOUS request failed (503) /integrations/grant-requests"],
  ] as const) {
    const calls: Call[] = [];
    const s = setup(
      override(
        fakeNous(calls),
        (url, init) => url.endsWith("/grant-requests") && init.method === "POST",
        () => jsonResponse({ detail }, 503),
      ),
    );
    try {
      await assert.rejects(rc(s.options), { message });
      assert.equal(s.runs.length, 0);
    } finally {
      s.cleanup();
    }
  }
});

test("a failed Codex probe makes no network call", async () => {
  const s = setup();
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
  const s = setup();
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

test("a folder too broad for Codex is refused before any network call", async () => {
  const s = setup();
  const tooBroad = (root: string) => ({
    message: `${root} is too broad for Codex to work in; run nous --rc in a project folder`,
  });
  try {
    s.options.home = s.folder;
    await assert.rejects(rc(s.options), tooBroad(s.folder));
    s.options.home = undefined;
    s.options.dir = "/";
    await assert.rejects(rc(s.options), tooBroad("/"));
    s.options.dir = s.folder;
    s.options.stateDir = join(s.folder, "state");
    await assert.rejects(rc(s.options), tooBroad(s.folder));
    assert.equal(s.calls.length, 0);
    assert.equal(s.runs.length, 0);
  } finally {
    s.cleanup();
  }
});

test("a project folder inside the home folder still works", async () => {
  const s = setup();
  try {
    s.options.home = realpathSync(tmpdir());
    await rc(s.options);
    assert.equal(s.state().workspaces[0].root, s.folder);
    assert.deepEqual(s.runs, [s.dir]);
  } finally {
    s.cleanup();
  }
});

test("a server link that is not http(s) is refused", async () => {
  const cases = [
    {
      match: (url: string) => url.endsWith("/cli-auth/start"),
      reply: { session_id: "s", poll_token: "p", browser_url: "javascript:alert(1)", verification_code: "ABCD-1234" },
    },
    ...["javascript:alert(1)", "//evil.example/x", "/\\evil.example/x", "/\t/evil.example/x"].map((link) => ({
      match: (url: string) => url.endsWith("/grant-requests"),
      reply: { id: GRANT, approval_url: link },
    })),
  ];
  for (const { match, reply } of cases) {
    const calls: Call[] = [];
    const s = setup(override(fakeNous(calls), match, () => jsonResponse(reply)));
    try {
      await assert.rejects(rc(s.options), { message: "invalid NOUS response" });
      assert.ok(!s.messages.some((m) => m.includes("javascript:")));
      assert.equal(s.runs.length, 0);
    } finally {
      s.cleanup();
    }
  }
});

test("control characters in a server link are percent-encoded", async () => {
  const calls: Call[] = [];
  const login = override(
    fakeNous(calls),
    (url) => url.endsWith("/cli-auth/start"),
    () =>
      jsonResponse({
        session_id: "s",
        poll_token: "p",
        browser_url: "https://nous.test/l\u001b[31m",
        verification_code: "ABCD-1234",
      }),
  );
  const fake = override(
    login,
    (url) => url.endsWith("/grant-requests"),
    () => jsonResponse({ id: GRANT, approval_url: "https://nous.test/a\u001b[31m" }),
  );
  const s = setup(fake);
  try {
    await rc(s.options);
    assert.ok(s.messages.some((m) => m.includes("https://nous.test/l%1B[31m")));
    assert.ok(s.messages.some((m) => m.includes("https://nous.test/a%1B[31m")));
    assert.ok(!s.messages.some((m) => m.includes("\u001b")));
  } finally {
    s.cleanup();
  }
});

test("an unreachable NOUS is named instead of a bare fetch failed", async () => {
  const unreachable = (async () => {
    throw new TypeError("fetch failed", {
      cause: Object.assign(new Error("connect ECONNREFUSED"), { code: "ECONNREFUSED" }),
    });
  }) as typeof fetch;
  const s = setup(unreachable);
  try {
    await assert.rejects(rc(s.options), (error: Error) => {
      assert.equal(error.message, "Could not reach NOUS at https://nous.test/api/v1 (ECONNREFUSED)");
      assert.ok(error.cause instanceof TypeError);
      return true;
    });
    assert.equal(s.runs.length, 0);
  } finally {
    s.cleanup();
  }
});

test("a 503 from the pairing endpoints passes through unchanged", async () => {
  const calls: Call[] = [];
  const s = setup(fakeNous(calls, { status: 503 }));
  try {
    await assert.rejects(rc(s.options), /NOUS request failed \(503\) \/integrations\/devices/);
    assert.equal(s.runs.length, 0);
  } finally {
    s.cleanup();
  }
});

test("a second 401 after a fresh sign-in is an error", async () => {
  const calls: Call[] = [];
  const s = setup(fakeNous(calls, { tokenOk: () => false }));
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
  const s = setup(fake);
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
  const s = setup(fake);
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
