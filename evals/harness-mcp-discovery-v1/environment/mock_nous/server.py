#!/usr/bin/env python3
"""Deterministic NOUS integration-gateway double for the harness MCP eval.

Serves exactly the two routes the real ``packages/harness-bridge`` MCP server
calls (``packages/harness-bridge/src/mcp/client.ts``):

- ``GET  /api/v1/integrations/tools``      -> ``catalog.json`` (generated from
  ``backend/src/services/integrations/read_tools.py`` by
  ``generate_catalog.py``; never hand-edited).
- ``POST /api/v1/integrations/tools/read`` -> ``{content, is_error,
  source_refs}`` in the backend gateway's per-tool shapes
  (``invoke_read``/``_finish``/``_source_refs`` and the ``tools_impl``
  payloads), answered from ``fixtures.json``.

Both routes demand ``Authorization: Bearer <token>`` and
``X-NOUS-Integration-Grant: nous_ig_<43 chars>`` (401 otherwise), never
redirect, and answer well inside the bridge's 30 s timeout. Every request to
``/api/v1/integrations/*`` is appended to an in-memory log served read-only at
``GET /events``; nothing else can write it. ``GET /healthz`` is readiness.

Fidelity limits (documented in the task spec): search relevance is a keyword
match on the lowercased query, not arXiv/PubMed ranking or the real ILIKE
title match; the arXiv query is rewritten like ``_tool_search_arxiv`` (filler
and venue tokens dropped, ``all:`` fielding, ``submittedDate`` window) and
echoed, but the window is not applied; the do_kb tool behaves as if a
knowledge base were provisioned.
Python standard library only.
"""

from __future__ import annotations

import json
import os
import re
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

HERE = Path(__file__).resolve().parent
CATALOG: list[dict[str, Any]] = json.loads((HERE / "catalog.json").read_text())
CONNECTORS: dict[str, Any] = json.loads((HERE / "connectors.json").read_text())
FIXTURES: dict[str, Any] = json.loads((HERE / "fixtures.json").read_text())
# tools_impl._ARXIV_STOPWORDS / _ARXIV_VENUE_STOPWORDS and
# arxiv_service._ARXIV_QUERY_SYNTAX, generated from backend source by
# generate_catalog.py (which also checks the helpers below for parity).
ARXIV_QUERY: dict[str, Any] = json.loads((HERE / "arxiv_query.json").read_text())

TOOLS = {tool["name"]: tool for tool in CATALOG}
CONNECTOR_INFO = {item["name"]: item for item in CONNECTORS["connectors"]}
MATCH_TERMS = tuple(FIXTURES["match_terms"])
PROJECT = FIXTURES["project"]
CORPUS = {doc["id"]: doc for doc in FIXTURES["corpus"]["documents"]}
# The real document tools ORDER BY Document.created_at DESC.
CORPUS_NEWEST_FIRST = sorted(
    CORPUS.values(), key=lambda doc: doc["created_at"], reverse=True
)
ARXIV = FIXTURES["arxiv"]["papers"]
ARXIV_BY_BASE = {paper["id"].split("v")[0]: paper for paper in ARXIV}
PUBMED = FIXTURES["external"]["pubmed"]["articles"]
DOMAINS = (
    "biomedical",
    "chemistry",
    "genomics",
    "finance",
    "economic",
    "clinical",
    "literature",
    "patent",
    "general",
)

