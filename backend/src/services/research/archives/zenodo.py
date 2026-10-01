"""Zenodo sandbox deposition adapter (GOO-318): the only deposit module that
talks HTTP.

Remote flow (legacy REST deposition API, still served by Zenodo):
``POST /deposit/depositions`` (draft: id, bucket link, pre-reserved DOI) ->
``PUT {bucket}/{name}`` per file (md5 checksum) -> ``POST
/deposit/depositions/{id}/actions/publish`` (record id, DOI, submitted) ->
``GET /records/{id}`` read-back.

Errors: a timeout, transport failure or 5xx is ``retryable`` because the
remote side may have acted (the service records it as outcome ``unknown``,
never ``failed``); 404, 409 and 429 are retryable; any other 4xx is not.
The token is sent only to the configured Zenodo host, never logged and never
part of a returned value. Every return is normalized; ``raw`` is the remote
JSON for the caller to redact and retain.
"""

import hashlib
import logging
from typing import Any, Mapping, cast
from urllib.parse import urlsplit

import httpx
from pydantic import SecretStr

logger = logging.getLogger(__name__)
_RETRYABLE_4XX = {404, 409, 429}
# ponytail: the newest 100 depositions of the service account; page through
# when one account holds more in-flight drafts than that.
_FIND_PAGE = 100


class ZenodoError(Exception):
    def __init__(self, message: str, *, retryable: bool, status: int | None = None):
        super().__init__(message)
        self.retryable = retryable
        self.status = status


def _md5(checksum: Any) -> str | None:
    if not isinstance(checksum, str) or not checksum:
        return None
    return checksum.split(":", 1)[1] if ":" in checksum else checksum


def _files(entries: Any) -> list[dict[str, Any]]:
    return [
        {
            "name": str(f.get("filename") or f.get("key")),
            "md5": _md5(f.get("checksum")),
        }
        for f in entries or []
        if isinstance(f, Mapping)
    ]


def _deposition(raw: Mapping[str, Any]) -> dict[str, Any]:
    metadata = raw.get("metadata") or {}
    prereserve = metadata.get("prereserve_doi") or {}
    record_id = raw.get("record_id")
    return {
        "deposition_id": str(raw["id"]),
        "bucket": (raw.get("links") or {}).get("bucket"),
        "prereserve_doi": prereserve.get("doi"),
        "state": raw.get("state"),
        "submitted": raw.get("submitted") is True,
        "record_id": None if record_id is None else str(record_id),
        "doi": raw.get("doi") or None,
        "notes": metadata.get("notes"),
        "files": _files(raw.get("files")),
        "raw": dict(raw),
    }


