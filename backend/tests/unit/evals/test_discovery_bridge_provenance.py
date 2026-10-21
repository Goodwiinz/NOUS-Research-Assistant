"""Raw HTTP cannot become real MCP traffic by choosing Node's User-Agent."""

from __future__ import annotations

import importlib.util
import json
import sys
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[4]
SERVER = ROOT / "evals/harness-mcp-discovery-v1/environment/mock_nous/server.py"


def test_gateway_attests_only_its_private_bridge_credential(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    spec = importlib.util.spec_from_file_location(
        "discovery_provenance_gateway", SERVER
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, spec.name, module)
    spec.loader.exec_module(module)
    token = tmp_path / "private-token"
    token.write_text("synthetic-private-bridge-token-" + "X" * 48)
    monkeypatch.setattr(module, "TOKEN_FILE", token)
    server = ThreadingHTTPServer(("127.0.0.1", 0), module.Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    url = f"http://127.0.0.1:{server.server_port}"
    try:
        for bearer, expected in [("agent-chosen", 401), (token.read_text(), 200)]:
            request = urllib.request.Request(
                url + "/api/v1/integrations/tools",
                headers={
                    "Authorization": "Bearer " + bearer,
                    "X-NOUS-Integration-Grant": "nous_ig_" + "A" * 43,
                    "User-Agent": "node",
                },
            )
            try:
                response = urllib.request.urlopen(request)
            except urllib.error.HTTPError as error:
                response = error
            with response:
                assert response.status == expected
        with urllib.request.urlopen(url + "/events") as response:
            events = json.loads(response.read())
        assert [event["bridge_authenticated"] for event in events] == [False, True]
        assert all(event["user_agent"] == "node" for event in events)
        assert token.read_text() not in json.dumps(events)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
