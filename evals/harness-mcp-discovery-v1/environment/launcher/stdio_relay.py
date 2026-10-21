"""Relay the harness's stdio MCP stream to the isolated real bridge service."""

from __future__ import annotations

import os
import socket
import sys
import threading


def main() -> None:
    with socket.create_connection(("bridge", 19090), timeout=20) as connection:
        connection.settimeout(None)

        def send() -> None:
            try:
                while data := os.read(sys.stdin.fileno(), 65536):
                    connection.sendall(data)
            except OSError:
                pass
            finally:
                try:
                    connection.shutdown(socket.SHUT_WR)
                except OSError:
                    pass

        incoming = threading.Thread(target=send, daemon=True)
        incoming.start()
        while data := connection.recv(65536):
            sys.stdout.buffer.write(data)
            sys.stdout.buffer.flush()


if __name__ == "__main__":
    main()
