"""Runtime configuration for the NOUS coordination milestone."""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from .coordination import ValidationError
from .schema import SchemaError, validate_branch, validate_machine_id


def _validation(message: str) -> ValidationError:
    return ValidationError(SchemaError(message), message=message)


@dataclass(frozen=True)
class RuntimeConfig:
    coord_mode: str
    git_remote: str
    github_repository: str
    coordination_branch: str
    loop_bridge_dir: Path | None
    receipt_dir: Path
    machine_id: str


def load_runtime_config(
    environ: Mapping[str, str], *, repo_root: Path
) -> RuntimeConfig:
    mode = environ.get("NOUS_COORD_MODE", "local")
    if mode not in {"local", "remote-required"}:
        raise _validation("NOUS_COORD_MODE must be local or remote-required")
    machine = environ.get("NOUS_MACHINE_ID")
    if machine is None:
        raise _validation("NOUS_MACHINE_ID must be configured explicitly")
    try:
        machine = validate_machine_id(machine)
        branch = validate_branch(environ.get("NOUS_COORD_BRANCH", "nous-coordination"))
    except SchemaError as exc:
        raise ValidationError(exc) from None
    remote = environ.get("NOUS_COORD_GIT_REMOTE", "origin")
    if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", remote) is None:
        raise _validation("NOUS_COORD_GIT_REMOTE is invalid")
    repository = environ.get("NOUS_GITHUB_REPOSITORY", "goodwiins/NOUS-Research-Assistant")
    if re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository) is None:
        raise _validation("NOUS_GITHUB_REPOSITORY must be owner/repo")
    bridge_value = environ.get("LOOP_BRIDGE_DIR")
    bridge = Path(bridge_value).expanduser() if bridge_value else None
    receipt = Path(
        environ.get(
            "NOUS_RECEIPT_DIR", str(Path.home() / ".nous-runs" / repo_root.name)
        )
    ).expanduser()
    return RuntimeConfig(mode, remote, repository, branch, bridge, receipt, machine)


__all__ = ["RuntimeConfig", "load_runtime_config"]