API_PREFIX = "/api/v1/integrations"
CATALOG_PATH = f"{API_PREFIX}/tools"
READ_PATH = f"{API_PREFIX}/tools/read"
# Mirrors read_tools.py constants.
MAX_RESULTS = 50
MAX_EXTERNAL_RESULTS = 20
MAX_DOCUMENT_IDS = 20
MAX_PAGE_CHARS = 48000
MAX_ARTIFACTS = 50
LOCAL_TOOLS = frozenset(
    {
        "get_document_content",
        "retrieve_passages",
        "get_arxiv_paper_content",
        "find_researchers",
        "get_researcher",
    }
)
# Registry tools validate with their pydantic model; only this gateway model
# carries bounds of its own (``_ListProjectArtifactsArgs``: ge=1, le=50).
MODEL_BOUNDED_TOOLS = frozenset({"list_project_artifacts"})
ARXIV_ID_RE = re.compile(
    r"^(\d{4}\.\d{4,5}(v\d+)?|[a-z\-]+(\.[A-Z]{2})?/\d{7}(v\d+)?)$"
)
GRANT_RE = re.compile(r"^nous_ig_[A-Za-z0-9_-]{43}$")
ARXIV_STOPWORDS = frozenset(ARXIV_QUERY["stopwords"])
ARXIV_VENUE_STOPWORDS = frozenset(ARXIV_QUERY["venue_stopwords"])
ARXIV_QUERY_SYNTAX = re.compile(ARXIV_QUERY["query_syntax_pattern"])
NO_ARXIV_RESULTS_WARNING = (
    "No arXiv papers matched the query within the "
    "recency window. Tell the user the search returned no "
    "results — do not claim a search was performed without "
    "naming that it was empty. Suggest broadening the "
    "query, widening recency_days, or removing categories."
)

EVENTS: list[dict[str, Any]] = []
EVENT_LOCK = threading.Lock()
STARTED = time.monotonic()


class ArgumentRejected(ValueError):
    """The gateway's ToolArgumentError: HTTP 422 with a stable detail."""


def record(event: dict[str, Any]) -> None:
    with EVENT_LOCK:
        EVENTS.append(
            {
                "seq": len(EVENTS) + 1,
                "t": round(time.monotonic() - STARTED, 3),
                **event,
            }
        )


def relevant(query: str) -> bool:
    lowered = query.lower()
    return any(term in lowered for term in MATCH_TERMS)


def clamp(value: Any, lo: int, hi: int, default: int) -> int:
    try:
        return max(lo, min(int(value), hi))
    except (TypeError, ValueError, OverflowError):
        return default


def canonical_uuid(value: Any) -> str:
    try:
        return str(uuid.UUID(str(value)))
    except ValueError as error:
        raise ArgumentRejected("document_ids must be UUIDs") from error


# --------------------------------------------------------------------------
# argument validation (read_tools._validate_arguments)
# --------------------------------------------------------------------------
def _type_ok(spec: dict[str, Any], value: Any) -> bool:
    if "anyOf" in spec:
        return any(_type_ok(option, value) for option in spec["anyOf"])
    kind = spec.get("type")
    if kind == "string":
        return isinstance(value, str)
    if kind == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if kind == "boolean":
        return isinstance(value, bool)
    if kind == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if kind == "null":
        return value is None
    if kind == "object":
        return isinstance(value, dict)
    if kind == "array":
        return isinstance(value, list) and all(
            _type_ok(spec.get("items", {}), item) for item in value
        )
    return True


def _bounds_ok(spec: dict[str, Any], value: Any) -> bool:
    if "enum" in spec and value not in spec["enum"]:
        return False
    if isinstance(value, int) and not isinstance(value, bool):
        return spec.get("minimum", value) <= value <= spec.get("maximum", value)
    if isinstance(value, str):
        return spec.get("minLength", 0) <= len(value) <= spec.get("maxLength", 2**31)
    if isinstance(value, list):
        return spec.get("minItems", 0) <= len(value) <= spec.get("maxItems", 2**31)
    return True


def validate_arguments(name: str, arguments: dict[str, Any]) -> None:
    if name not in TOOLS:
        raise ArgumentRejected("tool is not available to integrations")
    schema = TOOLS[name]["input_schema"]
    properties = schema.get("properties", {})
    if set(arguments) - set(properties):
        raise ArgumentRejected("unknown arguments")
    if set(schema.get("required", [])) - set(arguments):
        raise ArgumentRejected("missing required arguments")
    check_bounds = name in LOCAL_TOOLS or name in MODEL_BOUNDED_TOOLS
    for key, value in arguments.items():
        spec = properties[key]
        if not _type_ok(spec, value) or (check_bounds and not _bounds_ok(spec, value)):
            raise ArgumentRejected("invalid argument types")


