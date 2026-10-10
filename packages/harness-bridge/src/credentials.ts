import { constants } from "node:fs";
import { lstat, mkdir, open, readdir, rename, unlink } from "node:fs/promises";
import { randomUUID } from "node:crypto";
import { join, resolve } from "node:path";
import { record } from "./rpc.ts";

export type IntegrationCredentials = {
  accessToken: string;
  grantToken: string;
  // Present on connections made since grant renewal; see grants.ts.
  grantId?: string;
  renewedAt?: number;
};
export function integrationHeaders(
  credentials: IntegrationCredentials,
): Record<string, string> {
  return {
    Authorization: `Bearer ${credentials.accessToken}`,
    "X-NOUS-Integration-Grant": credentials.grantToken,
  };
}
const validHandle =
  /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/;
function ownerOnly(
  stat: { uid: number; mode: number },
  directory: boolean,
): void {
  if (
    typeof process.getuid !== "function" ||
    stat.uid !== process.getuid() ||
    (stat.mode & 0o777) !== (directory ? 0o700 : 0o600)
  )
    throw new Error("unsafe owner or permissions");
}
/** Owner-only POSIX storage. Windows requires a separate ACL/keychain implementation. */
export class CredentialStore {
  readonly directory: string;
  constructor(directory: string) {
    this.directory = resolve(directory);
  }
  private async ready(): Promise<void> {
    await mkdir(this.directory, { recursive: true, mode: 0o700 });
    const stat = await lstat(this.directory);
    if (stat.isSymbolicLink() || !stat.isDirectory())
      throw new Error("credential directory is a symlink or not a directory");
    ownerOnly(stat, true);
  }
  private path(name: string): string {
    if (!/^[a-zA-Z0-9-]+$/.test(name)) throw new Error("invalid storage name");
    return join(this.directory, name + ".json");
  }
  async writeLocal(name: string, value: unknown): Promise<void> {
    await this.ready();
    const destination = this.path(name);
    try {
      const st = await lstat(destination);
      if (st.isSymbolicLink() || !st.isFile())
        throw new Error("refusing symlinked storage file");
      ownerOnly(st, false);
    } catch (error) {
      if ((error as NodeJS.ErrnoException).code !== "ENOENT") throw error;
    }
    const temporary = this.path(randomUUID());
    const handle = await open(
      temporary,
      constants.O_WRONLY |
        constants.O_CREAT |
        constants.O_EXCL |
        constants.O_NOFOLLOW,
      0o600,
    );
    try {
      await handle.writeFile(JSON.stringify(value));
      await handle.sync();
    } finally {
      await handle.close();
    }
    // Atomic rename replaces the entry itself, never follows a swapped symlink.
    await rename(temporary, destination);
  }
  async readLocal(name: string): Promise<unknown> {
    await this.ready();
    const handle = await open(
      this.path(name),
      constants.O_RDONLY | constants.O_NOFOLLOW | constants.O_NONBLOCK,
    );
    try {
      const stat = await handle.stat();
      if (!stat.isFile() || stat.size > 1024 * 1024)
        throw new Error("invalid storage file");
      ownerOnly(stat, false);
      try {
        return JSON.parse(await handle.readFile("utf8"));
      } catch {
        throw new Error("invalid stored JSON");
      }
    } finally {
      await handle.close();
    }
  }
  async save(credentials: IntegrationCredentials): Promise<string> {
    this.validate(credentials);
    const credentialHandle = randomUUID();
    await this.writeLocal(credentialHandle, credentials);
    return credentialHandle;
  }
  /** Names of stored files starting with `prefix` (without `.json`), sorted. */
  async listLocal(prefix: string): Promise<string[]> {
    if (!/^[a-zA-Z0-9-]*$/.test(prefix)) throw new Error("invalid storage prefix");
    await this.ready();
    return (await readdir(this.directory))
      .filter((file) => file.startsWith(prefix) && file.endsWith(".json"))
      .map((file) => file.slice(0, -".json".length))
      .filter((name) => /^[a-zA-Z0-9-]+$/.test(name))
      .sort();
  }
  /** Delete one stored file; a missing file is already gone. */
  async removeLocal(name: string): Promise<void> {
    await this.ready();
    try {
      await unlink(this.path(name));
    } catch (error) {
      if ((error as NodeJS.ErrnoException).code !== "ENOENT") throw error;
    }
  }
  /** Replace stored credentials under the same handle (atomic rename). */
  async update(
    credentialHandle: string,
    credentials: IntegrationCredentials,
  ): Promise<void> {
    if (!validHandle.test(credentialHandle))
      throw new Error("invalid credential handle");
    this.validate(credentials);
    await this.writeLocal(credentialHandle, credentials);
  }
  async load(credentialHandle: string): Promise<IntegrationCredentials> {
    if (!validHandle.test(credentialHandle))
      throw new Error("invalid credential handle");
    const value = await this.readLocal(credentialHandle);
    this.validate(value);
    return value;
  }
  private validate(value: unknown): asserts value is IntegrationCredentials {
    if (
      !record(value) ||
      !["accessToken", "grantToken"].every(
        (key) =>
          typeof value[key] === "string" &&
          value[key].length > 0 &&
          !/[\r\n]/.test(value[key]),
      )
    )
      throw new Error("invalid credentials");
  }
}
