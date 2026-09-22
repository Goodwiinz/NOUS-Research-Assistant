"""Contract tests for the repository's JavaScript toolchain authority."""

import json
import re
from collections.abc import Callable, Iterator
from functools import lru_cache
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[4]
CANONICAL_NODE = "24"
CANONICAL_PNPM = "pnpm@10.18.2"
CANONICAL_NEXT = "16.2.11"
CANONICAL_REACT = "18.3.1"
REFERENCE_SCAN_EXCLUDED_DIRS = {
    ".git",
    ".next",
    ".venv",
    ".worktrees",
    "archive",
    "docs",
    "history",
    "node_modules",
    "specs",
}


def _json(path: str) -> dict[str, Any]:
    decoded = json.loads((REPO_ROOT / path).read_text(encoding="utf-8"))
    assert isinstance(decoded, dict), f"{path} must contain a JSON object"
    return decoded


def _workspace_overrides() -> dict[str, str]:
    workspace = (REPO_ROOT / "pnpm-workspace.yaml").read_text(encoding="utf-8")
    block = re.search(
        r"^overrides:\s*\n(?P<body>(?:^[ \t]+.*(?:\n|$))*)",
        workspace,
        re.MULTILINE,
    )
    assert block, "pnpm-workspace.yaml is missing its overrides mapping"

    overrides: dict[str, str] = {}
    for line in block.group("body").splitlines():
        entry = re.match(
            r"^\s{2}(?P<name>[\w>-]+):\s*['\"]?(?P<value>[^'\"\s#]+)", line
        )
        if entry:
            overrides[entry.group("name")] = entry.group("value")
    return overrides


def _iter_repository_files(root: Path) -> Iterator[Path]:
    pending = [root]
    while pending:
        directory = pending.pop()
        for path in directory.iterdir():
            if path.is_dir():
                is_included = path.name not in REFERENCE_SCAN_EXCLUDED_DIRS
                if is_included and not path.is_symlink():
                    pending.append(path)
            elif path.is_file():
                yield path


@lru_cache(maxsize=32)
def _build_reference_surface_paths(root: Path) -> tuple[Path, ...]:
    root = root.resolve()
    candidates: list[Path] = []
    for path in _iter_repository_files(root):
        relative = path.relative_to(root)
        is_workflow = (
            len(relative.parts) >= 3
            and relative.parts[:2] == (".github", "workflows")
            and path.suffix in {".yml", ".yaml"}
        )
        is_shell = path.suffix == ".sh"
        is_bake = path.match("docker-bake*.hcl")
        is_compose = path.suffix in {".yml", ".yaml"} and "compose" in path.name.lower()
        if is_workflow or is_shell or is_bake or is_compose:
            candidates.append(path)
    return tuple(sorted(candidates))


def _executable_reference_text(contents: str) -> str:
    executable_lines: list[str] = []
    for line in contents.splitlines():
        executable = line.split("#", 1)[0].rstrip()
        if executable:
            executable_lines.append(executable)
    return "\n".join(executable_lines)


@lru_cache(maxsize=32)
def _cached_frontend_dockerfile_references(
    root: Path,
) -> tuple[tuple[Path, tuple[str, ...]], ...]:
    root = root.resolve()
    references: list[tuple[Path, tuple[str, ...]]] = []
    for surface in _build_reference_surface_paths(root):
        contents = _executable_reference_text(surface.read_text(encoding="utf-8"))
        matches = sorted(set(re.findall(r"\bfrontend/Dockerfile(?:[.\w-]*)", contents)))
        if matches:
            references.append((surface, tuple(matches)))
    return tuple(references)


def _frontend_dockerfile_references(root: Path) -> dict[Path, set[str]]:
    return {
        surface: set(references)
        for surface, references in _cached_frontend_dockerfile_references(
            root.resolve()
        )
    }


