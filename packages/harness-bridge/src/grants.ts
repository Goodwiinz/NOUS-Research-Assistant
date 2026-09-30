import { CredentialStore, type IntegrationCredentials } from "./credentials.ts";

/** Grants live 15 minutes and cannot be renewed once expired; renew well before. */
export const GRANT_RENEW_AFTER_MS = 10 * 60_000;
/** Long-running processes check this often so an idle session never lapses. */
export const GRANT_CHECK_INTERVAL_MS = 2 * 60_000;
export const GRANT_EXPIRED_MESSAGE =
  "NOUS grant expired before it could be renewed; reconnect this device (nous-harness connect)";

const uuid = (value: unknown): value is string =>
  typeof value === "string" &&
  /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i.test(value);

export class GrantExpired extends Error {
  constructor() {
    super(GRANT_EXPIRED_MESSAGE);
  }
}

/**
 * Keeps one stored grant fresh for every process sharing the credential
 * handle (the bridge and each MCP child). The store is the source of truth:
 * each check re-reads it, so a renewal by another process is picked up, and a
 * renewal here is written back atomically under the same handle.
 */
export class GrantKeeper {
  private inflight: Promise<IntegrationCredentials> | undefined;
  private warnedLegacy = false;
  constructor(
    private readonly store: CredentialStore,
    private readonly handle: string,
    private readonly apiBase: string,
    private readonly fetchFn: typeof fetch = fetch,
    private readonly now: () => number = Date.now,
    private readonly pause: (ms: number) => Promise<void> = (ms) =>
      new Promise((resolve) => setTimeout(resolve, ms)),
  ) {}

  /** Current credentials, renewed first when the grant is due. */
  current(): Promise<IntegrationCredentials> {
    // One renewal at a time per process; concurrent callers share it.
    this.inflight ??= this.refresh().finally(() => {
      this.inflight = undefined;
    });
    return this.inflight;
  }

  /** A fetch that sends the current grant, for the scoped HTTPS clients. */
  readonly fetch: typeof fetch = async (input, init) => {
    const credentials = await this.current();
    const headers = new Headers(init?.headers);
    headers.set("Authorization", `Bearer ${credentials.accessToken}`);
    headers.set("X-NOUS-Integration-Grant", credentials.grantToken);
    return this.fetchFn(input, { ...init, headers });
  };

  /** Renew in the background of a long-running process; returns a stopper. */
  keepFresh(onError: (error: unknown) => void = () => {}): () => void {
    const timer = setInterval(() => {
      this.current().catch(onError);
    }, GRANT_CHECK_INTERVAL_MS);
    timer.unref?.();
    return () => clearInterval(timer);
  }

  private async refresh(): Promise<IntegrationCredentials> {
    const stored = await this.store.load(this.handle);
    if (!uuid(stored.grantId) || typeof stored.renewedAt !== "number") {
      if (!this.warnedLegacy) {
        this.warnedLegacy = true;
        console.error(
          "This connection predates grant renewal; it stops after 15 minutes. Reconnect with nous-harness connect to keep it alive.",
        );
      }
      return stored;
    }
    if (this.now() - stored.renewedAt < GRANT_RENEW_AFTER_MS) return stored;
    return this.renew(stored);
  }

  private async renew(stored: IntegrationCredentials): Promise<IntegrationCredentials> {
    const response = await this.fetchFn(
      `${this.apiBase}/integrations/grants/${stored.grantId}/renew`,
      {
        method: "POST",
        redirect: "error",
        headers: {
          Authorization: `Bearer ${stored.accessToken}`,
          "X-NOUS-Integration-Grant": stored.grantToken,
        },
        signal: AbortSignal.timeout(30_000),
      },
    );
    if (response.ok) {
      const issued: unknown = await response.json().catch(() => null);
      const token = (issued as { token?: unknown } | null)?.token;
      const grantId = (issued as { grant_id?: unknown } | null)?.grant_id;
      if (typeof token !== "string" || !token || !uuid(grantId))
        throw new Error("invalid NOUS grant renewal");
      const renewed = { ...stored, grantToken: token, grantId, renewedAt: this.now() };
      await this.store.update(this.handle, renewed);
      return renewed;
    }
    // 409: another process sharing this handle renewed first and revoked the
    // token we hold. Its write lands shortly; read it back instead of failing.
    if (response.status === 409) {
      for (let attempt = 0; attempt < 5; attempt += 1) {
        const latest = await this.store.load(this.handle);
        if (latest.grantId !== stored.grantId) return latest;
        await this.pause(200);
      }
    }
    // 401/403 (expired, revoked or consent withdrawn) cannot be renewed.
    throw new GrantExpired();
  }
}
