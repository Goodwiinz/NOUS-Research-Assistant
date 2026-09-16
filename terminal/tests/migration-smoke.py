"""Real TTY check for native Ink migration; stdlib only, fake local backend/auth."""

import fcntl
import hashlib
import json
import os
import pty
import select
import signal
import struct
import subprocess
import tempfile
import termios
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

requests: list[dict[str, Any]] = []
confirmations: list[dict[str, Any]] = []


class Handler(BaseHTTPRequestHandler):
    project_gate: threading.Event | None = None
    project_started = threading.Event()

    def log_message(self, format: str, *args: Any) -> None:
        pass

    def do_GET(self) -> None:
        data: Any
        if self.path.startswith("/api/v1/projects?"):
            if self.project_gate is not None:
                self.project_started.set()
                self.project_gate.wait(timeout=10)
            data = {
                "projects": [
                    {
                        "id": "project-1",
                        "name": "Smoke project",
                        "updated_at": "2026-09-15",
                        "document_count": 1,
                        "note_count": 0,
                        "draft_count": 0,
                    }
                ]
            }
        elif self.path.endswith("/projects/project-1/documents"):
            data = {
                "documents": [
                    {
                        "document": {
                            "id": "doc-1",
                            "title": "Smoke paper",
                            "status": "completed",
                        }
                    }
                ]
            }
        elif self.path.endswith("/documents/doc-1"):
            data = {
                "id": "doc-1",
                "title": "Smoke paper",
                "processing_status": "completed",
            }
        elif self.path.endswith("/workspaces"):
            data = [{"id": "workspace"}]
        elif "/conversations?" in self.path:
            data = {"conversations": [{"id": "conversation"}], "has_more": False}
        elif "/threads?" in self.path:
            data = {
                "threads": [
                    {
                        "id": "thread-2",
                        "title": "Saved thread",
                        "updated_at": "2026-09-15",
                    }
                ],
                "has_more": False,
            }
        elif self.path.startswith("/api/v2/threads/thread-2?"):
            data = {"conversation_id": "conversation", "title": "Saved thread"}
        elif self.path.startswith("/api/v2/threads/offline-thread/messages?"):
            self.send_error(503)
            return
        elif "/messages?" in self.path:
            data = {"messages": [], "has_more": False}
        else:
            self.send_error(404)
            return
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        try:
            self.wfile.write(json.dumps(data).encode())
        except (BrokenPipeError, ConnectionResetError):
            pass  # Expected when the client cancels a delayed read.

    def do_POST(self) -> None:
        events: list[tuple[str, dict[str, Any]]]
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        if self.path == "/api/v2/threads":
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"id":"branch-3"}')
            return
        if self.path.endswith("/confirm"):
            confirmations.append(body)
            events = [("token", {"content": "Denied safely"}), ("done", {})]
        else:
            requests.append(body)
            if body["messages"][-1]["content"] == "approval test":
                events = [
                    (
                        "confirmation",
                        {
                            "thread_id": body["thread_id"],
                            "confirmation": {"tool_name": "create_note"},
                        },
                    )
                ]
            else:
                events = [("token", {"content": "Handoff succeeded"}), ("done", {})]
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()
        self.wfile.write(
            "".join(
                f"event: {event}\ndata: {json.dumps(data)}\n\n"
                for event, data in events
            ).encode()
        )