def _active_frontend_dockerfiles() -> tuple[Path, ...]:
    references = _frontend_dockerfile_references(REPO_ROOT)
    relative_paths = set().union(*references.values()) if references else set()
    assert (
        relative_paths
    ), "no executable build surface references a frontend Dockerfile"
    missing = sorted(
        path for path in relative_paths if not (REPO_ROOT / path).is_file()
    )
    assert not missing, f"referenced frontend Dockerfiles do not exist: {missing}"
    return tuple(REPO_ROOT / path for path in sorted(relative_paths))


def _workflow_node_violations(workflow: str) -> list[str]:
    declarations: list[tuple[int, str, int]] = []
    top_level_values: list[str] = []
    in_top_level_env = False
    for line_number, line in enumerate(workflow.splitlines(), start=1):
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        indent = len(line) - len(line.lstrip(" "))
        if indent == 0:
            in_top_level_env = line.strip() == "env:"
        declaration = re.match(r"^\s*NODE_VERSION:\s*(?P<value>.*?)\s*$", line)
        if declaration:
            value = declaration.group("value").strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
                value = value[1:-1]
            declarations.append((indent, value, line_number))
            if in_top_level_env and indent == 2:
                top_level_values.append(value)

    violations: list[str] = []
    if top_level_values != [CANONICAL_NODE]:
        violations.append(
            "top-level env.NODE_VERSION must be exactly "
            f"{CANONICAL_NODE!r}, found {top_level_values!r}"
        )
    conflicts = [
        f"line {line_number}: {value}"
        for _, value, line_number in declarations
        if value != CANONICAL_NODE
    ]
    if conflicts:
        violations.append(f"conflicting NODE_VERSION declarations: {conflicts}")
    return violations


def _docker_instructions(contents: str) -> list[tuple[str, str]]:
    instructions: list[tuple[str, str]] = []
    logical_line = ""
    for raw_line in contents.splitlines():
        stripped = raw_line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        continued = stripped.endswith("\\")
        fragment = stripped[:-1].rstrip() if continued else stripped
        logical_line = f"{logical_line} {fragment}".strip()
        if continued:
            continue
        match = re.match(r"^(?P<name>[A-Za-z]+)(?:\s+(?P<body>.*))?$", logical_line)
        if match:
            instructions.append(
                (match.group("name").upper(), match.group("body") or "")
            )
        logical_line = ""
    if logical_line:
        match = re.match(r"^(?P<name>[A-Za-z]+)(?:\s+(?P<body>.*))?$", logical_line)
        if match:
            instructions.append(
                (match.group("name").upper(), match.group("body") or "")
            )
    return instructions


def _shell_commands(body: str) -> list[tuple[str, list[str]]]:
    commands: list[tuple[str, list[str]]] = []
    for segment in re.split(r"\s*(?:&&|\|\||;|\|)\s*", body):
        tokens = segment.strip().split()
        while tokens and tokens[0].startswith("--"):
            tokens.pop(0)
        while tokens and tokens[0] in {"then", "else", "do", "!"}:
            tokens.pop(0)
        if not tokens or tokens[0] in {"if", "elif", "fi", "for", "while"}:
            continue
        while tokens and re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", tokens[0]):
            tokens.pop(0)
        if tokens and tokens[0] in {"command", "env"}:
            tokens.pop(0)
            while tokens and (
                tokens[0].startswith("-")
                or re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", tokens[0])
            ):
                tokens.pop(0)
        if tokens:
            command = tokens[0].strip("'\"")
            commands.append((command.rsplit("/", 1)[-1], tokens[1:]))
    return commands


