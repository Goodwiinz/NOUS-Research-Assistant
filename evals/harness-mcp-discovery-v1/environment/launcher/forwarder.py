"""Loopback TCP forwarder: 127.0.0.1:<listen-port> -> <upstream host:port>.

The harness bridge refuses plain-http API origins that are not loopback, so
the eval exposes the ``mock-nous`` compose service on a loopback port. Bytes
are copied unchanged in both directions; half-closes are propagated so HTTP
keep-alive and request bodies behave exactly as on a direct connection.
Exits with status 3 when the listen port is already taken (another launcher
won the race; the caller then reuses that forwarder). Standard library only.
"""

from __future__ import annotations

import argparse
import socket
import sys
import threading
import time


def log(message: str) -> None:
    stamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    print(f"{stamp} [forwarder] {message}", file=sys.stderr, flush=True)


def pump(source: socket.socket, sink: socket.socket) -> None:
    try:
        while True:
            data = source.recv(65536)
            if not data:
                break
            sink.sendall(data)
    except OSError:
        pass
    finally:
        try:
            sink.shutdown(socket.SHUT_WR)
        except OSError:
            pass


def serve_connection(client: socket.socket, upstream: tuple[str, int]) -> None:
    try:
        server = socket.create_connection(upstream, timeout=10)
        server.settimeout(None)
    except OSError as error:
        log(f"upstream {upstream[0]}:{upstream[1]} unreachable: {error}")
        client.close()
        return
    back = threading.Thread(target=pump, args=(server, client), daemon=True)
    back.start()
    pump(client, server)
    back.join()
    client.close()
    server.close()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--listen-port", type=int, required=True)
    parser.add_argument("--upstream", required=True, help="host:port")
    args = parser.parse_args()
    host, _, port = args.upstream.rpartition(":")
    upstream = (host, int(port))
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        listener.bind(("127.0.0.1", args.listen_port))
    except OSError as error:
        log(f"cannot bind 127.0.0.1:{args.listen_port}: {error}")
        return 3
    listener.listen(64)
    log(f"listening on 127.0.0.1:{args.listen_port} -> {host}:{port}")
    while True:
        client, _address = listener.accept()
        threading.Thread(
            target=serve_connection, args=(client, upstream), daemon=True
        ).start()


if __name__ == "__main__":
    sys.exit(main())