# --------------------------------------------------------------------------
# result envelope (read_tools._finish / _source_refs)
# --------------------------------------------------------------------------
def source_refs(payload: dict[str, Any]) -> list[dict[str, Any]]:
    if "error" in payload:
        return []
    refs: list[dict[str, Any]] = []
    for ref in payload.get("source_refs") or []:
        if isinstance(ref, dict):
            refs.append(dict(ref))
    for paper in payload.get("papers") or []:
        if paper.get("id"):
            refs.append({"arxiv_id": str(paper["id"])})
    for hit in payload.get("results") or []:
        if hit.get("id"):
            refs.append(
                {
                    "external_id": str(hit["id"]),
                    "connector": hit.get("source") or payload.get("connector"),
                }
            )
    for doc in payload.get("documents") or []:
        if doc.get("id"):
            refs.append({"document_id": str(doc["id"])})
    for chunk in payload.get("chunks") or []:
        if chunk.get("document_id"):
            refs.append({"document_id": str(chunk["document_id"])})
    draft = payload.get("draft")
    if isinstance(draft, dict) and draft.get("id"):
        refs.append({"draft_id": str(draft["id"])})
    return refs[:MAX_RESULTS]


def finish(payload: dict[str, Any]) -> dict[str, Any]:
    is_error = "error" in payload or "reason" in payload
    return {
        "content": [payload],
        "is_error": is_error,
        "source_refs": [] if is_error else source_refs(payload),
    }


def upstream_unavailable() -> dict[str, Any]:
    return finish({"error": "upstream_unavailable"})


def documents_unavailable() -> dict[str, Any]:
    return {
        "content": [
            {
                "error": "requested_documents_unavailable",
                "reason": "requested_documents_unavailable",
                "chunks": [],
                "total": 0,
            }
        ],
        "is_error": True,
        "source_refs": [],
    }


def project_document_ids(raw_ids: Any) -> list[str] | None:
    """read_tools._project_document_ids: 1-20 UUIDs, None if any is foreign."""
    if not isinstance(raw_ids, list) or not 1 <= len(raw_ids) <= MAX_DOCUMENT_IDS:
        raise ArgumentRejected("document_ids must contain 1-20 UUIDs")
    requested = [canonical_uuid(value) for value in raw_ids]
    if not set(requested) <= set(CORPUS):
        return None
    return requested


def page(text: str, offset: int, limit: int) -> tuple[str, int | None]:
    chunk = text[offset : offset + limit]
    end = offset + len(chunk)
    return chunk, end if end < len(text) else None


# --------------------------------------------------------------------------
# tools
# --------------------------------------------------------------------------
def tool_search_documents(args: dict[str, Any]) -> dict[str, Any]:
    query = str(args["query"])
    limit = clamp(args.get("max_results", 10), 1, MAX_RESULTS, 10)
    docs = CORPUS_NEWEST_FIRST if relevant(query) else []
    documents = [
        {
            "id": doc["id"],
            "title": doc["title"],
            "type": doc["type"],
            "created_at": doc["created_at"],
        }
        for doc in docs[:limit]
    ]
    return finish({"documents": documents, "total": len(documents), "query": query})


def tool_list_project_documents(args: dict[str, Any]) -> dict[str, Any]:
    limit = clamp(args.get("limit", MAX_RESULTS), 1, MAX_RESULTS, MAX_RESULTS)
    offset = clamp(args.get("offset", 0), 0, 2**31 - 1, 0)
    docs = CORPUS_NEWEST_FIRST
    window = [
        {
            "id": doc["id"],
            "title": doc["title"],
            "type": doc["type"],
            "status": doc["status"],
        }
        for doc in docs[offset : offset + limit]
    ]
    return finish(
        {
            "project_name": PROJECT["name"],
            "documents": window,
            "total": len(docs),
            "returned": len(window),
            "limit": limit,
            "offset": offset,
            "has_more": offset + len(window) < len(docs),
        }
    )


def tool_do_kb_retrieve(args: dict[str, Any]) -> dict[str, Any]:
    query = str(args["query"])
    document_ids = project_document_ids(args.get("document_ids"))
    if document_ids is None:
        return documents_unavailable()
    top_k = clamp(args.get("top_k", 8), 1, MAX_DOCUMENT_IDS, 8)
    chunks = [
        {
            "document_id": doc_id,
            "title": CORPUS[doc_id]["title"],
            "text": CORPUS[doc_id]["passage"],
            "score": round(0.82 - 0.07 * index, 2),
        }
        for index, doc_id in enumerate(dict.fromkeys(document_ids))
    ][:top_k]
    return finish(
        {
            "chunks": chunks,
            "total": len(chunks),
            "source": "do_kb",
            "query": query,
            "evidence_mode": False,
            "score_semantics": "upstream",
        }
    )


