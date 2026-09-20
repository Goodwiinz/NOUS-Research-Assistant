import assert from "node:assert/strict";
import { spawn } from "node:child_process";
import { mkdtemp, readFile, rm, stat, writeFile } from "node:fs/promises";
import { createServer } from "node:http";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { fileURLToPath } from "node:url";
import { after, before, test } from "node:test";

const helper = fileURLToPath(new URL("../auth.mjs", import.meta.url));
const password = " synthetic password with spaces! ";
// Next.js keeps a visible ARIA route announcer outside the login form.
// Its text can change during navigation without indicating an auth failure.
const routeAnnouncer =
  '<div role="alert" id="__next-route-announcer__">Sign In | NOUS</div>';
let origin;
const server = createServer(async (request, response) => {
  response.setHeader("content-type", "text/html");
  if (request.method === "POST") {
    let body = "";
    for await (const chunk of request) body += chunk;
    // Model an asynchronous login: clicking submit finishes before auth does.
    await new Promise((resolve) => setTimeout(resolve, 100));
    const form = new URLSearchParams(body);
    if (
      form.get("email") === "qa@example.test" &&
      form.get("password") === password
    ) {
      response.writeHead(303, {
        location: "/chat",
        "set-cookie": "test_session=authenticated; Path=/; HttpOnly",
      });
      response.end();
    } else {
      response.end('<p role="alert">Invalid credentials</p>');
    }
    return;
  }
  if (
    request.url === "/chat" &&
    request.headers.cookie?.includes("test_session=authenticated")
  ) {
    response.end(`${routeAnnouncer}<button>Sign out</button>`);
    return;
  }
  response.end(`${routeAnnouncer}<form method="post" action="/login">
    <input name="email" data-testid="email-input">
    <input name="password" type="password" data-testid="password-input">
    <button data-testid="login-button">Sign in</button>
  </form><script>
    const form = document.querySelector('form');
    form.addEventListener('submit', async event => {
      event.preventDefault();
      const response = await fetch('/login', {
        method: 'POST', body: new URLSearchParams(new FormData(form)),
      });
      if (response.redirected) location.assign(response.url);
      else form.insertAdjacentHTML('afterbegin', await response.text());
    });
  </script>`);
});

before(async () => {
  await new Promise((resolve) => server.listen(0, "127.0.0.1", resolve));
  origin = `http://127.0.0.1:${server.address().port}`;
});
after(async () => {
  await new Promise((resolve) => server.close(resolve));
});

async function runAuth(args, environment, statePath) {
  const env = { ...process.env };
  for (const name of Object.keys(env)) {
    if (
      /^(NOUS_|AGENT_QA_)/.test(name) ||
      ["DISPLAY", "WAYLAND_DISPLAY"].includes(name)
    )
      delete env[name];
  }
  const child = spawn(process.execPath, [helper, ...args], {
    env: {
      ...env,
      NOUS_BASE_URL: origin,
      NOUS_AUTH_STATE: statePath,
      ...environment,
    },
    stdio: ["ignore", "pipe", "pipe"],
    timeout: 20_000,
  });
  let output = "";
  child.stdout.on("data", (chunk) => {
    output += chunk;
  });
  child.stderr.on("data", (chunk) => {
    output += chunk;
  });
  const code = await new Promise((resolve, reject) => {
    child.on("error", reject);
    child.on("close", resolve);
  });
  return { code, output };
}

// Mutation check (2026-09-19): removing the form scope in auth.mjs:64
// makes this test fail on the global route announcement; restoring it passes.
// node --test --test-name-pattern='ignores route announcements' tools/nous-playwright/unit/auth.test.mjs
test("headless login ignores route announcements and saves a private reusable session", async () => {
  const directory = await mkdtemp(join(tmpdir(), "nous-auth-"));
  try {
    const statePath = join(directory, "state.json");
    const result = await runAuth(
      ["--headless"],
      { NOUS_EMAIL: "qa@example.test", NOUS_PASSWORD: password },
      statePath,
    );
    assert.equal(result.code, 0, result.output);
    assert.doesNotMatch(result.output, /Missing X server|synthetic password/);
    const state = JSON.parse(await readFile(statePath, "utf8"));
    assert.equal(
      state.cookies.find((cookie) => cookie.name === "test_session")?.value,
      "authenticated",
    );
    assert.equal((await stat(statePath)).mode & 0o777, 0o600);
  } finally {
    await rm(directory, { recursive: true });
  }
});

test(
  "Linux without DISPLAY automatically uses headless and accepts the Q&A credentials",
  { skip: process.platform !== "linux" },
  async () => {
    const directory = await mkdtemp(join(tmpdir(), "nous-auth-"));
    try {
      const result = await runAuth(
        [],
        { AGENT_QA_EMAIL: "qa@example.test", AGENT_QA_PASSWORD: password },
        join(directory, "state.json"),
      );
      assert.equal(result.code, 0, result.output);
      assert.doesNotMatch(result.output, /Missing X server|synthetic password/);
    } finally {
      await rm(directory, { recursive: true });
    }
  },
);

test("missing credentials fail with actionable instructions before launching Chromium", async () => {
  const result = await runAuth(["--headless"], {}, "/unused-session.json");
  assert.equal(result.code, 1);
  assert.match(result.output, /NOUS_EMAIL and NOUS_PASSWORD/);
  assert.doesNotMatch(result.output, /<launching>|Missing X server/);
});

test("rejected login preserves the existing saved state and does not print the password", async () => {
  const directory = await mkdtemp(join(tmpdir(), "nous-auth-"));
  try {
    const statePath = join(directory, "state.json");
    await writeFile(statePath, "previous state");
    const result = await runAuth(
      ["--headless"],
      {
        NOUS_EMAIL: "qa@example.test",
        NOUS_PASSWORD: "incorrect-private-password",
      },
      statePath,
    );
    assert.equal(result.code, 1);
    assert.match(result.output, /Sign-in failed: Invalid credentials/);
    assert.doesNotMatch(result.output, /incorrect-private-password/);
    assert.equal(await readFile(statePath, "utf8"), "previous state");
  } finally {
    await rm(directory, { recursive: true });
  }
});