def check() -> None:
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    with tempfile.TemporaryDirectory(prefix="nous-ink-smoke-") as config:
        Path(config, "config.json").write_text(
            json.dumps(
                {
                    "token": "fake-test-token",
                    "user_email": "test@example.invalid",
                    "organization_id": "test",
                    "expires_at": "2099-01-01",
                    "thread_id": "offline-thread",
                }
            )
        )
        env = dict(
            os.environ,
            NOUS_CONFIG_DIR=config,
            NOUS_API_URL=f"http://127.0.0.1:{server.server_port}/api/v1",
            TERM="xterm-256color",
        )
        branch_dir = Path(config, "branches")
        branch_dir.mkdir()
        key = hashlib.sha256(
            f"{env['NOUS_API_URL']}|test|test@example.invalid|offline-thread".encode()
        ).hexdigest()
        Path(branch_dir, f"{key}.json").write_text(
            json.dumps(
                {
                    "headId": "sibling-user",
                    "nodes": [
                        {
                            "parentId": None,
                            "message": {
                                "runtimeId": "cached-user",
                                "threadId": "offline-thread",
                                "role": "user",
                                "content": "Locally saved question",
                                "timestamp": 1,
                            },
                        },
                        {
                            "parentId": None,
                            "message": {
                                "runtimeId": "sibling-user",
                                "threadId": "sibling-thread",
                                "role": "user",
                                "content": "Wrong sibling question",
                                "timestamp": 2,
                            },
                        },
                    ],
                }
            )
        )
        master, slave = pty.openpty()
        fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 40, 120, 0, 0))
        proc = subprocess.Popen(
            ["./nous"],
            cwd=Path(__file__).resolve().parents[2],
            stdin=slave,
            stdout=slave,
            stderr=slave,
            env=env,
            start_new_session=True,
        )
        os.close(slave)
        output = bytearray()

        def drain(duration: float = 0.15) -> None:
            end = time.monotonic() + duration
            while time.monotonic() < end:
                if select.select([master], [], [], 0.02)[0]:
                    try:
                        output.extend(os.read(master, 65536))
                    except OSError:
                        break

        def wait_for(text: str, start: int = 0) -> None:
            end = time.monotonic() + 12
            while text.encode() not in output[start:] and time.monotonic() < end:
                drain()
            assert text.encode() in output[start:], output.decode(errors="replace")[
                -3500:
            ]

        def send(text: str) -> None:
            os.write(master, text.encode())
            drain()

        def command(text: str, expected: str) -> None:
            start = len(output)
            send(text)
            send("\r")
            wait_for(expected, start)

        def wait_saved(key: str, value: Any) -> None:
            deadline = time.monotonic() + 5
            while saved().get(key) != value and time.monotonic() < deadline:
                drain()
            assert saved().get(key) == value, output.decode(errors="replace")[-3500:]

        def saved() -> dict[str, Any]:
            data: dict[str, Any] = json.loads(Path(config, "config.json").read_text())
            return data

        try:
            wait_for("Ask NOUS")
            wait_for("Locally saved question")
            wait_for("Could not refresh")
            assert b"Wrong sibling question" not in output
            drain(0.3)
            send("/")
            wait_for("Commands (")
            send("pro")
            wait_for("Commands (2)")
            send("\r")
            assert not requests, "Completing a command must not send a chat request"
            send("\r")
            wait_for("Pick a project")
            send("\x1b[B")
            send("\r")
            wait_saved("project_id", "project-1")
            command("/papers", "Pick a paper")
            send("\x1b[B")
            send("\r")
            wait_saved("paper_id", "doc-1")
            command("/settings", "Settings ·")
            send("\r")
            wait_for("Select model")
            for _ in range(3):
                send("\x1b[B")
            send("\r")
            wait_saved("model", "gpt-5.6-luna")
            command("/threads pick", "Pick a thread")
            send("\x1b")
            assert saved()["thread_id"] is None
            command("/threads pick", "Pick a thread")
            send("\r")
            wait_saved("thread_id", "thread-2")
            command("/help", "Edits and retries create")
            send("\x1b")
            command("hi", "Handoff succeeded")
            assert len(requests) == 1, requests
            assert requests[0]["messages"][-1]["content"] == "hi", requests
            assert requests[0]["thread_id"] == "thread-2", requests
            assert requests[0]["page_context"]["project_id"] == "project-1", requests
            assert requests[0]["attachment_ids"] == ["doc-1"], requests
            assert requests[0]["model"] == "gpt-5.6-luna", requests
            command("/edit 1", "Edit message")
            send("\x15")
            command("edited hi", "Handoff succeeded")
            wait_saved("thread_id", "branch-3")
            assert requests[-1]["thread_id"] == "branch-3", requests
            assert requests[-1]["messages"][-1]["content"] == "edited hi", requests
            assert len(requests[-1]["messages"]) == 1, requests
            command("/edit 1", "Edit message")
            send("\x1b")
            command("after cancelling edit", "Handoff succeeded")
            assert (
                requests[-1]["messages"][-1]["content"] == "after cancelling edit"
            ), requests
            command("approval test", "Approval required")
            send("\x1b")
            command("no", "Denied safely")
            assert confirmations == [
                {"thread_id": "branch-3", "confirmed": False}
            ], confirmations
            Handler.project_gate = threading.Event()
            send("/projects")
            send("\r")
            assert Handler.project_started.wait(timeout=5), output.decode(
                errors="replace"
            )[-5000:]
            send("\x1b")
            command("/help", "Edits and retries create")
            start = len(output)
            Handler.project_gate.set()
            Handler.project_gate = None
            drain(0.3)
            assert b"Pick a project" not in output[start:], "Dismissed menu reopened"
            send("\x1b")
            send("line one")
            send("\x0a")
            command("line two", "Handoff succeeded")
            assert (
                requests[-1]["messages"][-1]["content"] == "line one\nline two"
            ), requests
            start = len(output)
            send("\x10")
            wait_for("line one", start)
            wait_for("line two", start)
            send("\r")
            wait_for("Handoff succeeded", start)
            assert (
                requests[-1]["messages"][-1]["content"] == "line one\nline two"
            ), requests
            send("unsent draft")
            send("\x03")
            proc.wait(timeout=5)
            assert proc.returncode == 0
            assert Path(config, "draft.txt").read_text() == "unsent draft"
            print(
                "PASS: CI TTY startup, offline selected-branch recovery, slash discovery/completion, Ink-native project/paper/settings/thread menus, Escape cancellation, sending after /help, edit save/cancel, approval focus, delayed read cancellation, multiline recall, shared context and draft persistence."
            )
        finally:
            if proc.poll() is None:
                os.killpg(proc.pid, signal.SIGKILL)
                proc.wait(timeout=5)
            os.close(master)
            server.shutdown()


if __name__ == "__main__":
    check()