def tool_get_current_draft(_args: dict[str, Any]) -> dict[str, Any]:
    return finish(
        {
            "status": "not_found",
            "project_id": PROJECT["id"],
            "project_name": PROJECT["name"],
            "draft": None,
        }
    )


def tool_list_project_artifacts(_args: dict[str, Any]) -> dict[str, Any]:
    return finish({"artifacts": [], "total": 0})


def sanitize_arxiv_query(query: str) -> str:
    """tools_impl._sanitize_arxiv_query: drop filler, venue and repeated
    tokens; keep the original when nothing would remain."""
    strip = ARXIV_STOPWORDS | ARXIV_VENUE_STOPWORDS
    kept: list[str] = []
    seen: set[str] = set()
    for token in query.split():
        norm = re.sub(r"[^a-z0-9]", "", token.lower())
        if not norm or norm in strip or norm in seen:
            continue
        seen.add(norm)
        kept.append(token)
    return " ".join(kept).strip() or query


def field_arxiv_query(query: str) -> str:
    """arxiv_service.field_arxiv_query: ``all:`` AND fielding, except for
    queries already in arXiv syntax (quotes, parens, fields, operators)."""
    if not query or ARXIV_QUERY_SYNTAX.search(query):
        return query
    return " AND ".join(f"all:{token}" for token in query.split())


def rewritten_arxiv_query(query: str, recency_days: int) -> str:
    """The query tools_impl._tool_search_arxiv echoes: sanitized, fielded
    unless every token is filler, then ANDed with the submittedDate window
    (echoed only; the double does not apply it)."""
    query = sanitize_arxiv_query(query)
    if any(
        re.sub(r"[^a-z]", "", token.lower()) not in ARXIV_STOPWORDS
        for token in query.split()
    ):
        query = field_arxiv_query(query)
    if recency_days > 0:
        cutoff = datetime.now(timezone.utc) - timedelta(days=recency_days)
        window = f"submittedDate:[{cutoff.strftime('%Y%m%d%H%M')} TO 999912312359]"
        query = f"({query}) AND {window}" if query else window
    return query


def tool_search_arxiv(args: dict[str, Any]) -> dict[str, Any]:
    query = str(args["query"])
    # Gateway clamps to 1..20 (default 5); the implementation then caps at 5.
    max_results = min(clamp(args.get("max_results", 5), 1, MAX_EXTERNAL_RESULTS, 5), 5)
    recency_days = clamp(args.get("recency_days", 365), 0, 36525, 365)
    categories = args.get("categories")
    papers = ARXIV if relevant(query) else []
    if categories:
        wanted = {str(item) for item in categories}
        papers = [paper for paper in papers if wanted & set(paper["categories"])]
    results = [
        {
            "id": paper["id"],
            "title": paper["title"],
            "authors": paper["authors"][:3],
            "abstract": paper["abstract"][:250],
            "published": paper["published"],
            "categories": paper["categories"],
            # The real tool reads p["pdf_url"], which the arXiv parser never
            # sets, so the field is always empty on the live backend too.
            "pdf_url": "",
        }
        for paper in papers[:max_results]
    ]
    payload: dict[str, Any] = {
        "papers": results,
        "total": len(results),
        "query": rewritten_arxiv_query(query, recency_days),
    }
    if not results:
        payload["warning"] = NO_ARXIV_RESULTS_WARNING
    return finish(payload)


def _connector_rows(
    query: str, connector: str, max_results: int
) -> list[dict[str, Any]]:
    if connector != "pubmed" or not relevant(query):
        return []
    rows = []
    for article in PUBMED[: min(max_results, 200)]:
        content = "\n".join(
            [
                f"Abstract: {article['abstract']}",
                f"Journal: {article['journal']}",
                f"DOI: {article['doi']}",
            ]
        )
        rows.append(
            {
                "id": article["pmid"],
                "title": article["title"],
                "source": "pubmed",
                "url": f"https://pubmed.ncbi.nlm.nih.gov/{article['pmid']}/",
                "content": content[:300],
                "authors": article["authors"][:5],
                "published_date": article["pub_year"],
                "document_type": "article",
            }
        )
    return rows