def _expanded_shell_commands(
    body: str, commands: list[tuple[str, list[str]]]
) -> list[tuple[str, list[str]]]:
    expanded = list(commands)
    payloads: list[str] = []
    for command, args in commands:
        if command in {"sh", "bash"} and "-c" in args:
            command_index = args.index("-c")
            payload = " ".join(args[command_index + 1 :]).strip()
            if len(payload) >= 2 and payload[0] == payload[-1] and payload[0] in "\"'":
                payload = payload[1:-1]
            payloads.append(payload)
    wrapper_pattern = re.compile(
        r"\b(?:ba)?sh\s+-c\s+(?P<quote>[\"'])(?P<payload>.*?)(?P=quote)"
    )
    payloads.extend(match.group("payload") for match in wrapper_pattern.finditer(body))
    for payload in payloads:
        expanded.extend(_shell_commands(payload))
    return expanded


def _json_or_shell_command(body: str) -> tuple[str, list[str]] | None:
    if body.lstrip().startswith("["):
        try:
            decoded = json.loads(body)
        except json.JSONDecodeError:
            return None
        if (
            isinstance(decoded, list)
            and decoded
            and all(isinstance(item, str) for item in decoded)
        ):
            return decoded[0].rsplit("/", 1)[-1], decoded[1:]
        return None
    commands = _shell_commands(body)
    return commands[0] if commands else None


def _docker_node_violations(contents: str) -> list[str]:
    instructions = _docker_instructions(contents)
    global_args: dict[str, str | None] = {}
    before_first_from = True
    violations: list[str] = []
    for name, body in instructions:
        if name == "ARG" and before_first_from:
            arg_name, separator, value = body.partition("=")
            global_args[arg_name.strip()] = value.strip() if separator else None
        if name != "FROM":
            continue
        before_first_from = False
        image_match = re.match(r"(?:--platform=\S+\s+)?node:(?P<tag>\S+)", body)
        if not image_match:
            continue
        tag = image_match.group("tag")
        variable_tag = re.fullmatch(r"\$\{NODE_VERSION\}(?:-.+)?", tag)
        if variable_tag:
            actual = global_args.get("NODE_VERSION")
            if actual != CANONICAL_NODE:
                violations.append(
                    f"FROM uses ${{NODE_VERSION}} but global ARG defaults to {actual!r}"
                )
        elif "${NODE_VERSION}" in tag:
            violations.append(f"malformed NODE_VERSION image tag {tag!r}")
        elif tag != CANONICAL_NODE and not tag.startswith(f"{CANONICAL_NODE}-"):
            violations.append(f"noncanonical Node image tag {tag!r}")
    has_node_image = any(
        name == "FROM" and re.search(r"(?:^|\s)node:", body)
        for name, body in instructions
    )
    if not has_node_image:
        violations.append("no Node base image")
    return violations


def _docker_tool_violations(contents: str) -> list[str]:
    corepack_enabled = False
    pnpm_used = False
    npm_fallbacks: list[str] = []
    for instruction, body in _docker_instructions(contents):
        commands: list[tuple[str, list[str]]] = []
        if instruction == "RUN":
            if body.lstrip().startswith("["):
                parsed = _json_or_shell_command(body)
                commands = [parsed] if parsed else []
            else:
                commands = _shell_commands(body)
        if instruction in {"CMD", "ENTRYPOINT"}:
            parsed = _json_or_shell_command(body)
            commands = [parsed] if parsed else []
        commands = _expanded_shell_commands(body, commands)
        for command, args in commands:
            non_option_args = [
                arg.strip("'\",") for arg in args if not arg.startswith("-")
            ]
            if command == "corepack" and non_option_args[:1] == ["enable"]:
                corepack_enabled = True
            if command == "pnpm":
                pnpm_used = True
            if command == "npm":
                if instruction in {"CMD", "ENTRYPOINT"} or any(
                    arg in {"ci", "install", "run"} for arg in non_option_args
                ):
                    npm_fallbacks.append(f"{instruction} npm {' '.join(args)}".strip())
    violations: list[str] = []
    if not corepack_enabled:
        violations.append("missing executable `RUN corepack enable`")
    if not pnpm_used:
        violations.append("missing executable pnpm command")
    if npm_fallbacks:
        violations.append(f"npm fallbacks: {npm_fallbacks}")
    return violations


