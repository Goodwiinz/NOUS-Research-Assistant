"""Behavioural tests for scripts/ci/verify_deploy_health.py (CI7).

A real local HTTP server plays scripted responses; the probe must only pass
on consecutive healthy JSON answers and fail closed on everything else.
"""

from __future__ import annotations

import importlib.util
import socket
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import ModuleType
from typing import Iterator

import pytest

pytestmark = pytest.mark.unit

_SCRIPT = (
    Path(__file__).resolve().parents[4] / "scripts" / "ci" / "verify_deploy_health.py"
)


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("verify_deploy_health", _SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["verify_deploy_health"] = module
    spec.loader.exec_module(module)
    return module


health = _load()

Response = tuple[int, bytes]
HEALTHY: Response = (200, b'{"status":"healthy","timestamp":"t"}')
UNHEALTHY: Response = (200, b'{"status":"unhealthy"}')
DOWN: Response = (503, b'{"detail":"starting"}')
HTML: Response = (200, b"<html>maintenance</html>")


class _Script:
    def __init__(self, responses: list[Response]) -> None:
        self.responses = responses
        self.calls = 0

    def next(self) -> Response:
        index = min(self.calls, len(self.responses) - 1)
        self.calls += 1
        return self.responses[index]


@pytest.fixture
def server() -> Iterator[tuple[str, _Script]]:
    script = _Script([HEALTHY])

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802 - http.server API
            code, body = script.next()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args: object) -> None:
            return

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{httpd.server_address[1]}/health", script
    finally:
        httpd.shutdown()
        httpd.server_close()


def _run(url: str, *, timeout: float, consecutive: int) -> bool:
    now = [0.0]

    def clock() -> float:
        return now[0]

    def sleep(seconds: float) -> None:
        now[0] += seconds

    return bool(
        health.wait_until_healthy(
            url,
            timeout=timeout,
            interval=1.0,
            consecutive=consecutive,
            request_timeout=2.0,
            clock=clock,
            sleep=sleep,
            log=lambda _line: None,
        )
    )


def test_passes_once_a_rollout_settles(server: tuple[str, _Script]) -> None:
    url, script = server
    script.responses = [DOWN, DOWN, HEALTHY, HEALTHY, HEALTHY]
    assert _run(url, timeout=10, consecutive=3) is True
    assert script.calls == 5


def test_a_flapping_backend_never_passes(server: tuple[str, _Script]) -> None:
    url, script = server
    script.responses = [HEALTHY, UNHEALTHY] * 10
    assert _run(url, timeout=10, consecutive=2) is False


@pytest.mark.parametrize("response", [DOWN, UNHEALTHY, HTML])
def test_non_healthy_answers_fail_closed(
    server: tuple[str, _Script], response: Response
) -> None:
    url, script = server
    script.responses = [response]
    assert _run(url, timeout=3, consecutive=1) is False


def test_unreachable_backend_fails() -> None:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    assert _run(f"http://127.0.0.1:{port}/health", timeout=2, consecutive=1) is False


def test_cli_exit_codes(server: tuple[str, _Script]) -> None:
    url, script = server
    script.responses = [HEALTHY]
    assert health.main(["--url", url, "--consecutive", "1", "--timeout", "1"]) == 0
    script.responses = [DOWN]
    assert (
        health.main(
            ["--url", url, "--consecutive", "1", "--timeout", "0", "--interval", "1"]
        )
        == 1
    )


# --- build identity (CI7): /health git_sha vs backend.image.sourceSha ---

SHA_A = "a" * 40
SHA_B = "b" * 40
CHANGED_AT = 1_000_000


def _healthy_with(sha: str) -> Response:
    return (200, ('{"status":"healthy","git_sha":"%s"}' % sha).encode())


def _check(url: str, *, age_seconds: float, grace_minutes: float = 20) -> bool:
    lines: list[str] = []
    ok = health.check_build(
        url,
        expected_sha=SHA_A,
        changed_at=CHANGED_AT,
        grace_seconds=grace_minutes * 60,
        request_timeout=2.0,
        now=lambda: CHANGED_AT + age_seconds,
        log=lines.append,
    )
    return bool(ok)


def test_build_check_passes_when_the_expected_sha_serves(
    server: tuple[str, _Script],
) -> None:
    url, script = server
    script.responses = [_healthy_with(SHA_A)]
    assert _check(url, age_seconds=3 * 3600) is True


