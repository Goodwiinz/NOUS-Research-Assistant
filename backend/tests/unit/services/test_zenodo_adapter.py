"""Zenodo sandbox adapter (GOO-318) against ``httpx.MockTransport``.

The live sandbox is NOT RUN here (no ``ZENODO_SANDBOX_TOKEN``); these tests
pin the request shapes, the error classification and the token handling.
"""

import json
import logging
from uuid import uuid4

import httpx
import pytest
from pydantic import SecretStr, ValidationError

from src.core.config import Settings
from src.services.research.archives.zenodo import ZenodoAdapter, ZenodoError

pytestmark = pytest.mark.unit

BASE = "https://sandbox.zenodo.org/api"
# Built by concatenation so no token-shaped literal is committed.
TOKEN = "zen" + "odo-" + uuid4().hex


def _adapter(handler: object) -> ZenodoAdapter:
    return ZenodoAdapter(
        BASE, SecretStr(TOKEN), transport=httpx.MockTransport(handler)  # type: ignore[arg-type]
    )


def _deposition(dep_id: int, notes: str, **extra: object) -> dict[str, object]:
    return {
        "id": dep_id,
        "state": "unsubmitted",
        "submitted": False,
        "links": {"bucket": f"{BASE}/files/bucket-{dep_id}"},
        "metadata": {
            "notes": notes,
            "prereserve_doi": {"doi": f"10.5072/zenodo.{dep_id}"},
        },
        "files": [],
        **extra,
    }


async def test_timeout_is_unknown_not_failed() -> None:
    def timeout(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow", request=request)

    with pytest.raises(ZenodoError) as error:
        await _adapter(timeout).create_draft({"title": "t"}, "nous-operation:1")
    assert error.value.retryable and error.value.status is None
    for status, retryable in ((502, True), (429, True), (409, True), (400, False)):

        def respond(request: httpx.Request, status: int = status) -> httpx.Response:
            return httpx.Response(status, json={"message": "x"})

        with pytest.raises(ZenodoError) as failed:
            await _adapter(respond).publish("7")
        assert (failed.value.retryable, failed.value.status) == (retryable, status)

    def missing(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"message": "gone"})

    assert await _adapter(missing).get_deposition("7") is None
    assert await _adapter(missing).get_record("7") is None


async def test_find_by_operation_matches_marker_only() -> None:
    mine, other = f"nous-operation:{uuid4()}", f"nous-operation:{uuid4()}"
    seen: list[httpx.Request] = []

    def listing(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(
            200,
            json=[
                _deposition(1, other),
                _deposition(2, f"<p>{mine}</p>"),
                _deposition(3, "unrelated"),
            ],
        )

    found = await _adapter(listing).find_by_operation(mine)
    assert found is not None and found["deposition_id"] == "2"
    assert found["prereserve_doi"] == "10.5072/zenodo.2"
    assert found["bucket"] == f"{BASE}/files/bucket-2"
    assert seen[0].url.path == "/api/deposit/depositions"
    assert seen[0].url.params["sort"] == "mostrecent"
    assert (
        await _adapter(listing).find_by_operation(f"nous-operation:{uuid4()}") is None
    )


async def test_token_never_in_logged_request(caplog: pytest.LogCaptureFixture) -> None:
    sent: list[httpx.Request] = []

    def zenodo(request: httpx.Request) -> httpx.Response:
        sent.append(request)
        if request.method == "POST" and request.url.path.endswith("/depositions"):
            body = json.loads(request.content)
            return httpx.Response(201, json=_deposition(9, body["metadata"]["notes"]))
        if request.method == "PUT":
            return httpx.Response(
                201, json={"key": "package.zip", "checksum": "md5:abc", "size": 3}
            )
        published = _deposition(
            9, "n", submitted=True, state="done", record_id=9, doi="10.5072/zenodo.9"
        )
        return httpx.Response(202, json=published)

    adapter = _adapter(zenodo)
    with caplog.at_level(logging.DEBUG):
        draft = await adapter.create_draft({"title": "t"}, "nous-operation:1")
        upload = await adapter.upload_file(draft["bucket"], "package.zip", b"abc")
        published = await adapter.publish(draft["deposition_id"])
    assert all(r.headers["Authorization"] == f"Bearer {TOKEN}" for r in sent)
    assert json.loads(sent[0].content)["metadata"]["notes"] == "nous-operation:1"
    assert upload["md5"] == "abc"
    assert upload["local_md5"] == "900150983cd24fb0d6963f7d28e17f72"
    assert published["submitted"] and published["record_id"] == "9"
    assert TOKEN not in caplog.text and "Bearer" not in caplog.text
    assert TOKEN not in repr([draft, upload, published])
    assert "/api/deposit/depositions" in caplog.text
    # A bucket link on another host never receives the token.
    with pytest.raises(ZenodoError) as error:
        await adapter.upload_file("https://evil.example/files/b", "x", b"x")
    assert not error.value.retryable
    assert len(sent) == 3


def test_non_sandbox_host_rejected_by_settings() -> None:
    with pytest.raises(ValidationError, match="sandbox.zenodo.org"):
        Settings(ZENODO_BASE_URL="https://zenodo.org/api")
    allowed = Settings(
        ZENODO_BASE_URL="https://zenodo.org/api", ZENODO_SANDBOX_ONLY=False
    )
    assert allowed.ZENODO_BASE_URL == "https://zenodo.org/api"
    configured = Settings(ZENODO_SANDBOX_TOKEN=SecretStr(TOKEN))
    assert configured.ZENODO_SANDBOX_TOKEN is not None
    assert TOKEN not in repr(configured) and TOKEN not in str(configured.model_dump())