def _node_assignment_value(raw_value: str) -> str:
    value = raw_value.strip().rstrip("\\").strip()
    if value.startswith("${{") and "}}" in value:
        return value[: value.index("}}") + 2]
    if value[:1] in {'"', "'"}:
        quote = value[0]
        closing = value.find(quote, 1)
        if closing != -1:
            return value[1:closing]
    return value.split()[0] if value else "<environment>"


def _node_override_values(contents: str) -> set[str]:
    values: set[str] = set()
    for line in contents.splitlines():
        build_arg = re.search(
            r"--build-arg(?:=|\s+)NODE_VERSION(?:=(?P<value>.*?))?(?:\s|$)",
            line,
        )
        if build_arg:
            values.add(_node_assignment_value(build_arg.group("value") or ""))
        declaration = re.search(r"\bNODE_VERSION\s*[:=]\s*(?P<value>.+)$", line)
        if declaration:
            values.add(_node_assignment_value(declaration.group("value")))
    return values


def _bake_node_default(contents: str) -> str | None:
    variable = re.search(
        r'variable\s+"NODE_VERSION"\s*\{(?P<body>.*?)\}', contents, re.DOTALL
    )
    if not variable:
        return None
    default = re.search(r'default\s*=\s*"(?P<value>[^"]+)"', variable.group("body"))
    return default.group("value") if default else None


def _caller_node_override_violations(root: Path) -> list[str]:
    violations: list[str] = []
    for surface, references in _frontend_dockerfile_references(root).items():
        contents = _executable_reference_text(surface.read_text(encoding="utf-8"))
        for value in _node_override_values(contents):
            if value != CANONICAL_NODE:
                approved_bake_reference = (
                    surface.match("docker-bake*.hcl")
                    and value == "${NODE_VERSION}"
                    and _bake_node_default(contents) == CANONICAL_NODE
                )
                if approved_bake_reference:
                    continue
                paths = ", ".join(sorted(references))
                violations.append(
                    f"{surface.relative_to(root)} overrides {paths} with Node {value}"
                )
    return violations


def _npm_run_scripts(package_path: str) -> dict[str, str]:
    scripts = _json(package_path).get("scripts", {})
    return {
        name: command
        for name, command in scripts.items()
        if re.search(r"\bnpm\s+run\b", command)
    }


def _assert_contract_rejects(check: Callable[[], None], message: str) -> None:
    try:
        check()
    except AssertionError:
        return
    raise AssertionError(message)


def _activate_docker_fixture(
    tmp_path: Path, monkeypatch: Any, dockerfile: str, caller: str
) -> None:
    dockerfile_path = tmp_path / "frontend/Dockerfile.fixture"
    dockerfile_path.parent.mkdir(parents=True)
    dockerfile_path.write_text(dockerfile, encoding="utf-8")
    caller_path = tmp_path / "caller.sh"
    caller_path.write_text(caller, encoding="utf-8")
    monkeypatch.setitem(globals(), "REPO_ROOT", tmp_path)


def test_workflow_node_authority_does_not_accept_nested_canonical_value(
    tmp_path: Path, monkeypatch: Any
) -> None:
    workflow_path = tmp_path / ".github/workflows/test-pipeline.yml"
    workflow_path.parent.mkdir(parents=True)
    workflow_path.write_text(
        'env:\n  NODE_VERSION: "20"\njobs:\n  test:\n    env:\n'
        '      NODE_VERSION: "24"\n',
        encoding="utf-8",
    )
    monkeypatch.setitem(globals(), "REPO_ROOT", tmp_path)

    _assert_contract_rejects(
        test_ci_workflow_declares_canonical_node,
        "top-level Node 20 plus nested Node 24 must not satisfy the workflow contract",
    )