def _filters_ok(connector: dict[str, Any], filters: dict[str, Any]) -> bool:
    """ExternalDBConnector.validate_search_filters."""
    allowed = set(connector["supported_filter_keys"])
    for key, value in filters.items():
        if not isinstance(key, str) or key not in allowed:
            return False
        if isinstance(value, str):
            if not value.strip() or len(value) > 200:
                return False
            if any(ord(char) < 32 or ord(char) == 127 for char in value):
                return False
        elif isinstance(value, (bool, int)):
            continue
        elif isinstance(value, float) and value == value and abs(value) != float("inf"):
            continue
        else:
            return False
    return True


def tool_search_external_database(args: dict[str, Any]) -> dict[str, Any]:
    query = str(args["query"])
    if not query:
        return upstream_unavailable()
    max_results = min(
        clamp(args.get("max_results", 10), 1, MAX_EXTERNAL_RESULTS, 10), 20
    )
    connector_name = args.get("connector")
    domain_name = args.get("domain")
    filters = args.get("filters") or {}
    if connector_name:
        info = CONNECTOR_INFO.get(connector_name)
        if info is None or not info["available"]:
            return upstream_unavailable()
        targets = [info]
    elif domain_name:
        if domain_name not in DOMAINS:
            return upstream_unavailable()
        targets = [
            item
            for item in CONNECTORS["connectors"]
            if domain_name in item["domains"] and item["available"]
        ]
    else:
        targets = [item for item in CONNECTORS["connectors"] if item["available"]]
    if not targets:
        return upstream_unavailable()
    if any(not _filters_ok(target, filters) for target in targets):
        return upstream_unavailable()
    results: list[dict[str, Any]] = []
    for target in targets:
        results.extend(_connector_rows(query, target["name"], max_results))
    return finish(
        {
            "query": query,
            "total_results": len(results),
            "results": results,
            "connectors_searched": [target["name"] for target in targets],
        }
    )


def tool_list_external_databases(args: dict[str, Any]) -> dict[str, Any]:
    domain_name = args.get("domain")
    if not domain_name:
        return finish(json.loads(json.dumps(CONNECTORS)))
    if domain_name not in DOMAINS:
        return upstream_unavailable()
    # connector_registry.search_by_domain lists available connectors only.
    connectors = [
        item
        for item in CONNECTORS["connectors"]
        if domain_name in item["domains"] and item["available"]
    ]
    return finish(
        {
            "total": len(connectors),
            "available": len(connectors),
            "connectors": json.loads(json.dumps(connectors)),
        }
    )


def tool_get_document_content(args: dict[str, Any]) -> dict[str, Any]:
    try:
        document_id = str(uuid.UUID(str(args["document_id"])))
    except ValueError as error:
        raise ArgumentRejected("document_id must be a UUID") from error
    doc = CORPUS.get(document_id)
    if doc is None:
        return documents_unavailable()
    mode = str(args.get("mode", "summary"))
    text = f"{doc['summary']}\n\n{doc['passage']}" if mode == "full" else doc["summary"]
    offset = clamp(args.get("offset", 0), 0, 2**31 - 1, 0)
    chunk, next_offset = page(
        text,
        offset,
        clamp(args.get("limit", MAX_PAGE_CHARS), 1, MAX_PAGE_CHARS, MAX_PAGE_CHARS),
    )
    ref = {"document_id": doc["id"]}
    return {
        "content": [
            {
                "document_id": doc["id"],
                "title": doc["title"],
                "mode": mode,
                "offset": offset,
                "next_offset": next_offset,
                "total_chars": len(text),
                "text": chunk,
                "source_refs": [ref],
            }
        ],
        "is_error": False,
        "source_refs": [ref],
    }


