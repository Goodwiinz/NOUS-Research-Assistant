import { CredentialStore, type IntegrationCredentials } from "./credentials.ts";

/** Grants live 15 minutes and cannot be renewed once expired; renew well before. */
export const GRANT_LIFETIME_MS = 15 * 60_000;
export const GRANT_RENEW_AFTER_MS = 10 * 60_000;
/** A pinned sequence starts on a grant at most this old, so it has ≥10 minutes. */
export const GRANT_PIN_FRESH_MS = 5 * 60_000;
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
  private pins = 0;
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
  current(renewAfterMs: number = GRANT_RENEW_AFTER_MS): Promise<IntegrationCredentials> {
    // One renewal at a time per process; concurrent callers share it.
    this.inflight ??= this.refresh(renewAfterMs).finally(() => {
      this.inflight = undefined;
    });
    return this.inflight;
  }

  /**
   * Run a multi-request sequence on one grant. The backend binds some state
   * to the exact grant (an artifact reservation must be finished by the grant
   * that made it), so no renewal happens in this process while it runs; it
   * starts on a grant young enough that other processes will not renew it
   * for several minutes either.
   */
  async pinned<T>(sequence: () => Promise<T>): Promise<T> {
    await this.current(GRANT_PIN_FRESH_MS);
    this.pins += 1;
    try {
      return await sequence();
    } finally {
      this.pins -= 1;
    }
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

  private async refresh(renewAfterMs: number): Promise<IntegrationCredentials> {
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
    const age = this.now() - stored.renewedAt;
    if (this.pins > 0 || age < renewAfterMs) return stored;
    return this.renew(stored, age);
  }

  private async renew(
    stored: IntegrationCredentials,
    age: number,
  ): Promise<IntegrationCredentials> {
    let response: Response;
    try {
      response = await this.fetchFn(
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
    } catch (error) {
      return this.transient(stored, age, error instanceof Error ? error.message : String(error));
    }
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
    if ([401, 403, 409].includes(response.status)) {
      // Another process sharing this handle may have renewed first: its
      // renewal revoked our token, so we see 409 (lost the swap) or 403/401
      // (token already rejected). Its write lands shortly; read it back.
      for (let attempt = 0; attempt < 5; attempt += 1) {
        const latest = await this.store.load(this.handle);
        if (latest.grantId !== stored.grantId) return latest;
        await this.pause(200);
      }
      // Nobody renewed: the grant is expired, revoked or its consent ended.
      throw new GrantExpired();
    }
    // 404/429/5xx: renewal is unavailable right now, not refused.
    return this.transient(stored, age, `NOUS grant renewal failed (${response.status})`);
  }

  /** Keep using a still-valid grant; the next check retries the renewal. */
  private transient(
    stored: IntegrationCredentials,
    age: number,
    reason: string,
  ): IntegrationCredentials {
    console.error(`${reason}; will retry`);
    if (age >= GRANT_LIFETIME_MS) throw new GrantExpired();
    return stored;
  }
}