def test_variable_node_image_requires_canonical_arg_default(
    tmp_path: Path, monkeypatch: Any
) -> None:
    _activate_docker_fixture(
        tmp_path,
        monkeypatch,
        "ARG NODE_VERSION=20\nFROM node:${NODE_VERSION}-alpine\n",
        "docker build -f frontend/Dockerfile.fixture .\n",
    )

    _assert_contract_rejects(
        test_active_frontend_dockerfiles_use_canonical_node,
        "a variable Node image with ARG NODE_VERSION=20 must be rejected",
    )


def test_noncanonical_caller_build_arg_is_rejected(
    tmp_path: Path, monkeypatch: Any
) -> None:
    _activate_docker_fixture(
        tmp_path,
        monkeypatch,
        "ARG NODE_VERSION=24\nFROM node:${NODE_VERSION}-alpine\n",
        "docker build --build-arg NODE_VERSION=20 "
        "-f frontend/Dockerfile.fixture .\n",
    )

    _assert_contract_rejects(
        test_active_frontend_dockerfiles_use_canonical_node,
        "an active caller overriding NODE_VERSION=20 must be rejected",
    )


def test_comments_and_echo_do_not_satisfy_docker_tool_commands(
    tmp_path: Path, monkeypatch: Any
) -> None:
    _activate_docker_fixture(
        tmp_path,
        monkeypatch,
        "FROM node:24-alpine\n# RUN corepack enable\nRUN echo pnpm\n",
        "docker build -f frontend/Dockerfile.fixture .\n",
    )

    _assert_contract_rejects(
        test_active_frontend_dockerfiles_enable_corepack_and_use_pnpm,
        "Docker comments and `RUN echo pnpm` must not satisfy executable commands",
    )


def test_npm_flags_cannot_evade_docker_fallback_check(
    tmp_path: Path, monkeypatch: Any
) -> None:
    _activate_docker_fixture(
        tmp_path,
        monkeypatch,
        "FROM node:24-alpine\nRUN corepack enable && pnpm install\n"
        "RUN npm --silent install\n",
        "docker build -f frontend/Dockerfile.fixture .\n",
    )

    _assert_contract_rejects(
        test_active_frontend_dockerfiles_have_no_npm_install_or_run_fallbacks,
        "`npm --silent install` must be rejected",
    )


def test_json_cmd_npm_cannot_evade_docker_fallback_check(
    tmp_path: Path, monkeypatch: Any
) -> None:
    _activate_docker_fixture(
        tmp_path,
        monkeypatch,
        "FROM node:24-alpine\nRUN corepack enable && pnpm install\n"
        'CMD ["npm", "run", "dev"]\n',
        "docker build -f frontend/Dockerfile.fixture .\n",
    )

    _assert_contract_rejects(
        test_active_frontend_dockerfiles_have_no_npm_install_or_run_fallbacks,
        "JSON-form CMD using npm must be rejected",
    )


def test_json_run_npm_is_rejected() -> None:
    dockerfile = """\
FROM node:24-alpine
RUN corepack enable && pnpm install
RUN ["npm", "--silent", "install"]
"""

    violations = _docker_tool_violations(dockerfile)

    assert any("npm fallbacks" in item for item in violations)


def test_workflow_node_authority_rejects_nested_conflict() -> None:
    workflow = (
        'env:\n  NODE_VERSION: "24"\njobs:\n  test:\n    env:\n'
        '      NODE_VERSION: "20"\n'
    )

    violations = _workflow_node_violations(workflow)

    assert any("conflicting NODE_VERSION" in item for item in violations)


def test_workflow_node_authority_rejects_nested_dynamic_expression() -> None:
    workflow = (
        'env:\n  NODE_VERSION: "24"\njobs:\n  test:\n    env:\n'
        "      NODE_VERSION: ${{ matrix.node }}\n"
    )

    violations = _workflow_node_violations(workflow)

    assert any("conflicting NODE_VERSION" in item for item in violations)