def tool_retrieve_passages(args: dict[str, Any]) -> dict[str, Any]:
    query = str(args["query"])
    top_k = clamp(args.get("top_k", 8), 1, MAX_DOCUMENT_IDS, 8)
    if "document_ids" in args:
        document_ids = project_document_ids(args["document_ids"])
        if document_ids is None:
            return finish(documents_unavailable()["content"][0])
    else:
        document_ids = list(CORPUS)
    chunks = []
    if relevant(query):
        chunks = [
            {
                "document_id": doc_id,
                "title": CORPUS[doc_id]["title"],
                "text": CORPUS[doc_id]["passage"],
                "score": round(0.61 - 0.05 * index, 2),
            }
            for index, doc_id in enumerate(dict.fromkeys(document_ids))
        ][:top_k]
    return finish({"chunks": chunks, "total": len(chunks), "query": query})


def tool_get_arxiv_paper_content(args: dict[str, Any]) -> dict[str, Any]:
    arxiv_id = str(args["arxiv_id"])
    if not ARXIV_ID_RE.match(arxiv_id):
        raise ArgumentRejected("invalid arxiv id")
    paper = ARXIV_BY_BASE.get(arxiv_id.split("v")[0])
    if paper is None:
        return finish({"error": "arxiv_unavailable"})
    offset = clamp(args.get("offset", 0), 0, 2**31 - 1, 0)
    limit = clamp(args.get("limit", MAX_PAGE_CHARS), 1, MAX_PAGE_CHARS, MAX_PAGE_CHARS)
    text = paper["full_text"]
    chunk, next_offset = page(text, offset, limit)
    return finish(
        {
            "arxiv_id": arxiv_id,
            "offset": offset,
            "next_offset": next_offset,
            "total_chars": len(text),
            "text": chunk,
            "source_refs": [{"arxiv_id": arxiv_id}],
        }
    )


def _researcher_key(name: Any) -> str:
    return " ".join(str(name).split()).casefold()[:200].strip()


def tool_find_researchers(args: dict[str, Any]) -> dict[str, Any]:
    if not _researcher_key(args["query"]):
        raise ArgumentRejected("query must not be blank")
    # The planted library documents carry no author metadata, so the
    # researcher tools see nobody (read_tools._scan_authored_papers).
    return finish({"researchers": []})


def tool_get_researcher(args: dict[str, Any]) -> dict[str, Any]:
    if not _researcher_key(args["researcher_id"]):
        raise ArgumentRejected("researcher_id must not be blank")
    return {
        "content": [{"error": "researcher_not_found"}],
        "is_error": True,
        "source_refs": [],
    }


DISPATCH = {
    "search_documents": tool_search_documents,
    "list_project_documents": tool_list_project_documents,
    "do_kb_retrieve": tool_do_kb_retrieve,
    "get_current_draft": tool_get_current_draft,
    "list_project_artifacts": tool_list_project_artifacts,
    "search_arxiv": tool_search_arxiv,
    "search_external_database": tool_search_external_database,
    "list_external_databases": tool_list_external_databases,
    "get_document_content": tool_get_document_content,
    "retrieve_passages": tool_retrieve_passages,
    "get_arxiv_paper_content": tool_get_arxiv_paper_content,
    "find_researchers": tool_find_researchers,
    "get_researcher": tool_get_researcher,
}
missing = set(TOOLS) ^ set(DISPATCH)
if missing:
    raise SystemExit(f"catalog/dispatch mismatch: {sorted(missing)}")


def returned_ids(result: dict[str, Any]) -> list[str]:
    ids = []
    for ref in result.get("source_refs") or []:
        for key in ("document_id", "arxiv_id", "external_id"):
            if ref.get(key):
                ids.append(str(ref[key]))
    return ids


