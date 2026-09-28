#!/usr/bin/env python3
"""Verify required role prompt assets in a packaged backend tree.

This intentionally uses only the Python standard library. Run it against the
backend directory copied into an image, for example:

    python scripts/check_agent_role_assets.py --root /app
"""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Sequence

_ROLE_MARKERS = {
    "research": "# Research subgraph — driver protocol",
    "writing": "# Writing subgraph — driver protocol",
    "data": "# Data subgraph — driver protocol",
}


def check_assets(root: Path) -> list[str]:
    """Return configuration errors for missing, unreadable, or invalid assets."""
    asset_dir = root / "src/services/agent/subgraphs"
    errors: list[str] = []
    for role, heading in _ROLE_MARKERS.items():
        path = asset_dir / f"AGENTS_{role}.md"
        try:
            content = path.read_text(encoding="utf-8")
        except FileNotFoundError:
            errors.append(f"missing required role asset: {path}")
            continue
        except (OSError, UnicodeError) as exc:
            errors.append(
                f"unreadable required role asset: {path} ({type(exc).__name__})"
            )
            continue

        if not content.strip():
            errors.append(f"empty required role asset: {path}")
        elif heading not in content or "## Your tools" not in content:
            errors.append(f"invalid role heading or tools marker: {path}")
    return errors


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--root",
        type=Path,
        default=Path(__file__).resolve().parents[1],
        help="packaged backend root containing src/services/agent/subgraphs",
    )
    args = parser.parse_args(argv)

    errors = check_assets(args.root)
    if errors:
        for error in errors:
            print(error)
        return 1
    print("all required agent role assets are present and readable")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