def test_variable_caller_build_arg_is_rejected(
    tmp_path: Path, monkeypatch: Any
) -> None:
    _activate_docker_fixture(
        tmp_path,
        monkeypatch,
        "ARG NODE_VERSION=24\nFROM node:${NODE_VERSION}-alpine\n",
        "docker build --build-arg NODE_VERSION=$NODE_VERSION "
        "-f frontend/Dockerfile.fixture .\n",
    )

    _assert_contract_rejects(
        test_active_frontend_dockerfiles_use_canonical_node,
        "a variable caller NODE_VERSION override must be rejected",
    )


def test_shell_wrapped_npm_fallback_is_rejected(
    tmp_path: Path, monkeypatch: Any
) -> None:
    _activate_docker_fixture(
        tmp_path,
        monkeypatch,
        "FROM node:24-alpine\nRUN corepack enable && pnpm install\n"
        'RUN sh -c "npm install"\n',
        "docker build -f frontend/Dockerfile.fixture .\n",
    )

    _assert_contract_rejects(
        test_active_frontend_dockerfiles_have_no_npm_install_or_run_fallbacks,
        "npm executed through `sh -c` must be rejected",
    )


def test_node_variable_must_fill_the_complete_version_prefix(
    tmp_path: Path, monkeypatch: Any
) -> None:
    _activate_docker_fixture(
        tmp_path,
        monkeypatch,
        "ARG NODE_VERSION=24\nFROM node:${NODE_VERSION}0-alpine\n",
        "docker build -f frontend/Dockerfile.fixture .\n",
    )

    _assert_contract_rejects(
        test_active_frontend_dockerfiles_use_canonical_node,
        "`${NODE_VERSION}0` must not satisfy the canonical image tag contract",
    )


def test_docker_node_parser_accepts_canonical_hardcoded_forms() -> None:
    assert not _docker_node_violations("FROM node:24\n")
    assert not _docker_node_violations(
        "FROM --platform=linux/amd64 node:24-alpine AS builder\n"
    )


def test_multiline_shell_branch_npm_fallback_is_rejected() -> None:
    dockerfile = """\
FROM node:24-alpine
RUN corepack enable && pnpm install
RUN if [ -f package-lock.json ]; then \\
      npm ci; \\
    else \\
      pnpm install; \\
    fi
"""

    violations = _docker_tool_violations(dockerfile)

    assert any("npm fallbacks" in item for item in violations)


def test_nonexecuted_npm_text_is_not_reported_as_a_fallback() -> None:
    dockerfile = """\
FROM node:24-alpine
# RUN npm install
RUN corepack enable && pnpm install
RUN echo npm install
"""

    violations = _docker_tool_violations(dockerfile)

    assert not [item for item in violations if item.startswith("npm fallbacks")]


def test_discovery_finds_new_executable_surfaces_and_ignores_docs(
    tmp_path: Path,
) -> None:
    fixtures = {
        ".github/workflows/build.yaml": "file: frontend/Dockerfile.workflow\n",
        "automation/build.sh": "docker build -f frontend/Dockerfile.shell .\n",
        "config/compose.extra.yml": "dockerfile: frontend/Dockerfile.compose\n",
        "docker-bake.extra.hcl": 'dockerfile = "frontend/Dockerfile.bake"\n',
        "docs/old-build.sh": "docker build -f frontend/Dockerfile.history .\n",
    }
    for relative_path, contents in fixtures.items():
        path = tmp_path / relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(contents, encoding="utf-8")

    references = _frontend_dockerfile_references(tmp_path)
    discovered = {path.relative_to(tmp_path).as_posix() for path in references}

    assert discovered == {
        ".github/workflows/build.yaml",
        "automation/build.sh",
        "config/compose.extra.yml",
        "docker-bake.extra.hcl",
    }


