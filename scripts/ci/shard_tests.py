#!/usr/bin/env python3
"""Print the test files one shard of a pytest directory should run.

Files are assigned greedily, largest first, to the currently lightest shard
(file size stands in for runtime). Every ``test_*.py`` file lands in exactly
one shard, and the split is stable for a given tree, so a matrix of
``--shard 1..N --of N`` runs the whole directory once.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from pathlib import Path


def shard_files(root: Path, shard: int, total: int) -> list[Path]:
    """Return the sorted files of 1-based ``shard`` out of ``total`` shards."""
    if total < 1:
        raise ValueError("--of must be at least 1")
    if not 1 <= shard <= total:
        raise ValueError(f"--shard must be between 1 and {total}")

    files = sorted(root.rglob("test_*.py"))
    # Largest first; ties break on path so the assignment is deterministic.
    by_weight = sorted(files, key=lambda path: (-path.stat().st_size, path.as_posix()))
    loads = [0] * total
    buckets: list[list[Path]] = [[] for _ in range(total)]
    for path in by_weight:
        lightest = min(range(total), key=lambda index: (loads[index], index))
        buckets[lightest].append(path)
        loads[lightest] += max(path.stat().st_size, 1)
    return sorted(buckets[shard - 1])


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("root", type=Path, help="directory to search for test_*.py")
    parser.add_argument("--shard", type=int, required=True, help="1-based shard index")
    parser.add_argument("--of", type=int, required=True, dest="total")
    args = parser.parse_args(argv)

    if not args.root.is_dir():
        print(f"error: {args.root} is not a directory", file=sys.stderr)
        return 2
    try:
        files = shard_files(args.root, args.shard, args.total)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    for path in files:
        print(path.as_posix())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