# --------------------------------------------------------------------------
# HTTP
# --------------------------------------------------------------------------
class Handler(BaseHTTPRequestHandler):
    server_version = "NOUSHarnessEvalMock/1.0"
    protocol_version = "HTTP/1.1"

    def log_message(self, _format: str, *_args: Any) -> None:
        return

    def send_json(self, status: int, payload: Any) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def read_body(self) -> bytes:
        length = int(self.headers.get("Content-Length") or 0)
        return self.rfile.read(length) if length > 0 else b""

    def auth_state(self) -> tuple[dict[str, bool], bool]:
        authorization = self.headers.get("Authorization") or ""
        grant = self.headers.get("X-NOUS-Integration-Grant") or ""
        present = {"authorization": bool(authorization), "grant": bool(grant)}
        bearer = authorization[7:] if authorization.startswith("Bearer ") else ""
        valid = bool(bearer.strip()) and bool(GRANT_RE.match(grant))
        return present, valid

    def base_event(self, method: str, path: str) -> dict[str, Any]:
        present, _valid = self.auth_state()
        return {
            "method": method,
            "path": path,
            "tool_name": None,
            "arguments": None,
            "invocation_id": None,
            "headers_present": present,
            "user_agent": self.headers.get("User-Agent"),
        }

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler contract
        path = urlparse(self.path).path
        if path == "/healthz":
            self.send_json(HTTPStatus.OK, {"status": "ok", "service": "mock-nous"})
            return
        if path == "/events":
            with EVENT_LOCK:
                snapshot = json.loads(json.dumps(EVENTS))
            self.send_json(HTTPStatus.OK, snapshot)
            return
        if not path.startswith(API_PREFIX):
            self.send_json(HTTPStatus.NOT_FOUND, {"detail": "Not Found"})
            return
        event = self.base_event("GET", path)
        _present, valid = self.auth_state()
        if path != CATALOG_PATH:
            status, payload = HTTPStatus.NOT_FOUND, {"detail": "Not Found"}
        elif not valid:
            status, payload = HTTPStatus.UNAUTHORIZED, {"detail": "Not authenticated"}
        else:
            status, payload = HTTPStatus.OK, CATALOG
        record({**event, "status": int(status)})
        self.send_json(status, payload)

    def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler contract
        path = urlparse(self.path).path
        raw = self.read_body()
        if not path.startswith(API_PREFIX):
            self.send_json(HTTPStatus.NOT_FOUND, {"detail": "Not Found"})
            return
        event = self.base_event("POST", path)
        status, payload = self.handle_read(path, raw, event)
        record({**event, "status": int(status)})
        self.send_json(status, payload)

    def handle_read(
        self, path: str, raw: bytes, event: dict[str, Any]
    ) -> tuple[int, Any]:
        if path != READ_PATH:
            return HTTPStatus.NOT_FOUND, {"detail": "Not Found"}
        try:
            body = json.loads(raw.decode("utf-8") or "null")
        except (UnicodeDecodeError, json.JSONDecodeError):
            body = None
        if isinstance(body, dict):
            event["tool_name"] = body.get("tool_name")
            event["arguments"] = body.get("arguments")
            event["invocation_id"] = body.get("invocation_id")
        _present, valid = self.auth_state()
        if not valid:
            return HTTPStatus.UNAUTHORIZED, {"detail": "Not authenticated"}
        invalid_body = (
            not isinstance(body, dict)
            or set(body) - {"tool_name", "arguments", "invocation_id"}
            or not isinstance(body.get("tool_name"), str)
            or not 1 <= len(body["tool_name"]) <= 64
            or not isinstance(body.get("arguments", {}), dict)
        )
        if not invalid_body:
            try:
                uuid.UUID(str(body.get("invocation_id")))
            except ValueError:
                invalid_body = True
        if invalid_body:
            return HTTPStatus.UNPROCESSABLE_ENTITY, {
                "detail": [{"type": "value_error", "msg": "invalid ToolInvocation"}]
            }
        name = body["tool_name"]
        arguments = body.get("arguments") or {}
        try:
            validate_arguments(name, arguments)
            result = DISPATCH[name](arguments)
        except ArgumentRejected as error:
            return HTTPStatus.UNPROCESSABLE_ENTITY, {"detail": str(error)}
        event["is_error"] = result["is_error"]
        event["returned_ids"] = returned_ids(result)
        return HTTPStatus.OK, result


def main() -> None:
    port = int(os.environ.get("MOCK_NOUS_PORT", "8080"))
    host = os.environ.get("MOCK_NOUS_HOST", "0.0.0.0")
    ThreadingHTTPServer((host, port), Handler).serve_forever()


if __name__ == "__main__":
    main()
