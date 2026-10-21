"""Host the real stdio bridge outside the agent's process/filesystem boundary.

Only this service and the gateway mount the private bearer volume. Each TCP
connection carries MCP bytes to a real bridge subprocess, never a shell.
"""

from __future__ import annotations

import os
import secrets
import socket
import socketserver
import subprocess
import threading
from pathlib import Path

TOKEN = Path("/run/nous-bridge/access-token")
READY = Path("/tmp/nous-bridge-ready")


def provision_token() -> None:
    TOKEN.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chown(TOKEN.parent, 10001, 10001)
    os.chmod(TOKEN.parent, 0o700)
    try:
        fd = os.open(TOKEN, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        if len(TOKEN.read_text().strip()) < 40:
            raise RuntimeError("Invalid private bridge credential") from None
    else:
        with os.fdopen(fd, "w") as handle:
            handle.write(secrets.token_urlsafe(48))
            handle.flush()
            os.fsync(handle.fileno())
        os.chown(TOKEN, 10001, 10001)
        os.chmod(TOKEN, 0o600)


class BridgeConnection(socketserver.BaseRequestHandler):
    def handle(self) -> None:
        client: socket.socket = self.request
        process = subprocess.Popen(
            ["/usr/bin/python3", "-I", "/opt/nous-eval/launch.py"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            env={
                "PATH": "/usr/bin:/bin",
                "LANG": "C.UTF-8",
                "NOUS_EVAL_ACCESS_TOKEN_FILE": str(TOKEN),
                "NOUS_EVAL_RUN_DIR": "/tmp/nous-private-bridge",
            },
        )
        if process.stdin is None or process.stdout is None:
            raise RuntimeError("Bridge pipes unavailable")
        input_pipe, output_pipe = process.stdin, process.stdout

        def receive() -> None:
            try:
                while data := client.recv(65536):
                    input_pipe.write(data)
                    input_pipe.flush()
            except (OSError, ValueError):
                pass
            finally:
                input_pipe.close()

        incoming = threading.Thread(target=receive, daemon=True)
        incoming.start()
        try:
            while data := os.read(output_pipe.fileno(), 65536):
                client.sendall(data)
        except OSError:
            pass
        finally:
            try:
                client.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
            output_pipe.close()
            incoming.join(timeout=1)


def main() -> None:
    provision_token()
    with socketserver.ThreadingTCPServer(
        ("0.0.0.0", 19090), BridgeConnection
    ) as server:
        server.daemon_threads = True
        READY.touch(mode=0o644)
        server.serve_forever()


if __name__ == "__main__":
    main()
