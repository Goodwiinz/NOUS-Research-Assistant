import { chromium } from "playwright";
import { mkdir, open } from "node:fs/promises";
import { dirname, resolve } from "node:path";
import { createInterface } from "node:readline/promises";
import { stdin, stdout } from "node:process";
import { Writable } from "node:stream";

async function question(message, { hidden = false } = {}) {
  let muted = false;
  const output = new Writable({
    write(chunk, encoding, done) {
      if (!muted) stdout.write(chunk, encoding);
      done();
    },
  });
  const prompt = createInterface({ input: stdin, output, terminal: true });
  const controller = new AbortController();
  prompt.once("SIGINT", () => controller.abort());
  prompt.once("close", () => controller.abort());
  try {
    const answer = prompt.question(message, { signal: controller.signal });
    muted = hidden;
    return await answer;
  } finally {
    prompt.close();
    output.end();
    if (hidden) stdout.write("\n");
  }
}

async function credentials() {
  const own =
    process.env.NOUS_EMAIL !== undefined ||
    process.env.NOUS_PASSWORD !== undefined;
  let email = own ? process.env.NOUS_EMAIL : process.env.AGENT_QA_EMAIL;
  let password = own
    ? process.env.NOUS_PASSWORD
    : process.env.AGENT_QA_PASSWORD;
  if ((!email?.trim() || !password) && !stdin.isTTY) {
    throw new Error(
      "Headless login needs NOUS_EMAIL and NOUS_PASSWORD (or AGENT_QA_EMAIL and AGENT_QA_PASSWORD). Run in an interactive terminal to enter them privately.",
    );
  }
  if (!email?.trim()) email = await question("Email: ");
  if (!password)
    password = await question("Password (hidden): ", { hidden: true });
  if (!email.trim() || !password)
    throw new Error("Email and password must not be empty.");
  return { email: email.trim(), password };
}

async function signIn(page, origin, account) {
  try {
    await page.getByTestId("email-input").fill(account.email);
    await page.getByTestId("password-input").fill(account.password);
    await page.getByTestId("login-button").click();
  } catch {
    throw new Error(
      "Could not submit the login form. Check that NOUS_BASE_URL points to the expected deployment.",
    );
  }
  // Next.js has a global role=alert route announcer, even before submission.
  // Only an error in this form indicates that sign-in was rejected.
  const formError = page
    .locator("form")
    .filter({ has: page.getByTestId("login-button") })
    .getByRole("alert")
    .filter({ hasText: /\S/ })
    .first();
  let rejection;
  try {
    rejection = await Promise.race([
      page
        .waitForURL(
          (url) =>
            url.origin === origin &&
            /^\/(dashboard|chat)(\/|$)/.test(url.pathname),
        )
        .then(() => undefined),
      formError.waitFor({ state: "visible" }).then(() => formError.innerText()),
    ]);
  } catch {
    throw new Error(
      "Sign-in did not reach the workspace before the timeout. Check the deployment's availability. For SSO or MFA, use --headed on a computer with a display.",
    );
  }
  if (rejection !== undefined) {
    const message = rejection
      .replaceAll(account.password, "[redacted]")
      .replaceAll(account.email, "[redacted]")
      .replace(/[\u0000-\u001f\u007f-\u009f]/g, " ")
      .slice(0, 500);
    throw new Error(`Sign-in failed: ${message}`);
  }
}

async function main() {
  const args = process.argv.slice(2);
  if (args.includes("--help")) {
    console.log(
      "Usage: pnpm auth [--headless | --headed]\nLinux without DISPLAY defaults to headless. Headless mode prompts privately or reads NOUS_EMAIL/NOUS_PASSWORD (also AGENT_QA_EMAIL/AGENT_QA_PASSWORD). NOUS_BASE_URL selects the site; NOUS_AUTH_STATE selects the saved session path.",
    );
    return;
  }
  if (
    args.some((arg) => !["--headless", "--headed"].includes(arg)) ||
    (args.includes("--headless") && args.includes("--headed"))
  ) {
    throw new Error("Use pnpm auth [--headless | --headed].");
  }
  const noDisplay = process.platform === "linux" && !process.env.DISPLAY;
  const headless =
    args.includes("--headless") || (noDisplay && !args.includes("--headed"));
  if (!headless && noDisplay) {
    throw new Error(
      "No DISPLAY is available. Use --headless here, or run --headed on a computer with a display.",
    );
  }
  if (!headless && !stdin.isTTY) {
    throw new Error(
      "Headed login needs an interactive terminal. Use --headless with environment credentials for unattended login.",
    );
  }
  let target;
  try {
    target = new URL(process.env.NOUS_BASE_URL ?? "https://goodwiinz.tech");
    if (
      !["https:", "http:"].includes(target.protocol) ||
      target.username ||
      target.password
    )
      throw new Error();
  } catch {
    throw new Error(
      "NOUS_BASE_URL must be an HTTP(S) URL without embedded credentials.",
    );
  }
  const origin = target.origin;
  const statePath = resolve(process.env.NOUS_AUTH_STATE ?? ".auth/nous.json");
  console.log(`Signing in to ${origin} (${headless ? "headless" : "headed"}).`);
  const account = headless ? await credentials() : undefined;
  const browser = await chromium.launch({ headless });
  try {
    // Authentication contexts intentionally have no video or trace recording.
    const context = await browser.newContext();
    context.setDefaultTimeout(30_000);
    const page = await context.newPage();
    await page.goto(`${origin}/login`);
    if (account) {
      await signIn(page, origin, account);
    } else {
      console.log("Sign in to NOUS in the opened browser.");
      await question(
        "When the signed-in workspace is visible, press Enter here. ",
      );
    }
    if (new URL(page.url()).origin !== origin) {
      throw new Error(
        "Return to the NOUS workspace before saving authentication.",
      );
    }
    await page
      .getByRole("button", { name: "Sign out", exact: true })
      .waitFor({ state: "visible" });
    const state = await context.storageState({ indexedDB: true });
    await mkdir(dirname(statePath), { recursive: true, mode: 0o700 });
    const file = await open(statePath, "w", 0o600);
    try {
      await file.chmod(0o600);
      await file.writeFile(JSON.stringify(state, null, 2));
    } finally {
      await file.close();
    }
    console.log(
      `Authenticated state saved to ${statePath}. Keep this file private.`,
    );
  } finally {
    await browser.close();
  }
}

main().catch((error) => {
  console.error(
    error.name === "AbortError"
      ? "Authentication cancelled."
      : error.message.split("\n")[0],
  );
  process.exitCode = 1;
});
