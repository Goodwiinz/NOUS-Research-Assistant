"""Behavioural tests for scripts/ci/verify_deploy_health.py (CI7).

A real local HTTP server plays scripted responses; the probe must only pass
on consecutive healthy JSON answers and fail closed on everything else.
"""

from __future__ import annotations

import importlib.util
import socket
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