def test_build_check_tolerates_a_mismatch_inside_the_grace_window(
    server: tuple[str, _Script],
) -> None:
    url, script = server
    script.responses = [_healthy_with(SHA_B)]
    assert _check(url, age_seconds=5 * 60) is True


def test_build_check_fails_a_mismatch_after_the_grace_window(
    server: tuple[str, _Script],
) -> None:
    url, script = server
    script.responses = [_healthy_with(SHA_B)]
    assert _check(url, age_seconds=21 * 60) is False


def test_build_check_fails_a_mismatch_when_the_values_file_is_undated(
    server: tuple[str, _Script],
) -> None:
    url, script = server
    script.responses = [_healthy_with(SHA_B)]
    assert (
        health.check_build(
            url,
            expected_sha=SHA_A,
            changed_at=None,
            grace_seconds=20 * 60,
            request_timeout=2.0,
            log=lambda _line: None,
        )
        is False
    )


def test_build_check_warns_but_passes_without_git_sha(
    server: tuple[str, _Script],
) -> None:
    url, script = server
    script.responses = [HEALTHY]
    lines: list[str] = []
    assert (
        health.check_build(
            url,
            expected_sha=SHA_A,
            changed_at=CHANGED_AT,
            grace_seconds=0,
            request_timeout=2.0,
            now=lambda: CHANGED_AT + 86_400,
            log=lines.append,
        )
        is True
    )
    assert any(line.startswith("::warning::") for line in lines)


_VALUES = """\
frontend:
  image:
    sourceSha: "{other}"
backend:
  enabled: true
  image:
    repository: "example/backend"
    tag: "{sha}"
    sourceSha: "{sha}"  # trailing comment
  env:
    - name: X
      value: "y"
"""


def test_read_source_sha_reads_the_backend_image_key(tmp_path: Path) -> None:
    values = tmp_path / "values.yaml"
    values.write_text(_VALUES.format(sha=SHA_A, other=SHA_B))
    assert health.read_source_sha(values) == SHA_A


@pytest.mark.parametrize("sha", ["", "unknown", SHA_A[:7], SHA_A.upper()])
def test_read_source_sha_rejects_non_full_shas(tmp_path: Path, sha: str) -> None:
    values = tmp_path / "values.yaml"
    values.write_text(_VALUES.format(sha=sha, other=SHA_B))
    with pytest.raises(ValueError):
        health.read_source_sha(values)


def test_repo_values_file_carries_a_source_sha() -> None:
    values = (
        _SCRIPT.parents[2]
        / "infrastructure"
        / "helm"
        / "knowledge-graph-analytics"
        / "values-aws.yaml"
    )
    assert health.SHA_RE.fullmatch(health.read_source_sha(values))


def _git(cwd: Path, *args: str, when: int) -> None:
    env = {
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@example.invalid",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@example.invalid",
        "GIT_AUTHOR_DATE": f"@{when} +0000",
        "GIT_COMMITTER_DATE": f"@{when} +0000",
        "HOME": str(cwd),
        "PATH": "/usr/bin:/bin:/usr/local/bin",
    }
    subprocess.run(["git", *args], cwd=cwd, env=env, check=True, capture_output=True)


def test_cli_dates_the_values_file_by_its_last_commit(
    server: tuple[str, _Script], tmp_path: Path
) -> None:
    url, script = server
    values = tmp_path / "values-aws.yaml"
    values.write_text(_VALUES.format(sha=SHA_A, other=SHA_B))
    _git(tmp_path, "init", "-q", when=0)
    _git(tmp_path, "add", values.name, when=0)
    _git(tmp_path, "commit", "-q", "-m", "values", when=1_000_000)
    (tmp_path / "other.txt").write_text("later commit, other file")
    _git(tmp_path, "add", "other.txt", when=0)
    _git(tmp_path, "commit", "-q", "-m", "other", when=2_000_000_000)

    assert health.last_changed_at(values) == 1_000_000

    base = ["--url", url, "--consecutive", "1", "--timeout", "1"]
    base += ["--values-file", str(values)]
    script.responses = [_healthy_with(SHA_A)]
    assert health.main(base) == 0
    script.responses = [_healthy_with(SHA_B)]
    assert health.main(base) == 1
    assert health.main([*base, "--grace-minutes", "1e9"]) == 0
    script.responses = [HEALTHY]
    assert health.main(base) == 0
