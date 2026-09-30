import { constants } from "node:fs";
import { lstat, open, realpath, stat } from "node:fs/promises";
import { dirname, isAbsolute, join, posix, sep } from "node:path";
import { ArtifactPathError, type GrantedOutputRoot } from "./contracts.ts";

export const MAX_SNAPSHOT_BYTES = 10 * 1024 * 1024;

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

/**
 * Read one regular file under the granted root as a stable byte snapshot.
 *
 * Node has no openat(), so containment is checked in layers instead of by
 * descriptor-relative traversal: the relative path is validated lexically,
 * every component is resolved with symlinks refused (the final open uses
 * O_NOFOLLOW and each ancestor is lstat'ed), the root's device/inode must
 * still match the grant, the opened file must be a single-link regular file
 * whose identity matches the path, and its size and mtime must be unchanged
 * after the read.
 */
export async function readSnapshot(
  root: GrantedOutputRoot,
  relativePath: string,
  maxBytes: number = MAX_SNAPSHOT_BYTES,
): Promise<Uint8Array> {
  if (sep !== "/") throw unsafe("unsupported platform");
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
  const normalized = posix.normalize(segments.join("/"));
  if (normalized.startsWith("../") || normalized === "..") throw unsafe("path may not traverse");

  // The granted root must still be the same directory it was when bound.
  const rootNow = await stat(root.path).catch(() => null);
  if (!rootNow || !rootNow.isDirectory() || rootNow.dev !== root.device || rootNow.ino !== root.inode)
    throw unsafe("output root changed");

  // Every ancestor must be a real directory (not a symlink) inside the root.
  let current = root.path;
  for (const segment of segments.slice(0, -1)) {
    current = join(current, segment);
    const info = await lstat(current).catch(() => null);
    if (!info || info.isSymbolicLink() || !info.isDirectory())
      throw unsafe("ancestor is not a directory");
  }
  const fullPath = join(current, segments[segments.length - 1]!);
  const before = await lstat(fullPath).catch(() => null);
  if (!before || before.isSymbolicLink() || !before.isFile())
    throw unsafe("not a regular file");
  if (before.nlink !== 1) throw unsafe("hard-linked file");
  if (before.size > maxBytes)
    throw new ArtifactPathError("too_large", `file exceeds ${maxBytes} bytes`);

  const handle = await open(
    fullPath,
    constants.O_RDONLY | constants.O_NOFOLLOW | constants.O_NONBLOCK,
  ).catch(() => {
    throw unsafe("cannot open file");
  });
  try {
    const opened = await handle.stat();
    if (
      !opened.isFile() ||
      opened.nlink !== 1 ||
      opened.dev !== before.dev ||
      opened.ino !== before.ino ||
      opened.size > maxBytes
    )
      throw unsafe("file changed while opening");
    // The directory containing the opened file must resolve under the root.
    const parentReal = await realpath(dirname(fullPath));
    if (parentReal !== root.path && !parentReal.startsWith(root.path + "/"))
      throw unsafe("file resolved outside the root");
    const bytes = await handle.readFile();
    const after = await handle.stat();
    if (
      after.size !== opened.size ||
      after.mtimeMs !== opened.mtimeMs ||
      after.ino !== opened.ino ||
      bytes.length !== opened.size
    )
      throw unsafe("file changed while reading");
    return new Uint8Array(bytes);
  } finally {
    await handle.close();
  }
}