def test_reference_discovery_cache_is_scoped_per_root(tmp_path: Path) -> None:
    first_root = tmp_path / "first"
    second_root = tmp_path / "second"
    for root, dockerfile in (
        (first_root, "frontend/Dockerfile.first"),
        (second_root, "frontend/Dockerfile.second"),
    ):
        root.mkdir()
        (root / "build.sh").write_text(
            f"docker build -f {dockerfile} .\n", encoding="utf-8"
        )
        excluded = root / ".venv"
        excluded.mkdir()
        (excluded / "hidden.sh").write_text(
            "docker build -f frontend/Dockerfile.hidden .\n", encoding="utf-8"
        )

    _build_reference_surface_paths.cache_clear()
    first = _build_reference_surface_paths(first_root)
    first_cached = _build_reference_surface_paths(first_root)
    second = _build_reference_surface_paths(second_root)

    assert first is first_cached
    assert {path.name for path in first} == {"build.sh"}
    assert {path.parent for path in first} == {first_root.resolve()}
    assert {path.parent for path in second} == {second_root.resolve()}


def test_active_dockerfile_discovery_covers_executable_build_surfaces() -> None:
    references = _frontend_dockerfile_references(REPO_ROOT)
    discovered = {path.relative_to(REPO_ROOT).as_posix() for path in references}
    required = {
        ".github/workflows/test-pipeline.yml",
        "config/docker-compose/docker-compose.ci.yml",
        "deployment/deploy.sh",
        "docker-bake.hcl",
        "scripts/ci/stages.sh",
        "scripts/docker-security-scan.sh",
    }

    assert required <= discovered, (
        "active Dockerfile reference surfaces escaped discovery: "
        f"{required - discovered}"
    )


def test_nvmrc_declares_canonical_node() -> None:
    actual = (REPO_ROOT / ".nvmrc").read_text(encoding="utf-8").strip()

    assert (
        actual == CANONICAL_NODE
    ), f".nvmrc declares Node {actual!r}; expected {CANONICAL_NODE!r}"


def test_root_package_declares_canonical_pnpm() -> None:
    actual = _json("package.json").get("packageManager")

    assert (
        actual == CANONICAL_PNPM
    ), f"package.json packageManager is {actual!r}; expected {CANONICAL_PNPM!r}"


def test_frontend_package_declares_canonical_pnpm() -> None:
    actual = _json("frontend/package.json").get("packageManager")

    assert actual == CANONICAL_PNPM, (
        "frontend/package.json packageManager is "
        f"{actual!r}; expected {CANONICAL_PNPM!r}"
    )


def test_root_package_declares_canonical_node_engine() -> None:
    actual = _json("package.json").get("engines", {}).get("node")

    assert (
        actual == f"{CANONICAL_NODE}.x"
    ), f"package.json engines.node is {actual!r}; expected '{CANONICAL_NODE}.x'"


def test_frontend_package_declares_canonical_node_engine() -> None:
    actual = _json("frontend/package.json").get("engines", {}).get("node")

    assert actual == f"{CANONICAL_NODE}.x", (
        "frontend/package.json engines.node is "
        f"{actual!r}; expected '{CANONICAL_NODE}.x'"
    )


def test_ci_workflow_declares_canonical_node() -> None:
    workflow = (REPO_ROOT / ".github/workflows/test-pipeline.yml").read_text(
        encoding="utf-8"
    )

    violations = _workflow_node_violations(workflow)

    assert not violations, (
        ".github/workflows/test-pipeline.yml has Node authority drift: " f"{violations}"
    )


def test_docker_bake_defaults_to_canonical_node() -> None:
    bake = (REPO_ROOT / "docker-bake.hcl").read_text(encoding="utf-8")
    variable = re.search(
        r'variable\s+"NODE_VERSION"\s*\{(?P<body>.*?)\}', bake, re.DOTALL
    )

    assert variable, 'docker-bake.hcl is missing variable "NODE_VERSION"'
    default = re.search(r'default\s*=\s*"(?P<value>[^"]+)"', variable.group("body"))
    actual = default.group("value") if default else None
    assert actual == CANONICAL_NODE, (
        "docker-bake.hcl NODE_VERSION default is "
        f"{actual!r}; expected {CANONICAL_NODE!r}"
    )


