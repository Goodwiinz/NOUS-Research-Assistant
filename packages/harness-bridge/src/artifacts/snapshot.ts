import { constants } from "node:fs";
import { lstat, open, realpath, stat } from "node:fs/promises";
import { isAbsolute, join, sep } from "node:path";
import { ArtifactPathError, type GrantedOutputRoot } from "./contracts.ts";

export const MAX_SNAPSHOT_BYTES = 10 * 1024 * 1024;
// macOS 11+: refuse symlinks in ANY path component, not just the last one.
// Node's fs.constants does not expose it.
const O_NOFOLLOW_ANY = 0x20000000;

/** Test seam: runs after the ancestor checks, before the final lstat and open. */
export type SnapshotHooks = {
  afterAncestors?: () => Promise<void>;
  afterOpen?: () => Promise<void>;
};

/** Pin the root's identity at binding time; a swapped directory is refused later. */
export async function grantedRoot(path: string): Promise<GrantedOutputRoot> {
  const real = await realpath(path);
  const info = await stat(real);
  if (!info.isDirectory())
    throw new ArtifactPathError("unsafe_path", "output root is not a directory");
  return { path: real, device: info.dev, inode: info.ino };
}

function unsafe(reason: string): ArtifactPathError {
  return new ArtifactPathError("unsafe_path", `unsafe artifact path: ${reason}`);
}
const errno = (error: unknown): string =>
  (error as NodeJS.ErrnoException)?.code ?? "unknown error";
/** ENOENT is a missing file the model can fix; every other errno is named. */
function refuse(error: unknown, what: string): ArtifactPathError {
  if (errno(error) === "ENOENT")
    return new ArtifactPathError("not_found", `${what} does not exist`);
  return unsafe(`${what} unreadable (${errno(error)})`);
}

/**
 * Read one regular file under the granted root as a stable byte snapshot.
 *
 * Containment is enforced on the descriptor, not the path string, because a
 * shell-capable model can swap an ancestor directory for a symlink between any
 * path-based check and the open. On macOS the open itself refuses symlinks in
 * every component (O_NOFOLLOW_ANY); on Linux the opened descriptor's real path
 * (/proc/self/fd) must lie under the root. Other platforms are refused. The
 * lexical, ancestor and identity checks in front of it are cheap early exits,
 * not the security boundary.
 */
export async function readSnapshot(
  root: GrantedOutputRoot,
  relativePath: string,
  maxBytes: number = MAX_SNAPSHOT_BYTES,
  hooks: SnapshotHooks = {},
): Promise<Uint8Array> {
  if (sep !== "/" || !["darwin", "linux"].includes(process.platform))
    throw unsafe("unsupported platform");
  if (
    typeof relativePath !== "string" ||
    relativePath.length === 0 ||
    relativePath.includes("\u0000") ||
    isAbsolute(relativePath)
  )
    throw unsafe("path must be relative");
  const segments = relativePath.split("/").filter((s) => s.length > 0);
  if (segments.length === 0 || segments.some((s) => s === "." || s === ".."))
    throw unsafe("path may not traverse");

  // The granted root must still be the same directory it was when bound.
  const rootNow = await stat(root.path).catch((error: unknown) => {
    throw refuse(error, "output root");
  });
  if (
    !rootNow.isDirectory() ||
    rootNow.dev !== root.device ||
    rootNow.ino !== root.inode
  )
    throw unsafe("output root changed");

  // Early exits only; the open below is what actually holds.
  let current = root.path;
  for (const segment of segments.slice(0, -1)) {
    current = join(current, segment);
    const info = await lstat(current).catch((error: unknown) => {
      throw refuse(error, `directory ${segment}`);
    });
    if (info.isSymbolicLink() || !info.isDirectory())
      throw unsafe("ancestor is not a directory");
  }
  const fullPath = join(current, segments[segments.length - 1]!);
  await hooks.afterAncestors?.();
  const before = await lstat(fullPath).catch((error: unknown) => {
    throw refuse(error, "file");
  });
  if (before.isSymbolicLink() || !before.isFile())
    throw unsafe("not a regular file");
  if (before.nlink !== 1) throw unsafe("hard-linked file");
  if (before.size > maxBytes)
    throw new ArtifactPathError("too_large", `file exceeds ${maxBytes} bytes`);

  // macOS rejects O_NOFOLLOW together with O_NOFOLLOW_ANY (EINVAL); the latter
  // already refuses a symlink in the final component too.
  const flags =
    constants.O_RDONLY |
    constants.O_NONBLOCK |
    (process.platform === "darwin" ? O_NOFOLLOW_ANY : constants.O_NOFOLLOW);
  const handle = await open(fullPath, flags).catch((error: unknown) => {
    // Only ELOOP means a symlink was refused; name any other errno honestly.
    if (errno(error) === "ELOOP")
      throw unsafe("cannot open file without following a symlink");
    throw refuse(error, "file");
  });
  try {
    const opened = await handle.stat();
    if (
      !opened.isFile() ||
      opened.nlink !== 1 ||
      opened.dev !== before.dev ||
      opened.ino !== before.ino ||
      opened.size !== before.size ||
      opened.size > maxBytes
    )
      throw unsafe("file changed while opening");
    if (process.platform === "linux") {
      // Where the descriptor really points, independent of the path walked.
      const target = await realpath(`/proc/self/fd/${handle.fd}`).catch(
        (error: unknown) => {
          throw unsafe(`cannot verify the descriptor path (/proc: ${errno(error)})`);
        },
      );
      if (!target.startsWith(root.path + "/"))
        throw unsafe("descriptor resolved outside the root");
    }
    await hooks.afterOpen?.();
    // Bounded read: exactly the size we validated, never to EOF.
    const buffer = Buffer.alloc(opened.size + 1);
    const { bytesRead } = await handle.read(buffer, 0, buffer.length, 0);
    if (bytesRead !== opened.size) throw unsafe("file changed while reading");
    const after = await handle.stat();
    if (
      after.size !== opened.size ||
      after.mtimeMs !== opened.mtimeMs ||
      after.ino !== opened.ino
    )
      throw unsafe("file changed while reading");
    return new Uint8Array(buffer.buffer, 0, opened.size);
  } finally {
    await handle.close();
  }
}