class ZenodoAdapter:
    def __init__(
        self,
        base_url: str,
        token: SecretStr,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        timeout: float = 120.0,
    ) -> None:
        self._base = base_url.rstrip("/")
        self._host = urlsplit(self._base).hostname
        self._token = token
        self._transport = transport
        self._timeout = httpx.Timeout(timeout, connect=10.0)

    async def _call(
        self, method: str, url: str, **kwargs: Any
    ) -> tuple[int, dict[str, Any] | list[Any] | None]:
        if not url.startswith("http"):
            url = f"{self._base}{url}"
        if urlsplit(url).hostname != self._host:
            # Never send the token to a host the bucket link points elsewhere.
            raise ZenodoError("unexpected remote host", retryable=False)
        headers = {"Authorization": f"Bearer {self._token.get_secret_value()}"}
        try:
            async with httpx.AsyncClient(
                transport=self._transport, timeout=self._timeout
            ) as client:
                response = await client.request(method, url, headers=headers, **kwargs)
        except httpx.TimeoutException as error:
            raise ZenodoError("timeout", retryable=True) from error
        except httpx.TransportError as error:
            raise ZenodoError("transport_error", retryable=True) from error
        path = urlsplit(url).path
        logger.info("zenodo %s %s -> %s", method, path, response.status_code)
        if response.status_code >= 500:
            raise ZenodoError(
                f"http_{response.status_code}",
                retryable=True,
                status=response.status_code,
            )
        if response.status_code >= 400 and response.status_code != 404:
            raise ZenodoError(
                f"http_{response.status_code}",
                retryable=response.status_code in _RETRYABLE_4XX,
                status=response.status_code,
            )
        if response.status_code == 404:
            return 404, None
        return response.status_code, response.json() if response.content else None

    def _found(self, status: int, body: Any, what: str) -> dict[str, Any]:
        if status == 404 or not isinstance(body, dict):
            raise ZenodoError(f"{what}_not_found", retryable=True, status=404)
        return cast(dict[str, Any], body)

    async def create_draft(
        self, metadata: Mapping[str, Any], marker: str
    ) -> dict[str, Any]:
        """A new draft whose ``metadata.notes`` carries ``marker``."""
        status, body = await self._call(
            "POST",
            "/deposit/depositions",
            json={"metadata": {**metadata, "notes": marker}},
        )
        return _deposition(self._found(status, body, "deposition"))

    async def upload_file(self, bucket: str, name: str, data: bytes) -> dict[str, Any]:
        """One file into the draft's bucket; ``md5`` is Zenodo's checksum and
        ``local_md5`` ours over the same bytes."""
        status, body = await self._call(
            "PUT", f"{bucket.rstrip('/')}/{name}", content=data
        )
        raw = self._found(status, body, "bucket")
        return {
            "name": str(raw.get("key") or name),
            "md5": _md5(raw.get("checksum")),
            "local_md5": hashlib.md5(data, usedforsecurity=False).hexdigest(),
            "size": raw.get("size"),
            "raw": raw,
        }

    async def publish(self, deposition_id: str) -> dict[str, Any]:
        status, body = await self._call(
            "POST", f"/deposit/depositions/{deposition_id}/actions/publish"
        )
        return _deposition(self._found(status, body, "deposition"))

    async def get_deposition(self, deposition_id: str) -> dict[str, Any] | None:
        """The draft or published deposition; None when Zenodo has no such id."""
        status, body = await self._call("GET", f"/deposit/depositions/{deposition_id}")
        if status == 404 or not isinstance(body, dict):
            return None
        return _deposition(body)

    async def find_by_operation(self, marker: str) -> dict[str, Any] | None:
        """The account's deposition whose notes carry ``marker``, if any.
        Matched client-side on the exact marker, never on search relevance."""
        status, body = await self._call(
            "GET",
            "/deposit/depositions",
            params={"sort": "mostrecent", "size": _FIND_PAGE, "all_versions": "true"},
        )
        if status == 404 or not isinstance(body, list):
            return None
        for raw in body:
            if not isinstance(raw, Mapping):
                continue
            notes = (raw.get("metadata") or {}).get("notes")
            if isinstance(notes, str) and marker in notes:
                return _deposition(raw)
        return None

    async def get_record(self, record_id: str) -> dict[str, Any] | None:
        """The published record as Zenodo serves it publicly."""
        status, body = await self._call("GET", f"/records/{record_id}")
        if status == 404 or not isinstance(body, dict):
            return None
        return {
            "record_id": str(body.get("id")),
            "doi": body.get("doi")
            or (body.get("pids") or {}).get("doi", {}).get("identifier"),
            "files": _files(
                body["files"].get("entries", {}).values()
                if isinstance(body.get("files"), dict)
                else body.get("files")
            ),
            "raw": body,
        }


def from_settings() -> ZenodoAdapter | None:
    """The configured adapter, or None without a token or account label.
    The only reader of ``ZENODO_SANDBOX_TOKEN`` (protected configuration)."""
    from src.core.config import settings

    token = settings.ZENODO_SANDBOX_TOKEN
    if (
        token is None
        or not token.get_secret_value()
        or not settings.ZENODO_ACCOUNT_LABEL
    ):
        return None
    return ZenodoAdapter(settings.ZENODO_BASE_URL, token)