def test_root_scripts_do_not_shell_out_to_npm_run() -> None:
    offenders = _npm_run_scripts("package.json")

    assert not offenders, f"package.json scripts still use `npm run`: {offenders}"


def test_frontend_scripts_do_not_shell_out_to_npm_run() -> None:
    offenders = _npm_run_scripts("frontend/package.json")

    assert (
        not offenders
    ), f"frontend/package.json scripts still use `npm run`: {offenders}"


def test_active_frontend_dockerfiles_use_canonical_node() -> None:
    drift: list[str] = []
    for path in _active_frontend_dockerfiles():
        contents = path.read_text(encoding="utf-8")
        violations = _docker_node_violations(contents)
        if violations:
            drift.append(f"{path.relative_to(REPO_ROOT)}: {violations}")

    drift.extend(_caller_node_override_violations(REPO_ROOT))

    assert not drift, (
        f"active frontend Dockerfiles must use Node {CANONICAL_NODE} "
        f"or ${{NODE_VERSION}}: {drift}"
    )


def test_active_frontend_dockerfiles_enable_corepack_and_use_pnpm() -> None:
    drift: list[str] = []
    for path in _active_frontend_dockerfiles():
        contents = path.read_text(encoding="utf-8")
        missing = [
            violation
            for violation in _docker_tool_violations(contents)
            if violation.startswith("missing executable")
        ]
        if missing:
            drift.append(f"{path.relative_to(REPO_ROOT)}: {missing}")

    assert (
        not drift
    ), f"active frontend Dockerfiles must use corepack plus pnpm: {drift}"


def test_active_frontend_dockerfiles_have_no_npm_install_or_run_fallbacks() -> None:
    drift: list[str] = []
    for path in _active_frontend_dockerfiles():
        contents = path.read_text(encoding="utf-8")
        fallbacks = [
            violation
            for violation in _docker_tool_violations(contents)
            if violation.startswith("npm fallbacks")
        ]
        if fallbacks:
            drift.append(f"{path.relative_to(REPO_ROOT)}: {fallbacks}")

    assert not drift, (
        "active frontend Dockerfiles still contain npm install/run fallbacks: "
        f"{drift}"
    )


def test_frontend_has_no_nested_pnpm_lockfile() -> None:
    nested_lock = REPO_ROOT / "frontend/pnpm-lock.yaml"

    assert (
        not nested_lock.exists()
    ), "frontend/pnpm-lock.yaml exists; root pnpm-lock.yaml must be the sole authority"


def test_next_override_matches_frontend_declaration() -> None:
    frontend_next = _json("frontend/package.json")["dependencies"].get("next")
    override_next = _workspace_overrides().get("next")

    assert frontend_next == CANONICAL_NEXT, (
        "frontend/package.json dependencies.next is "
        f"{frontend_next!r}; expected {CANONICAL_NEXT!r}"
    )
    assert override_next == frontend_next, (
        "pnpm-workspace.yaml overrides.next is "
        f"{override_next!r}; expected frontend declaration {frontend_next!r}"
    )


def _assert_react_override_matches_frontend_declaration(name: str) -> None:
    dependencies = _json("frontend/package.json")["dependencies"]
    overrides = _workspace_overrides()
    declaration = dependencies.get(name)
    override = overrides.get(f"multimodal-rag-frontend>{name}")
    assert declaration == CANONICAL_REACT, (
        f"frontend/package.json dependencies.{name} is {declaration!r}; "
        f"expected {CANONICAL_REACT!r}"
    )
    assert override == declaration, (
        f"pnpm-workspace.yaml overrides.{name} is {override!r}; "
        f"expected frontend declaration {declaration!r}"
    )


def test_react_override_matches_frontend_declaration() -> None:
    _assert_react_override_matches_frontend_declaration("react")


def test_react_dom_override_matches_frontend_declaration() -> None:
    _assert_react_override_matches_frontend_declaration("react-dom")
